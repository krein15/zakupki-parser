from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zkparser.website.notice import NOTICE_XML_PATH
from zkparser.website.search import MAX_PAGES, PAGE_SIZE, SEARCH_PATH

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def search_page() -> bytes:
    """Page 1 of the real results for Tyumen region, 30.09.2026: 50 of 112 notices."""
    return (FIXTURES / "search_page.html").read_bytes()


@pytest.fixture
def search_empty() -> bytes:
    return (FIXTURES / "search_empty.html").read_bytes()


def results_page(
    numbers: list[str], total: int, updated: str = "30.09.2026", stages: dict[str, str] | None = None
) -> bytes:
    """A results page in the site's markup, reduced to what the parser reads."""
    stages = stages or {}
    blocks = "".join(
        '<div class="search-registry-entry-block box-shadow-search-input">'
        '<div class="registry-entry__header-mid__number">'
        f'<a href="/epz/order/notice/ea20/view/common-info.html?regNumber={number}">№ {number}</a></div>'
        f'<div class="registry-entry__header-mid__title">{stages.get(number, "Подача заявок")}</div>'
        f'<div class="data-block__title">Обновлено</div><div class="data-block__value">{updated}</div>'
        "</div>"
        for number in numbers
    )
    return f'<html><body><div class="search-results__total">{total} записей</div>{blocks}</body></html>'.encode()


class FakeSite:
    """Answers like zakupki.gov.ru: search pages over notices by publication day, and notice XML.

    Past the last page (or page 100) it repeats that page, as the real site does.
    """

    def __init__(self, by_day: dict[date, list[str]], *, broken: set[str] = frozenset(), updated: str = "30.09.2026"):
        self.by_day = by_day
        self.broken = broken  # notices whose XML the site fails to give
        self.updated = updated
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict[str, str] | None = None) -> bytes:
        params = params or {}
        self.calls.append((path, params))
        if path == NOTICE_XML_PATH:
            number = params["regNumber"]
            if number in self.broken:
                return b"<html>error</html>"
            return f'<?xml version="1.0"?><epNotificationEF2020 n="{number}"/>'.encode()
        assert path == SEARCH_PATH
        start = _day(params["publishDateFrom"])
        end = _day(params["publishDateTo"])
        numbers = [n for day in sorted(self.by_day, reverse=True) if start <= day <= end for n in self.by_day[day]]
        last_page = max(1, min(MAX_PAGES, -(-len(numbers) // PAGE_SIZE)))
        page = min(int(params["pageNumber"]), last_page)
        return results_page(numbers[(page - 1) * PAGE_SIZE : page * PAGE_SIZE], len(numbers), self.updated)

    def requests_to(self, path: str) -> int:
        return sum(1 for called, _ in self.calls if called == path)


def _day(text: str) -> date:
    day, month, year = map(int, text.split("."))
    return date(year, month, day)


def numbers(count: int, start: int = 0) -> list[str]:
    return [f"{1672000034260000000 + start + i}" for i in range(count)]


def days(first: date, count: int) -> list[date]:
    return [first + timedelta(days=i) for i in range(count)]


NOTICES = {
    "0167100004126000028": "auction_ktru",  # diesel fuel and petrol, applications until 09.10
    "0167100002326000043": "quotation",  # storage services, until 07.10
    "0167200003426008040": "drugs",  # until 08.10 08:00
    "0167200003426008053": "joint",  # tomato paste, until 08.10 08:00
    "1200700110126000001": "audit_contest",  # until 20.10
}


class RegionSite:
    """Search results by region (one page) and notice XML from the fixtures."""

    def __init__(self, by_region: dict[str, list[str]], stages: dict[str, str] | None = None, xml=None):
        self.by_region = by_region
        self.stages = stages or {}
        self.xml = xml or {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == NOTICE_XML_PATH:
            number = params["regNumber"]
            return self.xml.get(number) or (FIXTURES / f"notice_{NOTICES[number]}.xml").read_bytes()
        assert path == SEARCH_PATH
        numbers = self.by_region.get(params["customerPlace"], [])
        return results_page(numbers, len(numbers), stages=self.stages)

    def searches(self) -> list[dict]:
        return [params for path, params in self.calls if path == SEARCH_PATH]
