"""Search: query parameters, parsing of the site's results page, paging and the 5 000 limit."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from conftest import FakeSite, days, numbers, results_page

from zkparser.website.client import SiteError
from zkparser.website.search import SEARCH_PATH, SearchQuery, Stage, parse_price, parse_results, search

TYUMEN, KHMAO = "72000000000", "86000000000"
DAY = date(2026, 9, 30)


def query(**changes) -> SearchQuery:
    values = {"regions": (TYUMEN,), "published_from": DAY, "published_to": DAY} | changes
    return SearchQuery(**values)


def test_params():
    params = query(regions=(TYUMEN, KHMAO), text="канцтовары", price_from=1000, price_to=500000).params(3)
    assert params["customerPlace"] == params["customerPlaceCodes"] == f"{TYUMEN},{KHMAO}"
    assert params["publishDateFrom"] == params["publishDateTo"] == "30.09.2026"
    assert params["searchString"] == "канцтовары"
    assert params["morphology"] == "on"
    assert params["fz44"] == "on"
    assert params["af"] == "on"
    assert (params["priceFromGeneral"], params["priceToGeneral"]) == ("1000", "500000")
    assert (params["recordsPerPage"], params["pageNumber"]) == ("_50", "3")


def test_params_any_stage_and_no_price():
    params = query(stages=()).params()
    assert not {stage.value for stage in Stage} & params.keys()
    assert "priceFromGeneral" not in params
    assert "priceToGeneral" not in params


@pytest.mark.parametrize(
    ("start", "end", "first", "second"),
    [
        (date(2026, 9, 1), date(2026, 9, 10), (1, 5), (6, 10)),
        (date(2026, 9, 1), date(2026, 9, 2), (1, 1), (2, 2)),
        (date(2026, 9, 1), date(2026, 9, 3), (1, 2), (3, 3)),
    ],
)
def test_split(start, end, first, second):
    a, b = query(published_from=start, published_to=end).split()
    assert (a.published_from.day, a.published_to.day) == first
    assert (b.published_from.day, b.published_to.day) == second


def test_parse_real_results_page(search_page):
    hits, total = parse_results(search_page)
    assert total == 112
    assert len(hits) == 50
    assert len({hit.reg_number for hit in hits}) == 50
    first = hits[0]
    assert first.reg_number == "0167200003426008101"
    assert first.url == "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0167200003426008101"
    assert first.placing_way == "Электронный аукцион"
    assert first.stage == "Подача заявок"
    assert first.title == "Поставка оборудования электрического осветительного"
    assert first.organization == "УПРАВЛЕНИЕ ГОСУДАРСТВЕННЫХ ЗАКУПОК ТЮМЕНСКОЙ ОБЛАСТИ"
    assert first.organization_code == "01672000034"
    assert first.price == Decimal("332000.00")
    assert (first.published, first.updated, first.deadline) == (DAY, DAY, date(2026, 10, 9))
    assert all(hit.title and hit.organization and hit.price and hit.updated for hit in hits)


def test_parse_empty_results(search_empty):
    assert parse_results(search_empty) == ([], 0)


def test_unrecognised_page_is_an_error_not_an_empty_result():
    with pytest.raises(SiteError, match="изменил"):
        parse_results("<html><body>Технические работы</body></html>".encode())


def test_entry_without_number_is_an_error():
    page = b'<html><body><div class="search-results__total">1</div><div class="search-registry-entry-block"></div>'
    with pytest.raises(SiteError, match="номера"):
        parse_results(page)


@pytest.mark.parametrize(
    ("text", "price"),
    [("78 001,16 ₽", Decimal("78001.16")), ("1\xa0234\xa0567,00 ₽", Decimal("1234567.00")), ("", None), ("—", None)],
)
def test_parse_price(text, price):
    assert parse_price(text) == price


@pytest.mark.parametrize(("count", "pages"), [(0, 1), (12, 1), (50, 2), (112, 3), (100, 3)])
def test_reads_every_page(count, pages):
    site = FakeSite({DAY: numbers(count)})
    result = search(site, query())
    assert [hit.reg_number for hit in result.hits] == numbers(count)
    assert not result.truncated
    assert site.requests_to(SEARCH_PATH) == pages


def test_notices_shifted_to_the_next_page_are_counted_once():
    class ShiftingSite(FakeSite):
        def get(self, path, params=None):
            page = super().get(path, params)
            if params["pageNumber"] == "2":  # a new notice appeared: the last one of page 1 moved to page 2
                return results_page(numbers(1, start=49) + numbers(10, start=50), 60)
            return page

    result = search(ShiftingSite({DAY: numbers(60)}), query())
    assert [hit.reg_number for hit in result.hits] == numbers(60)


def test_a_day_over_the_limit_is_marked_truncated():
    site = FakeSite({DAY: numbers(6000)})
    result = search(site, query())
    assert len(result.hits) == 5000
    assert result.truncated
    assert site.requests_to(SEARCH_PATH) == 100


def test_a_period_over_the_limit_is_split_until_it_fits():
    first = date(2026, 9, 1)
    site = FakeSite({day: numbers(2600, start=i * 10_000) for i, day in enumerate(days(first, 3))})
    result = search(site, query(published_from=first, published_to=date(2026, 9, 3)))
    assert len(result.hits) == 7800
    assert not result.truncated


def test_page_callback_sees_the_count_grow():
    seen = []
    search(FakeSite({DAY: numbers(112)}), query(), on_page=lambda collected, total: seen.append((collected, total)))
    assert seen == [(50, 112), (100, 112), (112, 112)]
