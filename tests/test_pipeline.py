"""run_profiles: one search per region for all profiles, then matching on the parsed notices."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from conftest import FIXTURES, results_page

from zkparser.cache import NoticeCache
from zkparser.pipeline import run_profiles
from zkparser.profiles import Profile
from zkparser.website.notice import NOTICE_XML_PATH
from zkparser.website.search import SEARCH_PATH

DAY = date(2026, 9, 30)
TYUMEN, KHMAO = "72000000000", "86000000000"
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


def profile(name: str, regions=(TYUMEN,), **rules) -> Profile:
    return Profile(name=name, regions=regions, path=Path(f"{name}.toml"), **rules)


def run(site, profiles, tmp_path):
    with NoticeCache(tmp_path) as cache:
        return run_profiles(site, cache, profiles, DAY, DAY)


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
