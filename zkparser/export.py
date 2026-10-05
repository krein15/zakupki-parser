"""Open notices of regions in a price range, without keywords or a profile: "what can I bid on right now".

The list comes straight from the search results, one request per 50 notices. The search's stage "Подача заявок" is
not enough on its own: the site keeps it for years after the deadline (Tyumen, 1–5 mln ₽: 749 of 855 such notices had
closed, the oldest in 2014). So a notice counts as open while its application deadline has not passed.

Only notices published in the last LOOKBACK_DAYS days are searched. Applications usually last one to three weeks, but
a deadline can be extended: in Sverdlovsk region one notice was still open 105 days after publication. The stale
"Подача заявок" ones are years old, so a window of half a year costs nearly nothing (Sverdlovsk: 700 notices in 60
days, 701 in 180, 14 116 with no window).

The search shows the organization that placed the notice, often an authorized body rather than the customer. With
``details`` the XML of every open notice is downloaded (one request each, cached) for the real customers with their
INNs, the exact deadline with its time zone and the positions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .cache import NoticeCache
from .fetch import STOPPED, Cancel, FetchReport, Progress, download_notices
from .models import Notice
from .notice_xml import NoticeFormatError, parse_notice
from .pipeline import NO_DEADLINE
from .website.client import SiteError
from .website.search import Client, SearchHit, SearchQuery, Stage, search

LOOKBACK_DAYS = 180

Listing = Callable[[str, int, int], None]  # region, notices collected, the total the site reports


@dataclass(frozen=True)
class ExportQuery:
    regions: tuple[str, ...]  # 11-digit KLADR codes
    price_from: int | None = None
    price_to: int | None = None
    text: str = ""  # optional words for the site's search, with word forms
    details: bool = False  # download every notice's XML: customers, INNs, positions


@dataclass(frozen=True)
class OpenNotice:
    hit: SearchHit
    regions: tuple[str, ...]  # a joint purchase can come up in several regions
    notice: Notice | None = None  # with details only

    @property
    def deadline_day(self) -> date | None:
        """The last day of applications, in the customer's local time."""
        if self.notice and self.notice.applications_end:
            return self.notice.applications_end.date()
        return self.hit.deadline

    def days_left(self, today: date) -> int | None:
        day = self.deadline_day
        return (day - today).days if day else None


@dataclass
class RegionCount:
    listed: int = 0  # "Подача заявок" on the site, published in the last LOOKBACK_DAYS days
    open: int = 0  # of them the deadline has not passed
    truncated: bool = False  # the site's limit of 5 000 results cut the list


@dataclass
class ExportResult:
    query: ExportQuery
    today: date
    since: date  # published from
    notices: list[OpenNotice] = field(default_factory=list)
    regions: dict[str, RegionCount] = field(default_factory=dict)
    listed: int = 0  # distinct notices the search gave
    closed: int = 0  # of them past the deadline, though the site still says "Подача заявок"
    downloads: FetchReport | None = None  # with details
    broken: list[tuple[str, str]] = field(default_factory=list)  # notices whose XML could not be parsed
    error: str = ""  # why the run stopped early; what was collected is still exported


class Stopped(Exception):
    """Raised from the page callback to stop a search halfway."""


def export_open(
    client: Client,
    cache: NoticeCache,
    query: ExportQuery,
    now: datetime,
    *,
    listing: Listing | None = None,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
) -> ExportResult:
    """``now`` is an aware datetime; its date in the computer's time zone is "today"."""
    today = now.date()
    result = ExportResult(query, today, today - timedelta(days=LOOKBACK_DAYS))
    found: dict[str, tuple[SearchHit, list[str]]] = {}
    for region in query.regions:
        search_query = SearchQuery(
            (region,), result.since, today, text=query.text, price_from=query.price_from,
            price_to=query.price_to, stages=(Stage.APPLICATIONS,),
        )

        def on_page(collected: int, total: int, region: str = region) -> None:
            if cancel and cancel():
                raise Stopped
            if listing:
                listing(region, collected, total)

        try:
            searched = search(client, search_query, on_page)
        except Stopped:
            result.error = STOPPED
            break
        except SiteError as error:
            result.error = str(error)
            break
        count = result.regions[region] = RegionCount(len(searched.hits), truncated=searched.truncated)
        for hit in searched.hits:
            if hit.deadline is None or hit.deadline >= today:
                count.open += 1
            found.setdefault(hit.reg_number, (hit, []))[1].append(region)

    result.listed = len(found)
    candidates = [
        OpenNotice(hit, tuple(regions)) for hit, regions in found.values()
        if hit.deadline is None or hit.deadline >= today
    ]
    if query.details and candidates and not result.error:
        candidates = _with_details(client, cache, candidates, now, result, progress, cancel)
    result.closed = result.listed - len(candidates)
    result.notices = sorted(candidates, key=_order)
    return result


def _with_details(
    client: Client,
    cache: NoticeCache,
    candidates: list[OpenNotice],
    now: datetime,
    result: ExportResult,
    progress: Progress | None,
    cancel: Cancel | None,
) -> list[OpenNotice]:
    """The notices with their parsed XML; the exact deadline drops those closed earlier today."""
    report = result.downloads = FetchReport(hits=[item.hit for item in candidates])
    download_notices(client, cache, report, progress, cancel)
    result.error = report.error
    detailed = []
    for item in candidates:
        xml = cache.load(item.hit.reg_number)
        if xml is None:  # not downloaded: stopped, or the site failed to give it
            detailed.append(item)
            continue
        try:
            notice = parse_notice(xml)
        except NoticeFormatError as error:
            result.broken.append((item.hit.reg_number, str(error)))
            detailed.append(item)
            continue
        if notice.applications_end and notice.applications_end < now:
            continue
        detailed.append(OpenNotice(item.hit, item.regions, notice))
    return detailed


def _order(item: OpenNotice) -> tuple[datetime, int]:
    """The nearest deadline first; on the same day the most expensive on top."""
    if item.notice and item.notice.applications_end:
        deadline = item.notice.applications_end
    elif item.hit.deadline:
        deadline = datetime.combine(item.hit.deadline, datetime.max.time(), NO_DEADLINE.tzinfo)
    else:
        deadline = NO_DEADLINE
    return deadline, -int(item.hit.price or 0)
