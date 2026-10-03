"""HTTP access to zakupki.gov.ru: one polite client for every request the website source makes.

* TLS. The site's certificate chains up to the Russian Trusted Root CA (Минцифры), which neither certifi nor a
  clean Windows install trusts. The root ships with the package and every request is verified against it;
  verification is never switched off.
* Rate limit. After a burst of about 70 requests the site answers 429 with ``Retry-After: 5`` and then lets
  through about 30 requests a minute. The client spaces requests out, waits as long as Retry-After asks and
  stops after several 429 in a row instead of pressing on.
* Identity. Requests carry an honest User-Agent with a link to the project; the site serves it like a browser.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from .. import __version__

log = logging.getLogger("zkparser.website")

BASE_URL = "https://zakupki.gov.ru"
CA_BUNDLE = Path(__file__).resolve().parents[1] / "certs" / "russian_trusted_root_ca.pem"
USER_AGENT = f"zakupki-parser/{__version__} (+https://github.com/krein15/zakupki-parser)"

REQUEST_INTERVAL = 2.5  # seconds between requests: 24 a minute, below the ~30 the site lets through
TIMEOUT = 60
DEFAULT_RETRY_AFTER = 5  # what the site sends with its 429
MAX_RETRY_AFTER = 120
MAX_THROTTLED = 6  # 429 answers in a row before giving up
NETWORK_RETRIES = 3
BACKOFF = 5  # first pause after a network error or a 5xx, doubled on every retry


class SiteError(RuntimeError):
    """The site could not be read. The message can be shown to the user as is."""


class RateLimited(SiteError):
    """The site keeps answering 429: the run stops rather than keep knocking."""


@dataclass
class ClientStats:
    requests: int = 0
    throttled: int = 0  # 429 answers
    retried: int = 0  # retries after network errors and 5xx


class SiteClient:
    def __init__(
        self,
        *,
        interval: float = REQUEST_INTERVAL,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self.stats = ClientStats()
        self._session = session or new_session()
        self._clock = clock
        self._sleep = sleep
        self._last_request: float | None = None

    def get(self, path: str, params: Mapping[str, str] | None = None) -> bytes:
        """GET a page of the site and return its body, waiting out the rate limit and transient failures."""
        url = BASE_URL + path
        throttled = failures = 0
        while True:
            self._wait_turn()
            self.stats.requests += 1
            try:
                response = self._session.get(url, params=params, timeout=TIMEOUT)
            except requests.RequestException as error:
                failures += 1
                if failures > NETWORK_RETRIES:
                    raise SiteError(f"Сайт ЕИС не отвечает: {error}") from error
                self._back_off(failures, type(error).__name__)
                continue

            status = response.status_code
            if status == 429:
                throttled += 1
                self.stats.throttled += 1
                if throttled > MAX_THROTTLED:
                    raise RateLimited(
                        "Сайт ЕИС ограничил частоту запросов и не снимает ограничение. "
                        "Подождите несколько минут и запустите снова."
                    )
                wait = retry_after(response)
                log.info("ЕИС просит подождать %d с", wait)
                self._sleep(wait)
                continue
            if status >= 500:
                failures += 1
                if failures > NETWORK_RETRIES:
                    raise SiteError(f"Сайт ЕИС ответил ошибкой {status}")
                self._back_off(failures, f"HTTP {status}")
                continue
            if status != 200:
                raise SiteError(f"Сайт ЕИС ответил ошибкой {status} на {path}")
            return response.content

    def _wait_turn(self) -> None:
        if self._last_request is not None:
            delay = self._last_request + self.interval - self._clock()
            if delay > 0:
                self._sleep(delay)
        self._last_request = self._clock()

    def _back_off(self, attempt: int, reason: str) -> None:
        self.stats.retried += 1
        wait = BACKOFF * 2 ** (attempt - 1)
        log.warning("ЕИС: %s, повтор через %d с", reason, wait)
        self._sleep(wait)


def new_session() -> requests.Session:
    session = requests.Session()
    session.verify = str(CA_BUNDLE)
    session.headers["User-Agent"] = USER_AGENT
    return session


def retry_after(response: requests.Response) -> int:
    """Seconds to wait from a Retry-After header (a number or an HTTP date); the site's usual 5 if it is odd."""
    value = response.headers.get("Retry-After", "")
    try:
        seconds = int(value)
    except ValueError:
        try:
            seconds = int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            seconds = DEFAULT_RETRY_AFTER
    return min(max(seconds, 1), MAX_RETRY_AFTER)
