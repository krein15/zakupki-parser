"""One run over several profiles: every region is fetched once, every notice parsed once and checked against each
profile. One download serves any number of niches or clients."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from .cache import NoticeCache
from .fetch import FetchReport, Progress, fetch_notices
from .matching import Matcher, Verdict
from .models import Notice
from .notice_xml import NoticeFormatError, parse_notice
from .profiles import Profile
from .website.search import Client, SearchHit, SearchQuery, Stage

APPLICATIONS_STAGE = "Подача заявок"  # how the search results name Stage.APPLICATIONS
NO_DEADLINE = datetime.max.replace(tzinfo=UTC)  # sorts notices without an application deadline last

RegionDone = Callable[[str, FetchReport], None]


@dataclass(frozen=True)
class Found:
    notice: Notice
    hit: SearchHit
    verdict: Verdict
    regions: tuple[str, ...] = ()  # where the search found it; a joint purchase can span regions


@dataclass
class ProfileResult:
    profile: Profile
    checked: int = 0  # notices of the profile's regions and stages that went through the matcher
    matches: list[Found] = field(default_factory=list)


@dataclass
class RunResult:
    profiles: list[ProfileResult]
    reports: dict[str, FetchReport] = field(default_factory=dict)  # by region code
    broken: list[tuple[str, str]] = field(default_factory=list)  # notices whose XML could not be parsed
    error: str = ""  # why fetching stopped early; matching still ran over what was fetched


def run_profiles(
    client: Client,
    cache: NoticeCache,
    profiles: list[Profile],
    start: date,
    end: date,
    *,
    progress: Progress | None = None,
    region_done: RegionDone | None = None,
) -> RunResult:
    matchers = [Matcher(profile) for profile in profiles]  # a broken keyword surfaces before any download
    result = RunResult([ProfileResult(profile) for profile in profiles])

    found: dict[str, tuple[SearchHit, set[str]]] = {}  # a joint purchase can come up in several regions
    for region, query in _queries(profiles, start, end):
        report = fetch_notices(client, cache, query, progress)
        result.reports[region] = report
        if region_done:
            region_done(region, report)
        for hit in report.hits:
            found.setdefault(hit.reg_number, (hit, set()))[1].add(region)
        if report.error:
            result.error = report.error
            break

    for reg_number, (hit, regions) in found.items():
        xml = cache.load(reg_number)
        if xml is None:  # the download failed and there is no older copy
            continue
        try:
            notice = parse_notice(xml)
        except NoticeFormatError as error:
            result.broken.append((reg_number, str(error)))
            continue
        for profile_result, matcher in zip(result.profiles, matchers, strict=True):
            profile = profile_result.profile
            if regions.isdisjoint(profile.regions):
                continue
            if not profile.all_stages and hit.stage != APPLICATIONS_STAGE:
                continue
            profile_result.checked += 1
            verdict = matcher.evaluate(notice)
            if verdict.matched:
                profile_result.matches.append(Found(notice, hit, verdict, tuple(sorted(regions))))

    for profile_result in result.profiles:
        profile_result.matches.sort(key=lambda item: item.notice.applications_end or NO_DEADLINE)
    return result


def _queries(profiles: list[Profile], start: date, end: date) -> list[tuple[str, SearchQuery]]:
    """One search per region, wide enough for every profile: matching narrows it down afterwards."""
    stages = () if any(profile.all_stages for profile in profiles) else (Stage.APPLICATIONS,)
    floors = [profile.price_from for profile in profiles]
    ceilings = [profile.price_to for profile in profiles]
    price_from = min(floors) if None not in floors else None
    price_to = max(ceilings) if None not in ceilings else None
    regions = dict.fromkeys(code for profile in profiles for code in profile.regions)
    return [
        (region, SearchQuery((region,), start, end, price_from=price_from, price_to=price_to, stages=stages))
        for region in regions
    ]
