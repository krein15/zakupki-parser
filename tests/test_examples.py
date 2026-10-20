"""A profile from example notices: numbers from links, codes cut to the group, shared words, the customers' regions."""

from __future__ import annotations

from conftest import FIXTURES

from zkparser.cache import NoticeCache
from zkparser.examples import group_code, load_examples, parse_numbers, suggest, to_profile
from zkparser.matching import Matcher, default_morphology
from zkparser.notice_xml import parse_notice
from zkparser.website.client import SiteError
from zkparser.website.notice import NOTICE_XML_PATH

TYUMEN = "72000000000"


def fixture(name: str):
    return parse_notice((FIXTURES / f"notice_{name}.xml").read_bytes())


def test_numbers_come_from_links_and_bare_numbers_once_each():
    text = ("https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0373100024326000060\n"
            "0162200025226000014, 0373100024326000060; телефон 83452123456")
    assert parse_numbers(text) == ["0373100024326000060", "0162200025226000014"]


def test_codes_are_cut_to_the_group():
    assert [group_code(code) for code in ("62.01.11.000", "10.39.17.112", "62.01.1", "62.01")] == [
        "62.01.1", "10.39.1", "62.01.1", "62.01"]


def test_pairs_are_written_the_way_people_write_them():
    morphology = default_morphology()
    assert morphology.phrase("информационной", "системы") == "информационная система"
    assert morphology.phrase("деревянные", "балки") == "деревянная балка"


def test_one_example_gives_its_own_words_codes_and_region():
    suggestion = suggest([fixture("joint")])  # tomato paste for four customers of Tyumen region
    assert suggestion.keywords[0] == "томатная паста"
    assert "поставка" not in suggestion.keywords
    assert suggestion.okpd2 == ("10.39.1",)
    assert suggestion.regions == (TYUMEN,)
    assert suggestion.notes[0] == "«томатная паста» — в примере"


def test_several_examples_keep_the_shared_words_and_every_code():
    suggestion = suggest([fixture("it_support"), fixture("licence")])
    assert suggestion.keywords == ("сопровождение",)  # the only word both have
    assert suggestion.okpd2 == ("58.29.1", "62.02.3", "62.03.1")
    assert any(note.startswith("ОКПД2 58.29.1 «") and note.endswith("— в 1 из 2") for note in suggestion.notes)


def test_the_draft_finds_its_own_examples():
    notices = [fixture("joint"), fixture("auction_ktru")]
    profile = to_profile(suggest(notices), "По примерам")
    assert profile.regions == (TYUMEN,)
    assert all(Matcher(profile).evaluate(notice).matched for notice in notices)
    assert to_profile(suggest(notices), "x", ("86000000000",)).regions == ("86000000000",)


class ExampleSite:
    def __init__(self):
        self.asked: list[str] = []

    def get(self, path, params=None):
        assert path == NOTICE_XML_PATH
        self.asked.append(params["regNumber"])
        if params["regNumber"] == "0000000000000000001":
            raise SiteError("ЕИС не отдал XML извещения")
        return (FIXTURES / "notice_joint.xml").read_bytes()


def test_examples_are_downloaded_once_and_failures_named(tmp_path):
    site = ExampleSite()
    with NoticeCache(tmp_path) as cache:
        notices, problems = load_examples(site, cache, ["0167200003426008053", "0000000000000000001"])
        again, _ = load_examples(site, cache, ["0167200003426008053"])
    assert [notice.reg_number for notice in notices + again] == ["0167200003426008053"] * 2
    assert problems == ["0000000000000000001: ЕИС не отдал XML извещения"]
    assert site.asked == ["0167200003426008053", "0000000000000000001"]
