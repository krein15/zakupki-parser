"""A profile from a few notices that fit: the codes of their positions and the words they share.

A newcomer knows a fitting notice when they see one, not the words and codes that describe it. So they give the links
of two to five such notices, and the profile is made of what those have in common:
- the OKPD2 codes of the positions, cut to the group: 62.01.1, not 62.01.11.000 — a niche spreads over the sub-codes;
  and not 62.01 either — that would take in 62.01.2, the resale of licences;
- the nouns, and adjective + noun pairs, of the titles and position names that come up in at least two of the notices
  (with a single notice — its own), leaving out the words every notice has: "поставка", "оказание", "нужды"…
- the regions of the customers, when the profile has none yet.
It is a draft to look at before saving: minus words are not guessed.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from .cache import NoticeCache
from .matching import WORD, Morphology, default_morphology, normalize
from .models import Notice
from .notice_xml import NoticeFormatError, parse_notice
from .profiles import Profile
from .regions import resolve_region
from .website.client import SiteError
from .website.notice import download_notice
from .website.search import Client, SearchHit, SearchQuery, read_page

MAX_KEYWORDS = 8
LENGTH_223 = 11
MIN_LETTERS = 3
NOUN, ADJECTIVES = "NOUN", ("ADJF", "PRTF")
LINK_NUMBER = re.compile(r"regNumber=(\d+)")
# A purchase number: 19 digits (44-ФЗ), 18 (ПП РФ 615) or 11 starting with 3 (223-ФЗ, not a phone number).
BARE_NUMBER = re.compile(r"(?<!\d)(\d{18,19}|3\d{10})(?!\d)")
SINCE = date(2014, 1, 1)  # the search of a 223-ФЗ number reaches back this far
# Words of nearly every notice: they tell nothing about the niche.
STOP_WORDS = frozenset([
    "поставка", "поставить", "оказание", "оказать", "услуга", "выполнение", "выполнить", "работа", "нужда", "нужды",
    "закупка", "закупить", "приобретение", "предоставление", "обеспечение", "право", "заключение", "контракт",
    "договор", "объект", "товар", "количество", "единица", "штука", "прочее", "прочий", "иной", "необходимый",
    "муниципальный", "государственный", "федеральный", "областной", "районный", "городской", "краевой", "окружной",
    "учреждение", "бюджетный", "казённый", "казенный", "автономный", "общеобразовательный", "образовательный",
    "дошкольный", "год", "город", "область", "район", "край", "округ", "республика", "российский", "федерация",
    "министерство", "управление", "служба", "отдел", "администрация", "департамент", "комитет", "заказчик",
    "организация", "филиал", "число", "период", "соответствие", "требование",
])


@dataclass(frozen=True)
class Suggestion:
    keywords: tuple[str, ...]
    okpd2: tuple[str, ...]
    regions: tuple[str, ...]  # 11-digit KLADR codes of the customers
    notes: tuple[str, ...] = ()  # where each word and code comes from: "ОКПД2 62.01.1 «…» — в 2 из 3 примеров"


def parse_numbers(text: str) -> list[str]:
    """Registry numbers from links ("…common-info.html?regNumber=0373…") and bare numbers, each once."""
    return list(dict.fromkeys(LINK_NUMBER.findall(text) + BARE_NUMBER.findall(text)))


def load_examples(client: Client, cache: NoticeCache, numbers: list[str]) -> tuple[list[Notice], list[str]]:
    """The notices, from the cache or the site; and why the others could not be read."""
    notices, problems = [], []
    for number in numbers:
        try:
            xml = cache.load(number)
            if xml is None:
                hit = _find(client, number) if len(number) == LENGTH_223 else SearchHit(number, "")
                xml = download_notice(client, number, hit.guid)
                cache.store(hit, xml)
            notices.append(parse_notice(xml))
        except (SiteError, NoticeFormatError) as error:
            problems.append(f"{number}: {error}")
    return notices, problems


def _find(client: Client, number: str) -> SearchHit:
    """A 223-ФЗ notice is downloaded by its guid, which only its search result gives."""
    query = SearchQuery((), SINCE, date.today(), text=number, stages=(), laws=("223",))
    hits, _ = read_page(client, query, 1)
    hit = next((hit for hit in hits if hit.reg_number == number), None)
    if hit is None or not hit.guid:
        raise SiteError("поиск ЕИС не нашёл такую закупку 223-ФЗ")
    return hit


def to_profile(suggestion: Suggestion, name: str, regions: tuple[str, ...] = ()) -> Profile:
    """The draft as a profile; without regions of its own it takes the customers' ones."""
    return Profile(name=name, regions=regions or suggestion.regions, keywords=suggestion.keywords,
                   okpd2=suggestion.okpd2)


def group_code(code: str) -> str:
    """62.01.11.000 → 62.01.1: the group of the code."""
    head, _, tail = code.partition(".")
    sub, _, rest = tail.partition(".")
    return f"{head}.{sub}.{rest[:1]}" if rest else code


def suggest(notices: list[Notice], morphology: Morphology | None = None) -> Suggestion:
    morphology = morphology or default_morphology()
    needed = 2 if len(notices) > 1 else 1
    in_notices: Counter[str] = Counter()  # how many of the notices have the word or pair (by normal forms)
    seen: Counter[str] = Counter()  # how often it comes up at all: the order among equals
    labels: dict[str, str] = {}  # how to write it: "информационная система" for "информационный система"
    for notice in notices:
        found: list[str] = []
        for text in (notice.title, *(position.name for position in notice.positions)):
            for key, label in _terms(text, morphology):
                found.append(key)
                labels.setdefault(key, label)
        in_notices.update(set(found))
        seen.update(found)
    chosen = [term for term, count in in_notices.items() if count >= needed]
    order = sorted(chosen, key=lambda term: (-in_notices[term], -seen[term], term))
    # Pairs first: "деревянный балка" says more than "балка", and a noun of a chosen pair is left out.
    pairs = [term for term in order if " " in term][:MAX_KEYWORDS]
    nouns = [term for term in order if " " not in term and not any(term in pair.split() for pair in pairs)]
    chosen_keys = (pairs + nouns)[:MAX_KEYWORDS]
    keywords = [labels[key] for key in chosen_keys]
    code_notices: Counter[str] = Counter()
    code_names: dict[str, str] = {}
    for notice in notices:
        groups = {group_code(position.okpd2_code): position.okpd2_name for position in notice.positions
                  if position.okpd2_code}
        code_notices.update(groups.keys())
        for group, name in groups.items():
            code_names.setdefault(group, name)
    codes = sorted(code_notices)
    total = len(notices)
    notes = [f"«{labels[key]}» — {_share(in_notices[key], total)}" for key in chosen_keys]
    notes += [f"ОКПД2 {code} «{_short(code_names[code])}» — {_share(code_notices[code], total)}" for code in codes]
    return Suggestion(tuple(keywords), tuple(codes), _regions(notices), tuple(notes))


def _share(count: int, total: int) -> str:
    return f"в {count} из {total}" if total > 1 else "в примере"


def _short(name: str, limit: int = 60) -> str:
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _terms(text: str, morphology: Morphology) -> list[tuple[str, str]]:
    """The nouns of the text and its adjective + noun pairs: (normal forms, the way to write it)."""
    words = []
    for word, _ in WORD.findall(normalize(text)):
        if word.isdigit():
            continue
        lemma, part = morphology.best(word)
        useful = len(lemma) >= MIN_LETTERS and lemma not in STOP_WORDS
        words.append((word, lemma if useful else "", part))
    terms = [(lemma, lemma) for _, lemma, part in words if lemma and part == NOUN]
    terms += [(f"{first[1]} {second[1]}", morphology.phrase(first[0], second[0]))
              for first, second in pairwise(words)
              if first[1] and second[1] and first[2] in ADJECTIVES and second[2] == NOUN]
    return terms


def _regions(notices: list[Notice]) -> tuple[str, ...]:
    """The customers' regions by the first two digits of their INN — the region where the tax office registered them."""
    regions = []
    for notice in notices:
        for inn in notice.customer_inns:
            try:
                regions.append(resolve_region(inn[:2]))
            except ValueError:
                continue
    return tuple(dict.fromkeys(regions))
