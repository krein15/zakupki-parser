"""fetch_notices: search, download, cache and the ways a run stops."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from conftest import FakeSite, numbers

from zkparser.cache import NoticeCache
from zkparser.fetch import CACHED, DOWNLOADED, FAILED, MAX_FAILURES_IN_ROW, fetch_notices
from zkparser.website.client import RateLimited, SiteError
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


def test_notices_that_fail_while_the_site_answers_are_skipped(tmp_path):
    """Huge road contracts break off on the way; the rest of the run must not depend on them."""
    broken = set(numbers(10)[2:7])
    site = FakeSite({DAY: numbers(10)}, broken=broken)
    with NoticeCache(tmp_path) as cache:
        report = fetch_notices(site, cache, QUERY)
        failure = cache.failure(numbers(10)[2])
    assert sorted(number for number, _ in report.failed) == sorted(broken)
    assert report.downloaded == 5
    assert not report.error
    assert failure.attempts == 1


def test_all_failing_from_the_start_but_the_search_answers(tmp_path):
    site = FakeSite({DAY: numbers(10)}, broken=set(numbers(10)))
    with NoticeCache(tmp_path) as cache:
        report = fetch_notices(site, cache, QUERY)
    assert len(report.failed) == 10
    assert not report.error


def test_stops_when_the_site_goes_down_and_leaves_the_rest_untried(tmp_path):
    class GoesDown(FakeSite):
        def get(self, path, params=None):
            if self.requests_to(NOTICE_XML_PATH) >= 2:
                self.calls.append((path, params))
                raise SiteError("Сайт ЕИС не отвечает: SSLError")
            return super().get(path, params)

    with NoticeCache(tmp_path) as cache:
        report = fetch_notices(GoesDown({DAY: numbers(10)}), cache, QUERY)
        tried = [number for number in numbers(10) if cache.failure(number)]
    assert report.downloaded == 2
    assert len(report.failed) == MAX_FAILURES_IN_ROW
    assert len(tried) == MAX_FAILURES_IN_ROW  # notices after the stop are not marked as failed
    assert "не отвечает" in report.error


def test_a_notice_that_failed_in_two_runs_rests_for_a_day(tmp_path):
    bad = numbers(3)[1]
    site = FakeSite({DAY: numbers(3)}, broken={bad})
    with NoticeCache(tmp_path) as cache:
        fetch_notices(site, cache, QUERY)
        fetch_notices(site, cache, QUERY)
        before = site.requests_to(NOTICE_XML_PATH)
        third = fetch_notices(site, cache, QUERY)
        assert site.requests_to(NOTICE_XML_PATH) == before  # nothing new to download, the bad one is resting
        assert third.resting == [bad] and third.failed == []
        assert cache.resting(bad, now=datetime.now() + timedelta(hours=25)) is None  # tried again a day later


def test_a_downloaded_notice_forgets_its_failures(tmp_path):
    with NoticeCache(tmp_path) as cache:
        cache.record_failure(numbers(1)[0], "обрыв")  # once: not resting yet, so it is tried again
        fetch_notices(FakeSite({DAY: numbers(1)}), cache, QUERY)
        assert cache.failure(numbers(1)[0]) is None


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
