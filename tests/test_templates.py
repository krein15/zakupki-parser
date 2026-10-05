"""Niche templates: every one is a valid profile once regions are added, and each finds its own kind of notice."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FIXTURES

from zkparser.matching import Matcher
from zkparser.notice_xml import parse_notice
from zkparser.profiles import from_template, load_templates, parse_profile

TEMPLATES = {template.name: template for template in load_templates()}
CODE = re.compile(r"\d{2}(\.\d*)*")


def test_templates_are_there():
    assert len(TEMPLATES) == 12
    assert "Канцтовары и бумага" in TEMPLATES
    for template in TEMPLATES.values():
        assert template.keywords, template.name
        assert template.okpd2, template.name
        assert all(CODE.fullmatch(code) for code in template.okpd2), template.name


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_template_is_a_valid_profile(name):
    template = TEMPLATES[name]
    data = {
        "name": template.name,
        "regions": ["72"],
        "keywords": list(template.keywords),
        "minus": list(template.minus),
        "okpd2": list(template.okpd2),
        "ktru": list(template.ktru),
    }
    profile = parse_profile(data, Path("template.toml"))
    Matcher(profile)  # every keyword and minus word compiles


def test_from_template_keeps_the_regions():
    profile = from_template(TEMPLATES["Мебель"], ("72000000000", "86000000000"))
    assert profile.name == "Мебель"
    assert profile.regions == ("72000000000", "86000000000")
    assert profile.okpd2 == TEMPLATES["Мебель"].okpd2


FIXTURE_NICHES = {
    "auction_ktru": "Топливо и ГСМ",  # diesel fuel and petrol
    "drugs": "Лекарственные препараты",
    "joint": "Продукты питания",  # tomato paste
    "it_support": "IT-услуги и разработка ПО",  # support of the "Парус-Бюджет" software
}


@pytest.mark.parametrize(("fixture", "niche"), list(FIXTURE_NICHES.items()))
def test_each_notice_is_found_by_its_template_only(fixture, niche):
    notice = parse_notice((FIXTURES / f"notice_{fixture}.xml").read_bytes())
    matched = {name for name, template in TEMPLATES.items()
               if Matcher(from_template(template, ("72000000000",))).evaluate(notice).matched}
    assert matched == {niche}


def test_storage_services_fit_no_template():
    notice = parse_notice((FIXTURES / "notice_quotation.xml").read_bytes())
    matched = [name for name, template in TEMPLATES.items()
               if Matcher(from_template(template, ("72000000000",))).evaluate(notice).matched]
    assert matched == []


def test_licence_resale_is_not_an_it_service():
    """A licence renewal carries an IT service code (62.03); only the minus words keep it out of the IT niche."""
    notice = parse_notice((FIXTURES / "notice_licence.xml").read_bytes())
    profile = from_template(TEMPLATES["IT-услуги и разработка ПО"], ("72000000000",))
    assert Matcher(replace(profile, minus=())).evaluate(notice).matched
    verdict = Matcher(profile).evaluate(notice)
    assert not verdict.matched
    assert verdict.reasons[0].startswith("минус-слово")
