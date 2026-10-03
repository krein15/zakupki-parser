"""fetch_notices: search, download, cache and the ways a run stops."""

from __future__ import annotations

from datetime import date

from conftest import FakeSite, numbers

from zkparser.cache import NoticeCache
from zkparser.fetch import CACHED, DOWNLOADED, FAILED, MAX_FAILURES_IN_ROW, fetch_notices
from zkparser.website.client import RateLimited
from zkparser.website.notice import NOTICE_XML_PATH
from zkparser.website.search import SearchQuery

DAY = date(2026, 9, 30)
QUERY = SearchQuery(regions=("72000000000",), published_from=DAY, published_to=DAY)


def test_downloads_once_then_serves_from_cache(tmp_path):
    site = FakeSite({DAY: numbers(3)})
    with NoticeCache(tmp_path) as cache:
        first = fetch_notices(site, cache, QUERY)
        second = fetch_notices(site, cache, QUERY)
        stored = cache.load(numbers(3)[0])
    assert (first.found, first.downloaded, first.cached) == (3, 3, 0)
    assert (second.found, second.downloaded, second.cached) == (3, 0, 3)
    assert site.requests_to(NOTICE_XML_PATH) == 3
    assert stored.startswith(b"<?xml")


def test_updated_notice_is_downloaded_again(tmp_path):
    with NoticeCache(tmp_path) as cache:
        fetch_notices(FakeSite({DAY: numbers(2)}), cache, QUERY)
        later = FakeSite({DAY: numbers(2)}, updated="01.10.2026")
        report = fetch_notices(later, cache, QUERY)
    assert report.downloaded == 2


def test_progress_reports_every_notice(tmp_path):
    seen = []
    with NoticeCache(tmp_path) as cache:
        fetch_notices(FakeSite({DAY: numbers(2)}), cache, QUERY)
        fetch_notices(FakeSite({DAY: numbers(3)}), cache, QUERY, progress=lambda *args: seen.append(args))
    assert [(n, total, status) for n, total, _, status in seen] == [(1, 3, CACHED), (2, 3, CACHED), (3, 3, DOWNLOADED)]


def test_notice_without_xml_is_reported_and_the_run_goes_on(tmp_path):
    bad = numbers(3)[1]
    seen = []
    with NoticeCache(tmp_path) as cache:
        report = fetch_notices(
            FakeSite({DAY: numbers(3)}, broken={bad}), cache, QUERY, progress=lambda *args: seen.append(args[3])
        )
    assert report.downloaded == 2
    assert [number for number, _ in report.failed] == [bad]
    assert seen == [DOWNLOADED, FAILED, DOWNLOADED]
    assert not report.error


def test_stops_when_notices_fail_in_a_row(tmp_path):
    site = FakeSite({DAY: numbers(10)}, broken=set(numbers(10)))
    with NoticeCache(tmp_path) as cache:
        report = fetch_notices(site, cache, QUERY)
    assert len(report.failed) == MAX_FAILURES_IN_ROW
    assert "остановлен" in report.error


def test_rate_limit_stops_the_run_and_keeps_what_was_downloaded(tmp_path):
    class LimitedSite(FakeSite):
        def get(self, path, params=None):
            if path == NOTICE_XML_PATH and self.requests_to(NOTICE_XML_PATH) == 2:
                raise RateLimited("Сайт ЕИС ограничил частоту запросов")
            return super().get(path, params)

    with NoticeCache(tmp_path) as cache:
        report = fetch_notices(LimitedSite({DAY: numbers(5)}), cache, QUERY)
        kept = [number for number in numbers(5) if cache.load(number)]
    assert report.downloaded == 2
    assert kept == numbers(2)
    assert "ограничил" in report.error


def test_search_failure_is_reported(tmp_path):
    class DownSite(FakeSite):
        def get(self, path, params=None):
            return b"<html><body>maintenance</body></html>"

    with NoticeCache(tmp_path) as cache:
        report = fetch_notices(DownSite({}), cache, QUERY)
    assert report.found == 0
    assert report.error
