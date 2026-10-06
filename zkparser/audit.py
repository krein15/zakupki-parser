"""Audit of a keyword set: what a client's words find in a week, how much of it is junk and what they miss.

The client's words (a plain text file, as typed into any tender search) and a reference profile of the niche run over
the same notices: everything the regions published in the period, at any stage. The reference decides what fits:

* "Совпало" — found by both: here the client's words work;
* "Мусор" — found by the client's words only; the word that pulled a notice in shows what to drop or narrow;
* "Пропущено" — found by the reference only; the words and codes that caught it are what the client's set lacks.

The reference is the operator's best dictionary of the niche, so the report is a basis for a talk with the client,
not a verdict: every row carries its reasons, and a wrong call is easy to spot. The client's words are matched the way
the program matches any profile — in the title and in every position, with word forms.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from .cache import NoticeCache
from .fetch import Cancel, Progress
from .matching import QUOTE_CHARS, Matcher, Verdict
from .pipeline import Found, RegionDone, run_profiles
from .profiles import Profile, ProfileError
from .website.search import Client

CLIENT_NAME = "Ваш набор слов"
MINUS_SIGNS = ("-", "−", "–", "—")


@dataclass(frozen=True)
class Keywords:
    keywords: tuple[str, ...]
    minus: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuditRow:
    found: Found
    client: Verdict | None  # why the client's words took it; None — they missed it
    reference: Verdict | None  # why it fits the niche; None — it does not


@dataclass(frozen=True)
class WordStat:
    word: str  # as the client wrote it
    found: int
    relevant: int

    @property
    def junk(self) -> int:
        return self.found - self.relevant


@dataclass
class AuditResult:
    keywords: Keywords
    reference: Profile
    start: date
    end: date
    checked: int = 0
    both: list[AuditRow] = field(default_factory=list)
    junk: list[AuditRow] = field(default_factory=list)
    missed: list[AuditRow] = field(default_factory=list)
    error: str = ""  # why downloading stopped early; the audit covers what was fetched

    @property
    def found(self) -> int:
        """Notices the client's words found."""
        return len(self.both) + len(self.junk)

    @property
    def relevant(self) -> int:
        """Notices that fit the niche."""
        return len(self.both) + len(self.missed)

    @property
    def precision(self) -> float | None:
        return len(self.both) / self.found if self.found else None

    @property
    def recall(self) -> float | None:
        return len(self.both) / self.relevant if self.relevant else None

    def missed_sum(self) -> Decimal:
        return sum((row.found.notice.max_price or Decimal(0) for row in self.missed), Decimal(0))

    def word_stats(self) -> list[WordStat]:
        """Each client word: how many notices it found and how many of them fit; the noisiest first."""
        found, relevant = Counter(), Counter()
        for rows, fits in ((self.both, True), (self.junk, False)):
            for row in rows:
                for label in row.client.matched_by if row.client else ():
                    found[label] += 1
                    relevant[label] += fits
        stats = [WordStat(word, found[_label(word)], relevant[_label(word)]) for word in self.keywords.keywords]
        return sorted(stats, key=lambda stat: (-stat.junk, -stat.found, stat.word))

    def hints(self) -> list[tuple[str, int, Decimal]]:
        """What caught the missed notices: a word or code of the reference, how many notices, their total price."""
        count, total = Counter(), Counter()
        for row in self.missed:
            for label in row.reference.matched_by if row.reference else ():
                count[label] += 1
                total[label] += row.found.notice.max_price or Decimal(0)
        return sorted(((label, count[label], total[label]) for label in count), key=lambda item: (-item[1], -item[2]))


def parse_keywords(text: str, source: str = "файл слов") -> Keywords:
    """Words one per line or separated by commas; "-слово" is a minus word; "#" starts a comment."""
    keywords: list[str] = []
    minus: list[str] = []
    for line in text.splitlines():
        for item in re.split(r"[,;]", line.split("#", 1)[0]):
            item = item.strip()
            target = keywords
            if item.startswith(MINUS_SIGNS):
                item, target = item.lstrip("".join(MINUS_SIGNS)).strip(), minus
            if item and item not in target:
                target.append(item)
    if not keywords:
        raise ProfileError(f"{source}: нет ни одного ключевого слова")
    return Keywords(tuple(keywords), tuple(minus))


def client_profile(keywords: Keywords, reference: Profile) -> Profile:
    """The client's words with the reference's regions, prices and customers: the two differ only in the words."""
    return replace(reference, name=CLIENT_NAME, keywords=keywords.keywords, minus=keywords.minus, okpd2=(), ktru=(),
                   all_stages=True, path=None)


def run_audit(
    client: Client,
    cache: NoticeCache,
    keywords: Keywords,
    reference: Profile,
    start: date,
    end: date,
    *,
    progress: Progress | None = None,
    region_done: RegionDone | None = None,
    cancel: Cancel | None = None,
) -> AuditResult:
    reference = replace(reference, all_stages=True)  # a week back many notices are past the applications already
    theirs = client_profile(keywords, reference)
    Matcher(theirs)  # a broken word surfaces before any download
    run = run_profiles(client, cache, [theirs, reference], start, end, progress=progress, region_done=region_done,
                       cancel=cancel)
    client_result, reference_result = run.profiles
    found = {item.notice.reg_number: item for item in client_result.matches}
    fits = {item.notice.reg_number: item for item in reference_result.matches}
    result = AuditResult(keywords, reference, start, end, checked=reference_result.checked, error=run.error)
    for number, item in found.items():
        if number in fits:
            result.both.append(AuditRow(item, item.verdict, fits[number].verdict))
        else:
            result.junk.append(AuditRow(item, item.verdict, None))
    result.missed = [AuditRow(item, None, item.verdict) for number, item in fits.items() if number not in found]
    for rows in (result.both, result.junk, result.missed):
        rows.sort(key=lambda row: -(row.found.notice.max_price or 0))  # the biggest money first
    return result


def _label(word: str) -> str:
    """How the matcher names a word in its reasons."""
    return f"«{word.strip().strip(QUOTE_CHARS)}»"
