"""Search the site and keep the XML of every notice found in the local cache.

A notice the site will not give does not stop the run: some never come through (road maintenance contracts with
thousands of positions break off on the way). Only when several fail in a row does the run ask the site something it
surely has — a notice it gave a moment ago, or the search page — and stops if that fails too: then the site is down,
and the notices not tried yet are left for the next run instead of being marked as failed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .cache import NoticeCache
from .website.client import RateLimited, SiteError
from .website.notice import download_notice
from .website.search import Client, SearchHit, SearchQuery, read_page, search

log = logging.getLogger("zkparser.website")

MAX_FAILURES_IN_ROW = 3  # then check whether the site answers at all

DOWNLOADED = "скачано"
CACHED = "из кэша"
FAILED = "ошибка"
RESTING = "пропущено: не скачивается"

Progress = Callable[[int, int, SearchHit, str], None]  # number, total, notice, one of the statuses above
Cancel = Callable[[], bool]  # asked between notices: True stops the run, what was downloaded stays in the cache
Choose = Callable[[list[SearchHit]], set[str]]  # the registry numbers worth downloading among what the search found
STOPPED = "Остановлено по запросу"


@dataclass
class FetchReport:
    found: int = 0
    downloaded: int = 0
    cached: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # registry number, reason
    resting: list[str] = field(default_factory=list)  # failed in earlier runs too: not tried this time
    skipped: int = 0  # not chosen for download: neither the niche's codes nor its words in the title
    truncated: bool = False  # the search hit the site's limit of 5 000 results
    error: str = ""  # why the run stopped early; empty if it went through
    hits: list[SearchHit] = field(default_factory=list)  # everything the search found


def fetch_notices(
    client: Client,
    cache: NoticeCache,
    query: SearchQuery,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    *,
    choose: Choose | None = None,
) -> FetchReport:
    """Search and download. ``choose`` picks the notices worth downloading (see shortlist.py); when it cannot —
    the site fails it — every notice is downloaded, as without it."""
    report = FetchReport()
    try:
        result = search(client, query)
    except SiteError as error:
        report.error = str(error)
        return report
    report.hits, report.truncated = result.hits, result.truncated
    only = None
    if choose is not None:
        try:
            only = choose(result.hits)
        except SiteError as error:
            log.warning("Не удалось отобрать закупки по кодам ниши (%s): скачиваются все", error)
    download_notices(client, cache, report, progress, cancel, probe=lambda: read_page(client, query, 1), only=only)
    return report


def download_notices(
    client: Client,
    cache: NoticeCache,
    report: FetchReport,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    *,
    probe: Callable[[], object] | None = None,
    only: set[str] | None = None,
) -> None:
    """Keep the XML of the notices in ``report.hits`` in the cache — of those in ``only`` when it is given; counts and
    the reason to stop go to the report.

    ``probe`` asks the site something it surely answers (the search page of the query) when no notice has come
    through yet in this run and several have failed in a row.
    """
    hits = report.hits if only is None else [hit for hit in report.hits if hit.reg_number in only]
    report.found = len(report.hits)
    report.skipped = len(report.hits) - len(hits)
    failures_in_row = 0
    last_good: SearchHit | None = None  # a notice the site gave in this run: asking again tells whether it is up
    for number, hit in enumerate(hits, 1):
        if cancel and cancel():
            report.error = STOPPED
            return
        if cache.is_fresh(hit):
            report.cached += 1
            status = CACHED
        elif cache.resting(hit.reg_number):
            report.resting.append(hit.reg_number)
            status = RESTING
        else:
            try:
                cache.store(hit, download_notice(client, hit.reg_number, hit.guid))
            except RateLimited as error:
                report.error = str(error)
                return
            except SiteError as error:
                cache.record_failure(hit.reg_number, str(error))
                report.failed.append((hit.reg_number, str(error)))
                failures_in_row += 1
                if failures_in_row >= MAX_FAILURES_IN_ROW:
                    try:
                        answers = _site_answers(client, last_good, probe)
                    except RateLimited as limited:
                        report.error = str(limited)
                        return
                    if not answers:
                        report.error = f"{error}. Сайт ЕИС не отвечает — запуск остановлен, следующий продолжит."
                        return
                    log.info("ЕИС отвечает: не скачались отдельные извещения, запуск продолжается")
                    failures_in_row = 0
                status = FAILED
            else:
                report.downloaded += 1
                failures_in_row = 0
                last_good = hit
                status = DOWNLOADED
        if progress:
            progress(number, len(hits), hit, status)


def chosen(report: FetchReport) -> str:
    """", для скачивания отобрано 40" for a region's line when the niche picked what to download; empty otherwise."""
    return f", для скачивания отобрано {report.found - report.skipped}" if report.skipped else ""


def problems(report: FetchReport) -> str:
    """", не скачалось 2, пропущено 3 (не скачиваются)" for a region's line; empty when all went well."""
    parts = []
    if report.failed:
        parts.append(f"не скачалось {len(report.failed)}")
    if report.resting:
        parts.append(f"пропущено {len(report.resting)} (не скачиваются)")
    return "".join(f", {part}" for part in parts)


def _site_answers(client: Client, last_good: SearchHit | None, probe: Callable[[], object] | None) -> bool:
    """Whether the site still gives what it surely has; False also when there is nothing to ask."""
    try:
        if last_good:
            download_notice(client, last_good.reg_number, last_good.guid)
        elif probe is not None:
            probe()
        else:
            return False
    except RateLimited:
        raise
    except SiteError:
        return False
    return True
