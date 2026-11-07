"""Learning from "Не моё": what to stop sending, and the change it makes to the profile.

A press of "Не моё" under a card in the owner's chat says the notice should not have come. The bot answers with up to
three ways not to get its kind again: a junk filter its title hits, or a noun of the title that neither the profile's
words nor the notices marked "Беру" have. A tap adds the choice to the profile file, which is edited in place — only
the changed list is rewritten, the comments and the rest stay — and the answer offers to undo it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from .examples import STOP_WORDS
from .filters import FILTERS
from .matching import WORD, Morphology, Token, compile_term, default_morphology, normalize, tokenize
from .profiles import Profile, ProfileError, load_profile, toml_strings

MAX_SUGGESTIONS = 3
MAX_WORD_BYTES = 40  # the word travels in the button's data, and Telegram allows 64 bytes there
MIN_LETTERS = 4
MINUS, FILTER = "m", "f"
# Nouns that say nothing about what is bought: as a minus word each would cut far more than the one kind of junk.
# Found on the cache of the Ural month: "лекарственного препарата для медицинского применения", "запасных частей".
GENERIC = frozenset([
    "применение", "использование", "значение", "средство", "часть", "реализация", "проведение", "осуществление",
    "цель", "рамка", "основа",
])


@dataclass(frozen=True)
class Suggestion:
    kind: str  # MINUS or FILTER
    value: str  # the minus word, or the key of the filter

    @property
    def label(self) -> str:
        if self.kind == FILTER:
            return f"фильтр «{FILTERS[self.value].title}»"
        return f"минус-слово «{self.value}»"


def suggest(profile: Profile, title: str, taken_titles: Iterable[str] = (),
            morphology: Morphology | None = None) -> list[Suggestion]:
    """Filters the title hits, then its nouns the profile's words and the taken notices do not have.

    Nothing is offered for a title with a strong word: it outweighs every minus word there (matching.py), so no
    filter or minus word would keep such a notice away. A filter is not offered when its words hit the profile's own
    keywords or a notice marked "Беру": "Медицина и лаборатории" would switch off a niche of medical devices.
    """
    morphology = morphology or default_morphology()
    tokens = tokenize(title, morphology)
    if any(compile_term(term, morphology, profile).found_in(tokens) for term in profile.strong):
        return []
    taken_texts = [tokenize(other, morphology) for other in taken_titles]
    own_texts = [tokenize(term, morphology) for term in (*profile.keywords, *profile.strong)]
    found = []
    for key, item in FILTERS.items():
        terms = [compile_term(term, morphology, profile) for term in item.minus]
        if (key not in profile.filters and any(term.found_in(tokens) for term in terms)
                and not any(term.found_in(text) for term in terms for text in (*own_texts, *taken_texts))):
            found.append(Suggestion(FILTER, key))
    wanted = [word for term in (*profile.keywords, *profile.strong)
              for word in compile_term(term, morphology, profile).words]
    # A word of a filter already offered needs no button of its own.
    covered = [word for offer in found for term in FILTERS[offer.value].minus
               for word in compile_term(term, morphology, profile).words]
    taken = [token for text in taken_texts for token in text]
    present = {normalize(term) for term in profile.minus}
    for word, _ in WORD.findall(normalize(title)):
        lemma, part = morphology.best(word)
        token = Token(word, morphology.lemmas(word))
        if (part != "NOUN" or len(lemma) < MIN_LETTERS or lemma in STOP_WORDS or lemma in GENERIC or lemma in present
                or len(lemma.encode()) > MAX_WORD_BYTES
                or any(keyword.matches(token) for keyword in [*wanted, *covered])
                or any(not token.lemmas.isdisjoint(other.lemmas) for other in taken)):
            continue
        candidate = Suggestion(MINUS, lemma)
        if candidate not in found:
            found.append(candidate)
    return found[:MAX_SUGGESTIONS]


def apply(path: Path, suggestion: Suggestion, *, undo: bool = False) -> bool:
    """Add the suggestion to the profile file, or take it away; False when there is nothing to change.

    Raises ProfileError when the file cannot be read or would not be a valid profile after the change; then the file
    is left as it was.
    """
    profile = load_profile(path)
    key = "filters" if suggestion.kind == FILTER else "minus"
    values = list(getattr(profile, key))
    if (suggestion.value in values) != undo:
        return False
    if undo:
        values.remove(suggestion.value)
    else:
        values.append(suggestion.value)
    text = path.read_text(encoding="utf-8")
    path.write_text(set_list(text, key, values), encoding="utf-8")
    try:
        load_profile(path)
    except ProfileError:
        path.write_text(text, encoding="utf-8")
        raise
    return True


def merge_learned(form: Profile, loaded: Profile, disk: Profile) -> tuple[Profile, list[str]]:
    """What the window saves: its form, plus what the bot changed in the file since the window read it.

    The window keeps a profile in its form for days; a tap under "Не моё" meanwhile adds a minus word or a filter to
    the file, and "Сохранить" must not quietly take it away. The bot touches only these two lists, so the other fields
    are the form's. Returns the profile to save and the labels of what was kept from the file.
    """
    changes: dict[str, tuple[str, ...]] = {}
    kept: list[str] = []
    for key, kind in (("minus", MINUS), ("filters", FILTER)):
        before, after, mine = getattr(loaded, key), getattr(disk, key), getattr(form, key)
        added = [value for value in after if value not in before and value not in mine]
        removed = {value for value in before if value not in after}
        changes[key] = (*(value for value in mine if value not in removed), *added)
        kept += [Suggestion(kind, value).label for value in added]
        kept += [f"убрано: {Suggestion(kind, value).label}" for value in removed if value in mine]
    return replace(form, **changes), kept


def set_list(text: str, key: str, values: list[str]) -> str:
    """The TOML text with the list ``key`` replaced (or added at the end); everything else as it was."""
    line = f"{key} = {toml_strings(values)}"
    block = re.compile(rf"(?ms)^{re.escape(key)}\s*=\s*\[.*?\]")
    if block.search(text):
        return block.sub(lambda _: line, text, count=1)
    return text.rstrip("\n") + "\n" + line + "\n"
