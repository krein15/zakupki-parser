"""Which notices of a niche to download, decided from the search results before any download.

A niche finds a notice by its codes or by its words. The codes the site checks itself: given the ids of its
classifier, the search lists only the notices with such a code in a position (Tyumen, a month, code 62: 28 of the 29
notices of the cache). The words are in the title the results show. So a niche downloads only the notices the code
search listed or whose title fits, instead of every notice of its regions. The price of it is a notice whose words
stand only in its positions while its codes are other ones: on 12 thousand cached notices of Ural that was about 2% of
the matches of the niche templates, mostly junk ("полиграфические работы" for stationery, "поверка средств измерений"
for drugs).

A profile of customers (customer_inn) cannot be told by the search results, so a run with one downloads everything.
A notice the results show without a title — a purchase through an electronic shop — is judged by its codes only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace

from .matching import Matcher
from .models import CustomerPart, Notice, Organization
from .profiles import Profile
from .website.okpd2 import Okpd2Ids
from .website.search import Client, SearchHit, SearchQuery, search

SEARCH_ONLY = "search"  # Notice.document_type of a notice known only from the search results


def search_only_notice(hit: SearchHit) -> Notice:
    """What the search results tell about a notice: its title, price and the organization that placed it."""
    organization = Organization(name=hit.organization)
    return Notice(
        reg_number=hit.reg_number, version=0, document_type=SEARCH_ONLY, url=hit.url, title=hit.title,
        placing_way=hit.placing_way, placing_way_code="", etp="", published=None, applications_start=None,
        applications_end=None, max_price=hit.price, currency="RUB", placer=organization, placer_role="",
        customers=(CustomerPart(organization),), positions=(),
    )


def can_shortlist(profiles: Iterable[Profile]) -> bool:
    """Whether the search results are enough to tell which notices the profiles may want."""
    profiles = list(profiles)
    return bool(profiles) and all(
        not profile.customer_inn and (profile.keywords or profile.strong or profile.okpd2 or profile.ktru)
        for profile in profiles
    )


def shortlist(
    client: Client,
    ids: Okpd2Ids,
    query: SearchQuery,
    hits: list[SearchHit],
    profiles: list[Profile],
    matchers: list[Matcher] | None = None,
    on_page: Callable[[int, int], None] | None = None,
) -> set[str]:
    """The registry numbers among ``hits`` (the results of ``query``) worth downloading for ``profiles``.

    One more search of the same query, filtered by the profiles' codes, lists the notices with those codes; the rest
    are judged by their titles. Raises SiteError when the site does not answer.
    """
    matchers = matchers or [Matcher(profile) for profile in profiles]
    wanted = {
        hit.reg_number for hit in hits
        if hit.title and any(matcher.evaluate(search_only_notice(hit)).matched for matcher in matchers)
    }
    codes = ids.ids(code for profile in profiles for code in (*profile.okpd2, *profile.ktru))
    if codes:
        coded = search(client, replace(query, okpd2=tuple(codes), okpd2_ids=tuple(codes.values())), on_page)
        listed = {hit.reg_number for hit in hits}
        wanted |= {hit.reg_number for hit in coded.hits if hit.reg_number in listed}
    return wanted
