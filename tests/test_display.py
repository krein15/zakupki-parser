"""Notice as text for the ``show`` command."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from conftest import FIXTURES

from zkparser.display import format_notice, money, quantity, utc_offset
from zkparser.notice_xml import parse_notice


@pytest.mark.parametrize(
    ("value", "currency", "text"),
    [(Decimal("233984.40"), "RUB", "233 984,40 ₽"), (Decimal("5"), "USD", "5,00 USD"), (None, "RUB", "—")],
)
def test_money(value, currency, text):
    assert money(value, currency) == text


@pytest.mark.parametrize(("value", "text"), [("800.00000000000", "800"), ("2.500", "2,5"), ("1560", "1560")])
def test_quantity(value, text):
    assert quantity(Decimal(value)) == text


def test_utc_offset():
    assert utc_offset(datetime(2026, 9, 30, tzinfo=timezone(timedelta(hours=5)))) == "UTC+05:00"
    assert utc_offset(None) == ""


def test_joint_notice_lists_every_customer_and_the_placer():
    text = format_notice(parse_notice((FIXTURES / "notice_joint.xml").read_bytes()))
    assert text.startswith("0167200003426008053 · Электронный аукцион · редакция 1")
    assert "Разместил: УПРАВЛЕНИЕ ГОСУДАРСТВЕННЫХ ЗАКУПОК ТЮМЕНСКОЙ ОБЛАСТИ, ИНН 7202203221" in text
    assert "Заказчики (4):" in text
    assert "ИНН 7224009250 · 136 000,00 ₽" in text
    assert "1. Томатная паста — 2872 Килограмм × 136,00 ₽ = 390 592,00 ₽" in text


def test_customer_placing_its_own_notice_is_not_listed_twice():
    text = format_notice(parse_notice((FIXTURES / "notice_auction_ktru.xml").read_bytes()))
    assert "Разместил" not in text
    assert "ОКПД2 19.20.21.300 · КТРУ 19.20.21.300-00000009" in text


def test_undefined_quantity_shows_unit_prices():
    text = format_notice(parse_notice((FIXTURES / "notice_quotation.xml").read_bytes()))
    assert "Количество заранее не определено" in text
    assert "25,94 ₽ за ед. (Квадратный метр)" in text
