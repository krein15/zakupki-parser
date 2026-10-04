"""Profile files: what is accepted and how mistakes are reported."""

from __future__ import annotations

from pathlib import Path

import pytest

from zkparser.profiles import ProfileError, load_profile, parse_profile, save_profile

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "example-profile.toml"
PATH = Path("client.toml")


def test_example_profile_loads():
    profile = load_profile(EXAMPLE)
    assert profile.name == "Канцтовары и бумага — Тюмень"
    assert profile.regions == ("72000000000",)
    assert "канцеляр* товар*" in profile.keywords
    assert '"бумага для офисной техники"' in profile.keywords
    assert "17.12.14" in profile.okpd2
    assert (profile.price_from, profile.price_to) == (10000, 3000000)
    assert not profile.all_stages
    assert profile.path == EXAMPLE


def test_minimal_profile_and_defaults():
    profile = parse_profile({"regions": ["тюмен", "72"], "keywords": [" бумага ", ""]}, PATH)
    assert profile.name == "client"
    assert profile.regions == ("72000000000",)
    assert profile.keywords == ("бумага",)
    assert (profile.minus, profile.okpd2, profile.customer_inn) == ((), (), ())
    assert profile.price_from is None


def test_customers_alone_are_enough():
    profile = parse_profile({"regions": ["72"], "customer_inn": ["7202161807"]}, PATH)
    assert profile.customer_inn == ("7202161807",)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"regions": ["72"], "keyword": ["бумага"]}, "неизвестный параметр «keyword»"),
        ({"regions": ["72"], "keywords": "бумага"}, "должен быть списком"),
        ({"keywords": ["бумага"]}, "хотя бы один регион"),
        ({"regions": ["Атлантида"], "keywords": ["бумага"]}, "Не найден регион"),
        ({"regions": ["72"], "minus": ["ремонт"]}, "задайте, что искать"),
        ({"regions": ["72"], "customer_inn": ["720216180"]}, "10 или 12 цифр"),
        ({"regions": ["72"], "keywords": ["б"], "price_from": 100, "price_to": 10}, "price_from больше price_to"),
        ({"regions": ["72"], "keywords": ["б"], "price_to": "много"}, "сумма в рублях"),
        ({"regions": ["72"], "keywords": ["б"], "all_stages": "да"}, "true или false"),
    ],
)
def test_mistakes_are_named(data, message):
    with pytest.raises(ProfileError, match=message) as error:
        parse_profile(data, PATH)
    assert str(error.value).startswith("client.toml:")


def test_saved_profile_reads_back_the_same(tmp_path):
    profile = parse_profile(
        {
            "name": 'Канцтовары "Тюмень"',
            "regions": ["72", "86"],
            "keywords": ["канцеляр* товар*", '"бумага для офисной техники"', "ёлка\\ель"],
            "minus": ["ремонт*"],
            "okpd2": ["17.23"],
            "customer_inn": ["7202161807"],
            "price_from": 10000,
            "all_stages": True,
            "telegram_chat": "-1001234567890",
        },
        tmp_path / "a.toml",
    )
    path = tmp_path / "saved.toml"
    save_profile(profile, path)
    again = load_profile(path)
    assert again == profile  # the path is not compared
    assert "price_to" not in path.read_text(encoding="utf-8")


def test_broken_toml(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text('regions = ["72"\n', encoding="utf-8")
    with pytest.raises(ProfileError, match="формате TOML"):
        load_profile(path)


def test_missing_file(tmp_path):
    with pytest.raises(ProfileError, match="не удалось прочитать"):
        load_profile(tmp_path / "nope.toml")
