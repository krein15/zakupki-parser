"""Profiles: what to look for, one TOML file per niche or client.

A profile names its regions and the matching rules: keywords and minus words (word forms are matched, ``*`` ends
a word prefix, quotes keep a phrase in order), OKPD2/KTRU code prefixes, customer INNs to include or exclude and the
price range. See profiles/example.toml.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .regions import resolve_region

LIST_FIELDS = ("regions", "keywords", "minus", "okpd2", "ktru", "customer_inn", "exclude_customer_inn")
KNOWN_FIELDS = {"name", "price_from", "price_to", "all_stages", *LIST_FIELDS}


class ProfileError(ValueError):
    """The profile file is wrong. The message names the file and can be shown to the user as is."""


@dataclass(frozen=True)
class Profile:
    name: str
    regions: tuple[str, ...]  # 11-digit KLADR codes
    keywords: tuple[str, ...] = ()
    minus: tuple[str, ...] = ()
    okpd2: tuple[str, ...] = ()
    ktru: tuple[str, ...] = ()
    customer_inn: tuple[str, ...] = ()
    exclude_customer_inn: tuple[str, ...] = ()
    price_from: int | None = None
    price_to: int | None = None
    all_stages: bool = False
    path: Path | None = field(default=None, compare=False)


def load_profile(path: Path) -> Profile:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ProfileError(f"{path}: не удалось прочитать файл ({error.strerror})") from error
    except tomllib.TOMLDecodeError as error:
        raise ProfileError(f"{path}: ошибка в формате TOML — {error}") from error
    return parse_profile(data, path)


def parse_profile(data: dict, path: Path) -> Profile:
    unknown = sorted(set(data) - KNOWN_FIELDS)
    if unknown:
        known = ", ".join(sorted(KNOWN_FIELDS))
        raise ProfileError(f"{path}: неизвестный параметр «{unknown[0]}». Допустимые: {known}")

    lists = {}
    for name in LIST_FIELDS:
        value = data.get(name, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ProfileError(f"{path}: «{name}» должен быть списком строк, например {name} = [\"…\"]")
        lists[name] = tuple(item.strip() for item in value if item.strip())

    if not lists["regions"]:
        raise ProfileError(f"{path}: укажите хотя бы один регион: regions = [\"72\"]")
    try:
        regions = tuple(dict.fromkeys(resolve_region(value) for value in lists["regions"]))
    except ValueError as error:
        raise ProfileError(f"{path}: {error}") from error

    for name in ("customer_inn", "exclude_customer_inn"):
        for inn in lists[name]:
            if not (inn.isdigit() and len(inn) in (10, 12)):
                raise ProfileError(f"{path}: «{inn}» в {name} — ИНН состоит из 10 или 12 цифр")
    if not (lists["keywords"] or lists["okpd2"] or lists["ktru"] or lists["customer_inn"]):
        raise ProfileError(
            f"{path}: задайте, что искать: keywords, okpd2, ktru или customer_inn — иначе подойдут все закупки"
        )

    prices = {}
    for name in ("price_from", "price_to"):
        value = data.get(name)
        if value is not None and (not isinstance(value, int | float) or isinstance(value, bool) or value < 0):
            raise ProfileError(f"{path}: «{name}» — сумма в рублях, например {name} = 100000")
        prices[name] = int(value) if value is not None else None
    if None not in prices.values() and prices["price_from"] > prices["price_to"]:
        raise ProfileError(f"{path}: price_from больше price_to")

    all_stages = data.get("all_stages", False)
    if not isinstance(all_stages, bool):
        raise ProfileError(f"{path}: «all_stages» — true или false")

    return Profile(
        name=str(data.get("name") or path.stem),
        regions=regions,
        keywords=lists["keywords"],
        minus=lists["minus"],
        okpd2=lists["okpd2"],
        ktru=lists["ktru"],
        customer_inn=lists["customer_inn"],
        exclude_customer_inn=lists["exclude_customer_inn"],
        price_from=prices["price_from"],
        price_to=prices["price_to"],
        all_stages=all_stages,
        path=path,
    )
