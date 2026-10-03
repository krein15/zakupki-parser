"""Search the site and keep the XML of every notice found in the local cache."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .cache import NoticeCache
from .website.client import RateLimited, SiteError
from .website.notice import download_notice
from .website.search import Client, SearchHit, SearchQuery, search

MAX_FAILURES_IN_ROW = 3  # the site is probably down: stop instead of retrying every notice

DOWNLOADED = "скачано"
CACHED = "из кэша"
FAILED = "ошибка"

Progress = Callable[[int, int, SearchHit, str], None]  # number, total, notice, one of the statuses above


@dataclass
class FetchReport:
    found: int = 0
    downloaded: int = 0
    cached: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # registry number, reason
    truncated: bool = False  # the search hit the site's limit of 5 000 results
    error: str = ""  # why the run stopped early; empty if it went through
    hits: list[SearchHit] = field(default_factory=list)  # everything the search found


def fetch_notices(
    client: Client, cache: NoticeCache, query: SearchQuery, progress: Progress | None = None
) -> FetchReport:
    report = FetchReport()
    try:
        result = search(client, query)
    except SiteError as error:
        report.error = str(error)
        return report
    report.hits, report.found, report.truncated = result.hits, len(result.hits), result.truncated

    failures_in_row = 0
    for number, hit in enumerate(result.hits, 1):
        if cache.is_fresh(hit):
            report.cached += 1
            status = CACHED
        else:
            try:
                cache.store(hit, download_notice(client, hit.reg_number))
            except RateLimited as error:
                report.error = str(error)
                return report
            except SiteError as error:
                report.failed.append((hit.reg_number, str(error)))
                failures_in_row += 1
                if failures_in_row >= MAX_FAILURES_IN_ROW:
                    report.error = f"{error}. Несколько закупок подряд не скачались, запуск остановлен."
                    return report
                status = FAILED
            else:
                report.downloaded += 1
                failures_in_row = 0
                status = DOWNLOADED
        if progress:
            progress(number, report.found, hit, status)
    return report
