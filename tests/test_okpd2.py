"""OKPD2 codes → ids of the site's classifier, and the search filtered by them."""

from __future__ import annotations

import json
from datetime import date

import pytest

from zkparser.website.client import SiteError
from zkparser.website.okpd2 import CLASSIFIER_PATH, Okpd2Ids, normalize
from zkparser.website.search import SearchQuery

# The classifier answers a value with the tree of every node containing it, as on the site (shortened).
TREE = {"children": [{"code": "J", "key": 8873870, "children": [
    {"code": "62", "key": 8873937, "children": [
        {"code": "62.01", "key": 8874706, "children": [
            {"code": "62.01.2", "key": 8876111, "children": None},
            {"code": "62.01.1", "key": 8876110, "children": None},
        ]},
    ]},
]}]}


class Classifier:
    def __init__(self, answer: bytes = json.dumps(TREE).encode()):
        self.answer = answer
        self.asked: list[str] = []

    def get(self, path, params=None):
        assert path == CLASSIFIER_PATH
        self.asked.append(params["value"])
        return self.answer


@pytest.mark.parametrize(
    ("code", "normal"),
    [("62.01.1", "62.01.1"), ("10.", "10"), (" 01.1 ", "01.1"), ("21.20.10.234-00019", "21.20.10.234")],
)
def test_normalize(code, normal):
    assert normalize(code) == normal


def test_codes_are_found_in_the_tree_and_remembered(tmp_path):
    site = Classifier()
    path = tmp_path / "ids.json"
    assert Okpd2Ids(site, path).ids(["62.01", "62.01.1", "62.01"]) == {"62.01": "8874706", "62.01.1": "8876110"}
    assert site.asked == ["62.01", "62.01.1"]
    again = Classifier()
    assert Okpd2Ids(again, path).ids(["62.01.1"]) == {"62.01.1": "8876110"}
    assert again.asked == []


def test_a_code_the_classifier_does_not_know_is_left_out(tmp_path, caplog):
    assert Okpd2Ids(Classifier()).ids(["99.99"]) == {}
    assert "ОКПД2 99.99 нет в справочнике" in caplog.text


def test_a_page_instead_of_json_is_a_site_error():
    with pytest.raises(SiteError, match="не JSON"):
        Okpd2Ids(Classifier(b"<html>maintenance</html>")).ids(["62"])


def test_a_broken_file_of_ids_is_asked_again(tmp_path):
    path = tmp_path / "ids.json"
    path.write_text("not json", encoding="utf-8")
    site = Classifier()
    assert Okpd2Ids(site, path).ids(["62"]) == {"62": "8873937"}
    assert site.asked == ["62"]


def test_search_by_codes_sends_the_ids_with_nested_codes():
    query = SearchQuery(("72000000000",), date(2026, 10, 1), date(2026, 10, 1), okpd2=("62", "63.11.1"),
                        okpd2_ids=("8873937", "8876118"))
    params = query.params()
    assert (params["okpd2Ids"], params["okpd2IdsCodes"], params["okpd2IdsWithNested"]) == (
        "8873937,8876118", "62,63.11.1", "on")
    assert "okpd2Ids" not in SearchQuery(("72000000000",), date(2026, 10, 1), date(2026, 10, 1)).params()
