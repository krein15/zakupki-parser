"""Matching a notice against a profile: word forms, prefixes, phrases, minus words, codes, filters and reasons."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from zkparser.matching import Matcher
from zkparser.models import CustomerPart, Notice, Organization, Position
from zkparser.profiles import Profile, ProfileError


def notice(title: str, *positions: Position, price: str = "100000", inn: str = "7202161807") -> Notice:
    customer = Organization("Заказчик", inn=inn)
    return Notice(
        reg_number="0167200003426008040",
        version=1,
        document_type="epNotificationEF2020",
        url="",
        title=title,
        placing_way="Электронный аукцион",
        placing_way_code="EAP20",
        etp="",
        published=None,
        applications_start=None,
        applications_end=datetime(2026, 10, 8),
        max_price=Decimal(price) if price else None,
        currency="RUB",
        placer=customer,
        placer_role="CU",
        customers=(CustomerPart(customer),),
        positions=positions,
    )


def position(name: str, okpd2: str = "", ktru: str = "", ktru_name: str = "") -> Position:
    return Position(name=name, okpd2_code=okpd2, ktru_code=ktru, ktru_name=ktru_name)


def matcher(**rules) -> Matcher:
    return Matcher(Profile(name="test", regions=("72000000000",), path=Path("test.toml"), **rules))


def verdict(rules: dict, title: str, *positions: Position, **notice_fields):
    return matcher(**rules).evaluate(notice(title, *positions, **notice_fields))


@pytest.mark.parametrize(
    ("keyword", "title"),
    [
        ("картридж", "Поставка картриджей для принтеров"),
        ("бумага", "Поставка бумаги"),
        ("канцеляр*", "Поставка канцелярских товаров"),
        ("канцеляр* товар*", "Поставка товаров канцелярских"),  # words in any order
        ("бумага офисная", "Поставка бумаги для офисной техники"),
        ('"ручка шариковая"', "Ручка шариковая синяя"),
        ("«ручка шариковая»", "Ручки шариковые"),
        ("ёлка", "Поставка искусственной елки"),
        ("сталь", "Поставка листовой стали"),  # "стали" is also a form of "стать": every reading counts
        ("горюче-смазочные материалы", "Приобретение горюче-смазочных материалов"),
    ],
)
def test_keyword_finds_its_word_forms_in_the_title(keyword, title):
    result = verdict({"keywords": (keyword,)}, title)
    assert result.matched, result.reasons
    assert result.reasons[0].startswith("название: «")


@pytest.mark.parametrize(
    ("keyword", "title"),
    [
        ("бумага", "Поставка картона"),
        ("бумага офисная", "Поставка бумаги для принтера"),  # one of the two words is missing
        ('"ручка шариковая"', "Шариковая ручка"),  # a quoted phrase keeps its order
        ("товар*", "Поставка полутоваров"),  # a prefix starts a word, it is not found inside one
    ],
)
def test_keyword_does_not_match(keyword, title):
    result = verdict({"keywords": (keyword,)}, title)
    assert not result.matched
    assert result.reasons == ("нет ключевых слов и кодов профиля",)


def test_words_of_a_keyword_must_meet_in_one_place():
    result = verdict({"keywords": ("бумага офисная",)}, "Поставка бумаги", position("Стол офисный"))
    assert not result.matched


def test_position_matches_when_the_title_does_not():
    result = verdict(
        {"keywords": ("бумага офисн*",), "okpd2": ("17.12.14",)},
        "поставка бумаги и картона",
        position("Картон гофрированный", okpd2="17.21.13.000"),
        position("Бумага для офисной техники", okpd2="17.12.14.110"),
    )
    assert result.matched
    assert result.positions == (2,)
    assert result.reasons == ("позиция 2 «Бумага для офисной техники»: «бумага офисн*», ОКПД2 17.12.14.110",)


def test_ktru_name_counts_as_position_text():
    result = verdict({"keywords": ("топливо",)}, "Закупка ГСМ", position("ДТ-Л-К5", ktru_name="Топливо дизельное"))
    assert result.positions == (1,)


def test_codes_match_by_prefix():
    result = verdict(
        {"okpd2": ("17.23",), "ktru": ("32.99.12.110-0000",)},
        "Поставка товаров",
        position("Папка", okpd2="17.23.13.193"),
        position("Ручка", okpd2="32.99.12.110", ktru="32.99.12.110-00000001"),
        position("Стул", okpd2="31.01.11.150"),
    )
    assert result.positions == (1, 2)
    assert result.reasons == (
        "позиция 1 «Папка»: ОКПД2 17.23.13.193",
        "позиция 2 «Ручка»: КТРУ 32.99.12.110-00000001",
    )


def test_minus_word_in_the_title_rejects_the_notice():
    result = verdict({"keywords": ("картридж*",), "minus": ("заправк*",)}, "Заправка картриджей")
    assert not result.matched
    assert result.reasons == ("минус-слово «заправк*» в названии",)


def test_minus_word_in_a_position_drops_only_that_position():
    result = verdict(
        {"keywords": ("картридж*",), "minus": ("ремонт",)},
        "Поставка расходных материалов",
        position("Ремонт картриджа"),
        position("Картридж лазерный"),
    )
    assert result.matched
    assert result.positions == (2,)


def test_many_matching_positions_are_summarised():
    positions = [position(f"Бумага {i}") for i in range(8)]
    result = verdict({"keywords": ("бумага",)}, "Закупка", *positions)
    assert result.positions == tuple(range(1, 9))
    assert len(result.reasons) == 6
    assert result.reasons[-1] == "и ещё позиций: 3"


def test_long_position_names_are_shortened():
    name = "Бумага " + "очень длинное описание " * 5
    (reason,) = verdict({"keywords": ("бумага",)}, "Закупка", position(name)).reasons
    assert "…»" in reason
    assert len(reason) < 120


def test_customer_filters():
    assert verdict({"keywords": ("бумага",), "customer_inn": ("7202161807",)}, "Бумага").matched
    rejected = verdict({"keywords": ("бумага",), "customer_inn": ("7200000000",)}, "Бумага")
    assert rejected.reasons == ("заказчик не из списка customer_inn",)
    excluded = verdict({"keywords": ("бумага",), "exclude_customer_inn": ("7202161807",)}, "Бумага")
    assert excluded.reasons == ("заказчик в списке исключений: ИНН 7202161807",)


def test_profile_of_customers_only_takes_all_their_notices():
    result = verdict({"customer_inn": ("7202161807",)}, "Что угодно")
    assert result.matched
    assert result.reasons == ("заказчик ИНН 7202161807",)


@pytest.mark.parametrize(
    ("price", "rules", "reason"),
    [
        ("5000", {"price_from": 10000}, "цена 5 000,00 ₽ меньше 10 000,00 ₽"),
        ("5000000", {"price_to": 3000000}, "цена 5 000 000,00 ₽ больше 3 000 000,00 ₽"),
        ("", {"price_from": 10000}, "цена — меньше 10 000,00 ₽"),
    ],
)
def test_price_range(price, rules, reason):
    result = verdict({"keywords": ("бумага",), **rules}, "Бумага", price=price)
    assert result.reasons == (reason,)


def test_price_inside_the_range_passes():
    assert verdict({"keywords": ("бумага",), "price_from": 10000, "price_to": 3000000}, "Бумага").matched


@pytest.mark.parametrize(("term", "message"), [("ка*", "слишком короткий префикс"), ('""', "пустое"), ("*", "пустое")])
def test_bad_terms_are_reported(term, message):
    with pytest.raises(ProfileError, match=message):
        matcher(keywords=(term,))
