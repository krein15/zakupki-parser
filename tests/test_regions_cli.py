"""Region lookup and the command line's argument handling."""

from __future__ import annotations

from datetime import date

import pytest

from zkparser.__main__ import build_parser, build_query, describe, parse_day, period
from zkparser.regions import REGIONS, resolve_region
from zkparser.website.search import Stage


def test_region_table():
    assert len(REGIONS) == 90
    assert all(len(code) == 11 and code.endswith("000000000") for code in REGIONS)


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("72", "72000000000"),
        ("7", "07000000000"),
        ("72000000000", "72000000000"),
        ("тюмен", "72000000000"),
        ("Москва", "77000000000"),
        ("московская", "50000000000"),
    ],
)
def test_resolve_region(value, code):
    assert resolve_region(value) == code


@pytest.mark.parametrize(("value", "message"), [("область", "нескольким"), ("Атлантида", "Не найден"), ("81", "Нет")])
def test_resolve_region_errors(value, message):
    with pytest.raises(ValueError, match=message):
        resolve_region(value)


@pytest.mark.parametrize("text", ["2026-09-30", "30.09.2026"])
def test_parse_day(text):
    assert parse_day(text) == date(2026, 9, 30)


def args(*argv):
    parser = build_parser()
    return parser.parse_args(["fetch", "-r", "72", *argv]), parser


def test_period_variants():
    today = date.today()
    assert period(*args()) == (today, today)
    assert period(*args("-d", "2026-09-30")) == (date(2026, 9, 30),) * 2
    assert period(*args("--from", "2026-09-01", "--to", "2026-09-30")) == (date(2026, 9, 1), date(2026, 9, 30))
    assert period(*args("--from", "2026-09-01")) == (date(2026, 9, 1), today)
    assert period(*args("--to", "2026-09-30")) == (date(2026, 9, 30),) * 2


@pytest.mark.parametrize(
    "argv", [("-d", "2026-09-30", "--from", "2026-09-01"), ("--from", "2026-09-30", "--to", "2026-09-01")]
)
def test_period_errors(argv):
    with pytest.raises(SystemExit):
        period(*args(*argv))


def test_build_query():
    parser = build_parser()
    argv = ["fetch", "-r", "72", "-r", "тюмен", "-r", "86", "-d", "30.09.2026", "-q", " канцтовары "]
    namespace = parser.parse_args([*argv, "--price-to", "500000"])
    query = build_query(namespace, parser)
    assert query.regions == ("72000000000", "86000000000")
    assert query.text == "канцтовары"
    assert (query.price_from, query.price_to) == (None, 500000)
    assert query.stages == (Stage.APPLICATIONS,)
    assert describe(query) == (
        "Тюменская область, Ханты-Мансийский автономный округ - Югра · 30.09.2026 · «канцтовары» · "
        "цена 0–500000 ₽ · подача заявок"
    )


def test_all_stages_flag():
    query = build_query(*args("--all-stages"))
    assert query.stages == ()
