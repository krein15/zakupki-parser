"""run_profiles: one search per region for all profiles, then matching on the parsed notices."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from conftest import NOTICES, RegionSite

from zkparser.cache import NoticeCache
from zkparser.pipeline import APPLICATIONS_STAGE, CLOSED_STAGE, actual_stage, run_profiles
from zkparser.profiles import Profile
from zkparser.telegram import notice_message
from zkparser.website.client import SiteError
from zkparser.website.search import SEARCH_PATH, SearchHit

DAY = date(2026, 9, 30)
BEFORE_DEADLINES = datetime(2026, 10, 1, tzinfo=UTC)  # every fixture still takes applications: the order is by date
TYUMEN, KHMAO = "72000000000", "86000000000"


def profile(name: str, regions=(TYUMEN,), **rules) -> Profile:
    return Profile(name=name, regions=regions, path=Path(f"{name}.toml"), **rules)


def run(site, profiles, tmp_path):
    with NoticeCache(tmp_path) as cache:
        return run_profiles(site, cache, profiles, DAY, DAY, now=BEFORE_DEADLINES)


def test_one_search_serves_every_profile(tmp_path):
    site = RegionSite({TYUMEN: list(NOTICES)})
    fuel = profile("ГСМ", keywords=("топливо дизельное",))
    food = profile("Продукты", keywords=("томатн*",))
    result = run(site, [fuel, food], tmp_path)

    assert len(site.searches()) == 1
    assert [found.notice.reg_number for found in result.profiles[0].matches] == ["0167100004126000028"]
    assert [found.notice.reg_number for found in result.profiles[1].matches] == ["0167200003426008053"]
    assert [p.checked for p in result.profiles] == [5, 5]
    assert result.reports[TYUMEN].found == 5
    assert not result.error


def test_profile_sees_only_its_regions(tmp_path):
    site = RegionSite({TYUMEN: list(NOTICES), KHMAO: []})
    tyumen = profile("Тюмень", keywords=("топливо",))
    khmao = profile("Югра", regions=(KHMAO,), keywords=("топливо",))
    result = run(site, [tyumen, khmao], tmp_path)
    assert [params["customerPlace"] for params in site.searches()] == [TYUMEN, KHMAO]
    assert len(result.profiles[0].matches) == 1
    assert (result.profiles[1].checked, result.profiles[1].matches) == (0, [])


def test_profiles_without_all_stages_skip_later_stages(tmp_path):
    site = RegionSite({TYUMEN: ["0167100002326000043"]}, stages={"0167100002326000043": "Работа комиссии"})
    any_stage = profile("Все этапы", keywords=("хранение",), all_stages=True)
    open_only = profile("Подача заявок", keywords=("хранение",))
    result = run(site, [any_stage, open_only], tmp_path)
    assert "af" not in site.searches()[0]  # one wide search for both
    assert len(result.profiles[0].matches) == 1
    assert (result.profiles[1].checked, result.profiles[1].matches) == (0, [])


def test_search_covers_the_widest_price_range(tmp_path):
    site = RegionSite({TYUMEN: []})
    profiles = [profile("a", keywords=("x",), price_from=50000), profile("b", keywords=("x",), price_from=10000)]
    run(site, profiles, tmp_path)
    params = site.searches()[0]
    assert params["priceFromGeneral"] == "10000"
    assert "priceToGeneral" not in params


def test_matches_are_sorted_by_application_deadline(tmp_path):
    site = RegionSite({TYUMEN: list(NOTICES)})
    everything = profile("Всё", okpd2=("10", "19", "21", "52", "69"))
    (result,) = run(site, [everything], tmp_path).profiles
    numbers = [found.notice.reg_number for found in result.matches]
    assert numbers[0] == "0167100002326000043"
    assert numbers[-1] == "1200700110126000001"
    assert len(numbers) == 5


def test_unreadable_notice_is_reported_and_skipped(tmp_path):
    bad = "0167100004126000028"
    site = RegionSite({TYUMEN: [bad]}, xml={bad: b"<?xml version='1.0'?><contract/>"})
    result = run(site, [profile("ГСМ", keywords=("топливо",))], tmp_path)
    assert [number for number, _ in result.broken] == [bad]
    assert result.profiles[0].matches == []


def test_closed_notices_go_after_open_ones(tmp_path):
    site = RegionSite({TYUMEN: list(NOTICES)})
    everything = profile("Всё", okpd2=("10", "19", "21", "52", "69"))
    later = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)  # quotation, drugs and joint have closed by then
    with NoticeCache(tmp_path) as cache:
        (result,) = run_profiles(site, cache, [everything], DAY, DAY, now=later).profiles
    numbers = [found.notice.reg_number for found in result.matches]
    assert numbers == ["0167100004126000028", "1200700110126000001", "0167100002326000043", "0167200003426008040",
                       "0167200003426008053"]
    assert [found.stage(later) for found in result.matches] == [APPLICATIONS_STAGE] * 2 + [CLOSED_STAGE] * 3


@pytest.mark.parametrize(
    ("stage", "deadline", "exact", "expected"),
    [
        ("Подача заявок", date(2026, 10, 7), None, CLOSED_STAGE),
        ("Подача заявок", date(2026, 10, 8), None, "Подача заявок"),  # the last day is still open
        ("Подача заявок", None, None, "Подача заявок"),
        ("Подача заявок", date(2026, 10, 8), datetime(2026, 10, 8, 8, 0, tzinfo=UTC), CLOSED_STAGE),
        ("Подача заявок", date(2026, 10, 8), datetime(2026, 10, 8, 18, 0, tzinfo=UTC), "Подача заявок"),
        ("Работа комиссии", date(2026, 10, 1), None, "Работа комиссии"),
    ],
)
def test_actual_stage(stage, deadline, exact, expected):
    hit = SearchHit("1", "", stage=stage, deadline=deadline)
    assert actual_stage(hit, datetime(2026, 10, 8, 12, 0, tzinfo=UTC), exact) == expected



class UndownloadableSite(RegionSite):
    """The search shows the notice with its title, but its XML never comes through (a huge road contract)."""

    NUMBER = "0818500000826004035"

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == SEARCH_PATH:
            block = (
                '<div class="search-registry-entry-block"><div class="registry-entry__header-mid__number">'
                f'<a href="/epz/order/notice/ea20/view/common-info.html?regNumber={self.NUMBER}">№</a></div>'
                '<div class="registry-entry__header-mid__title">Подача заявок</div>'
                '<div class="registry-entry__body-block"><div class="registry-entry__body-title">Объект закупки</div>'
                '<div class="registry-entry__body-value">Поставка дизельного топлива</div></div>'
                '<div class="price-block__value">5 000 000,00 ₽</div>'
                '<div class="data-block__title">Окончание подачи заявок</div>'
                '<div class="data-block__value">20.10.2026</div></div>'
            )
            return f'<div class="search-results__total">1</div>{block}'.encode()
        raise SiteError("Сайт ЕИС не отвечает: обрыв")


def test_a_notice_that_never_downloads_is_checked_by_its_search_title(tmp_path):
    with NoticeCache(tmp_path) as cache:
        result = run_profiles(UndownloadableSite({}), cache, [profile("ГСМ", keywords=("топливо",))], DAY, DAY,
                              now=BEFORE_DEADLINES)
    (found,) = result.profiles[0].matches
    assert found.search_only
    assert (found.notice.title, found.notice.max_price, found.notice.positions) == (
        "Поставка дизельного топлива", 5000000, ())
    assert found.verdict.reasons == ("название: «топливо»",)
    card = notice_message("ГСМ", found, datetime(2026, 10, 13, 12, 0, tzinfo=UTC))
    assert "⏳ до 20.10 (7 дн.)" in card
    assert "⚠️ Извещение не скачалось с сайта: проверено только название" in card
