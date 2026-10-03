"""SiteClient: spacing, Retry-After and retries — on a fake session and a fake clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests

from zkparser.website.client import (
    CA_BUNDLE,
    MAX_THROTTLED,
    NETWORK_RETRIES,
    RateLimited,
    SiteClient,
    SiteError,
    new_session,
    retry_after,
)


class FakeResponse:
    def __init__(self, status: int = 200, content: bytes = b"ok", headers: dict | None = None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class FakeSession:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(*answers, interval: float = 2.5):
    time, session = FakeTime(), FakeSession(*answers)
    return SiteClient(interval=interval, session=session, clock=time.clock, sleep=time.sleep), session, time


def test_returns_body_from_site_url():
    client, session, _ = make_client(FakeResponse(content=b"<html/>"))
    assert client.get("/epz/x.html", {"a": "1"}) == b"<html/>"
    assert session.calls == [("https://zakupki.gov.ru/epz/x.html", {"a": "1"})]


def test_spaces_requests_out():
    client, _, time = make_client(FakeResponse(), FakeResponse(), FakeResponse())
    client.get("/a")
    time.now += 1.0  # the caller spent a second between requests
    client.get("/b")
    client.get("/c")
    assert time.sleeps == pytest.approx([1.5, 2.5])


def test_no_wait_when_the_interval_has_passed():
    client, _, time = make_client(FakeResponse(), FakeResponse())
    client.get("/a")
    time.now += 10
    client.get("/b")
    assert time.sleeps == []


def test_waits_as_long_as_retry_after_asks():
    client, session, time = make_client(FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(content=b"x"))
    assert client.get("/a") == b"x"
    assert time.sleeps == [7]
    assert client.stats.throttled == 1
    assert len(session.calls) == 2


def test_gives_up_after_many_429_in_a_row():
    client, session, _ = make_client(*[FakeResponse(429, headers={"Retry-After": "5"})] * (MAX_THROTTLED + 1))
    with pytest.raises(RateLimited, match="ограничил частоту"):
        client.get("/a")
    assert len(session.calls) == MAX_THROTTLED + 1


def test_retries_server_errors_with_growing_pauses():
    client, _, time = make_client(FakeResponse(503), FakeResponse(502), FakeResponse(content=b"ok"))
    assert client.get("/a") == b"ok"
    assert [s for s in time.sleeps if s >= 5] == [5, 10]
    assert client.stats.retried == 2


def test_network_errors_end_in_a_readable_error():
    errors = [requests.ConnectionError("boom")] * (NETWORK_RETRIES + 1)
    client, session, _ = make_client(*errors)
    with pytest.raises(SiteError, match="не отвечает"):
        client.get("/a")
    assert len(session.calls) == NETWORK_RETRIES + 1


def test_client_errors_are_not_retried():
    client, session, _ = make_client(FakeResponse(404))
    with pytest.raises(SiteError, match="404"):
        client.get("/a")
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    ("header", "seconds"),
    [("12", 12), ("", 5), ("soon", 5), ("0", 1), ("100000", 120)],
)
def test_retry_after_values(header, seconds):
    assert retry_after(FakeResponse(429, headers={"Retry-After": header})) == seconds


def test_retry_after_http_date():
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 28 <= retry_after(FakeResponse(429, headers={"Retry-After": when})) <= 30


def test_session_verifies_against_the_bundled_root():
    session = new_session()
    assert session.verify == str(CA_BUNDLE)
    assert "BEGIN CERTIFICATE" in CA_BUNDLE.read_text(encoding="ascii")
    assert session.headers["User-Agent"].startswith("zakupki-parser/")
