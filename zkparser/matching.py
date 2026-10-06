"""Matching notices against a profile, with the reason for every decision.

Keywords and minus words are matched by word forms (pymorphy3): "картридж" finds "картриджей", and an ambiguous
word counts under each of its readings, so nothing is lost to a wrong guess. A word ending in "*" is a prefix
("канцтовар*" finds "канцтоварный"). Several words find a text that has all of them in any order; a phrase in
quotes ("ручка шариковая") must stand as written.

The title and every position (its name and KTRU name) are checked separately. A minus word in the title rejects the
notice; a minus word in a position drops only that position. OKPD2 and KTRU codes match by prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

import pymorphy3

from .display import money
from .models import Notice, Position
from .profiles import Profile, ProfileError

WORD = re.compile(r"(\w+)(\*?)")
QUOTES = ('"', "«»", "“”")
QUOTE_CHARS = "\"«»“”"
MIN_PREFIX = 3
MAX_POSITION_REASONS = 5
MAX_NAME = 60


def normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")


class Morphology:
    def __init__(self) -> None:
        self._analyzer = pymorphy3.MorphAnalyzer()
        self._lemmas: dict[str, frozenset[str]] = {}

    def lemmas(self, word: str) -> frozenset[str]:
        """Every normal form the word may have, plus the word itself."""
        found = self._lemmas.get(word)
        if found is None:
            forms = {normalize(parse.normal_form) for parse in self._analyzer.parse(word)}
            found = self._lemmas[word] = frozenset(forms | {word})
        return found


@cache
def default_morphology() -> Morphology:
    return Morphology()  # the dictionaries take a quarter of a second to load: once per process


@dataclass(frozen=True)
class Token:
    text: str
    lemmas: frozenset[str]


@dataclass(frozen=True)
class Word:
    text: str
    prefix: bool
    lemmas: frozenset[str]

    def matches(self, token: Token) -> bool:
        if self.prefix:
            return token.text.startswith(self.text) or any(lemma.startswith(self.text) for lemma in token.lemmas)
        return not self.lemmas.isdisjoint(token.lemmas)


@dataclass(frozen=True)
class Term:
    source: str  # as written in the profile
    words: tuple[Word, ...]
    ordered: bool

    def found_in(self, tokens: list[Token]) -> bool:
        if self.ordered:
            size = len(self.words)
            return any(
                all(word.matches(token) for word, token in zip(self.words, tokens[start : start + size], strict=True))
                for start in range(len(tokens) - size + 1)
            )
        return all(any(word.matches(token) for token in tokens) for word in self.words)


def tokenize(text: str, morphology: Morphology) -> list[Token]:
    return [Token(word, morphology.lemmas(word)) for word, _ in WORD.findall(normalize(text))]


def compile_term(source: str, morphology: Morphology, profile: Profile) -> Term:
    text = source.strip()
    ordered = len(text) > 1 and any(text[0] == pair[0] and text[-1] == pair[-1] for pair in QUOTES)
    if ordered:
        text = text[1:-1]
    words = []
    for word, star in WORD.findall(normalize(text)):
        if star and len(word) < MIN_PREFIX:
            raise ProfileError(f"{profile.path}: слишком короткий префикс «{word}*» — нужно хотя бы {MIN_PREFIX} буквы")
        words.append(Word(word, bool(star), frozenset() if star else morphology.lemmas(word)))
    if not words:
        raise ProfileError(f"{profile.path}: пустое ключевое или минус-слово «{source}»")
    return Term(source.strip(), tuple(words), ordered)


@dataclass(frozen=True)
class Verdict:
    matched: bool
    reasons: tuple[str, ...]  # why it matched; for a rejected notice, why it was rejected
    positions: tuple[int, ...] = ()  # numbers (from 1) of the positions that matched
    matched_by: tuple[str, ...] = ()  # the words and codes that matched, each once: «бумага офисн*», ОКПД2 17.12.14


class Matcher:
    def __init__(self, profile: Profile, morphology: Morphology | None = None) -> None:
        self.profile = profile
        self._morphology = morphology or default_morphology()
        self._keywords = [compile_term(term, self._morphology, profile) for term in profile.keywords]
        self._minus = [compile_term(term, self._morphology, profile) for term in profile.minus]

    def evaluate(self, notice: Notice) -> Verdict:
        profile = self.profile
        rejection = self._filters(notice)
        if rejection:
            return Verdict(False, (rejection,))

        title = tokenize(notice.title, self._morphology)
        minus = next((term for term in self._minus if term.found_in(title)), None)
        if minus:
            return Verdict(False, (f"минус-слово «{minus.source}» в названии",))

        if not (self._keywords or profile.okpd2 or profile.ktru):  # a profile of customers only
            inns = ", ".join(inn for inn in notice.customer_inns if inn in profile.customer_inn)
            return Verdict(True, (f"заказчик ИНН {inns}",))

        reasons = []
        title_terms = [term for term in self._keywords if term.found_in(title)]
        matched_by = [_quoted([term]) for term in title_terms]
        if title_terms:
            reasons.append(f"название: {_quoted(title_terms)}")
        positions = []
        for number, position in enumerate(notice.positions, 1):
            why = self._position_reasons(position)
            matched_by += [item for item in why if item not in matched_by]
            if why:
                positions.append(number)
                if len(positions) <= MAX_POSITION_REASONS:
                    reasons.append(f"позиция {number} «{_short(position.name)}»: {', '.join(why)}")
        if len(positions) > MAX_POSITION_REASONS:
            reasons.append(f"и ещё позиций: {len(positions) - MAX_POSITION_REASONS}")
        if not reasons:
            return Verdict(False, ("нет ключевых слов и кодов профиля",))
        return Verdict(True, tuple(reasons), tuple(positions), tuple(matched_by))

    def _filters(self, notice: Notice) -> str:
        """Why the notice fails the profile's hard filters, or an empty string."""
        profile = self.profile
        inns = set(notice.customer_inns)
        if profile.customer_inn and inns.isdisjoint(profile.customer_inn):
            return "заказчик не из списка customer_inn"
        excluded = sorted(inns.intersection(profile.exclude_customer_inn))
        if excluded:
            return f"заказчик в списке исключений: ИНН {', '.join(excluded)}"
        price = notice.max_price
        if profile.price_from is not None and (price is None or price < profile.price_from):
            return f"цена {money(price, notice.currency)} меньше {money(profile.price_from)}"
        if profile.price_to is not None and price is not None and price > profile.price_to:
            return f"цена {money(price, notice.currency)} больше {money(profile.price_to)}"
        return ""

    def _position_reasons(self, position: Position) -> list[str]:
        tokens = tokenize(f"{position.name} {position.ktru_name}", self._morphology)
        if any(term.found_in(tokens) for term in self._minus):
            return []
        why = [_quoted([term]) for term in self._keywords if term.found_in(tokens)]
        if position.okpd2_code and any(position.okpd2_code.startswith(code) for code in self.profile.okpd2):
            why.append(f"ОКПД2 {position.okpd2_code}")
        if position.ktru_code and any(position.ktru_code.startswith(code) for code in self.profile.ktru):
            why.append(f"КТРУ {position.ktru_code}")
        return why


def _quoted(terms: list[Term]) -> str:
    return ", ".join(f"«{term.source.strip(QUOTE_CHARS)}»" for term in terms)


def _short(name: str) -> str:
    return name if len(name) <= MAX_NAME else name[: MAX_NAME - 1].rstrip() + "…"
