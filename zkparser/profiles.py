"""Profiles: what to look for, one TOML file per niche or client.

A profile names its regions and the matching rules: keywords and minus words (word forms are matched, ``*`` ends
a word prefix, quotes keep a phrase in order), OKPD2/KTRU code prefixes, customer INNs to include or exclude and the
price range. See docs/example-profile.toml; ready-made starters for common niches are in templates/.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .regions import resolve_region

LIST_FIELDS = ("regions", "keywords", "minus", "okpd2", "ktru", "customer_inn", "exclude_customer_inn")
KNOWN_FIELDS = {"name", "price_from", "price_to", "all_stages", "telegram_chat", *LIST_FIELDS}
TELEGRAM_CHAT = re.compile(r"-?\d{3,}|@[A-Za-z]\w{3,}")
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_FIELDS = ("keywords", "minus", "okpd2", "ktru")


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
    telegram_chat: str = ""  # where to send new notices; empty — the default chat from .env
    path: Path | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Template:
    """A starter dictionary of a common niche: words and codes, no regions, prices or customers."""

    name: str
    keywords: tuple[str, ...] = ()
    minus: tuple[str, ...] = ()
    okpd2: tuple[str, ...] = ()
    ktru: tuple[str, ...] = ()


def load_templates(folder: Path = TEMPLATES_DIR) -> list[Template]:
    """The templates shipped with the program. Their OKPD2 classes were checked against real notices of the Tyumen
    region in October 2026; a template is where a niche dictionary starts, not where it ends."""
    templates = []
    for path in sorted(folder.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        templates.append(Template(data["name"], *(tuple(data.get(field, ())) for field in TEMPLATE_FIELDS)))
    return templates


def from_template(template: Template, regions: tuple[str, ...] = ()) -> Profile:
    return Profile(
        name=template.name,
        regions=regions,
        keywords=template.keywords,
        minus=template.minus,
        okpd2=template.okpd2,
        ktru=template.ktru,
    )


def dump_profile(profile: Profile) -> str:
    """The profile as TOML, the way load_profile reads it. A JSON string is a valid TOML basic string."""
    lines = [
        "# Профиль Zakupki Parser: что искать для одной ниши или одного клиента.",
        "# Слова ищутся по словоформам, «*» — начало слова, фраза в кавычках — точный порядок слов.",
        "",
        f"name = {_string(profile.name)}",
        f"regions = {_strings([code[:2] for code in profile.regions])}",
    ]
    for name in ("keywords", "minus", "okpd2", "ktru", "customer_inn", "exclude_customer_inn"):
        values = getattr(profile, name)
        if values or name in ("keywords", "minus"):
            lines.append(f"{name} = {_strings(values)}")
    for name in ("price_from", "price_to"):
        if getattr(profile, name) is not None:
            lines.append(f"{name} = {getattr(profile, name)}")
    lines.append(f"all_stages = {'true' if profile.all_stages else 'false'}")
    if profile.telegram_chat:
        lines.append(f"telegram_chat = {_string(profile.telegram_chat)}")
    return "\n".join(lines) + "\n"


def save_profile(profile: Profile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_profile(profile), encoding="utf-8")


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _strings(values) -> str:
    if not values:
        return "[]"
    if sum(len(value) for value in values) < 80:
        return "[" + ", ".join(_string(value) for value in values) + "]"
    return "[\n" + "".join(f"    {_string(value)},\n" for value in values) + "]"


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

    telegram_chat = str(data.get("telegram_chat", "")).strip()
    if telegram_chat and (isinstance(data["telegram_chat"], bool) or not TELEGRAM_CHAT.fullmatch(telegram_chat)):
        raise ProfileError(f"{path}: «telegram_chat» — номер чата (123456789, -1001234567890) или @канал")

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
        telegram_chat=telegram_chat,
        path=path,
    )
