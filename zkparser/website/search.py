"""Search of 44-ФЗ notices: the site's extended search, read from its HTML results.

The site also exports the same search as RSS, but the feed holds only the first 200 results and ignores the page
number and the sort order, so it cannot list a busy region's day (Moscow publishes ~840 notices a day).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

import lxml.html

from .client import BASE_URL, SiteError

log = logging.getLogger("zkparser.website")

SEARCH_PATH = "/epz/order/extendedsearch/results.html"
PAGE_SIZE = 50
MAX_PAGES = 100  # past page 100 the site keeps returning page 100
MAX_RESULTS = PAGE_SIZE * MAX_PAGES
HTML_PARSER = lxml.html.HTMLParser(encoding="utf-8")  # the site always serves UTF-8; do not guess from <meta>
ORGANIZATION_TITLES = ("Заказчик", "Организация, осуществляющая размещение")


class Stage(StrEnum):
    """Notice stages; the value is the search filter's parameter name."""

    APPLICATIONS = "af"  # Подача заявок
    COMMISSION = "ca"  # Работа комиссии
    COMPLETED = "pc"  # Закупка завершена
    CANCELLED = "pa"  # Закупка отменена


@dataclass(frozen=True)
class SearchQuery:
    regions: tuple[str, ...]  # 11-digit KLADR codes, see regions.py
    published_from: date
    published_to: date
    text: str = ""  # the site's own search with word forms ("канцтовары" finds "канцтоваров")
    price_from: int | None = None  # initial maximum price, rubles
    price_to: int | None = None
    stages: tuple[Stage, ...] = (Stage.APPLICATIONS,)  # empty — any stage

    def params(self, page: int = 1) -> dict[str, str]:
        regions = ",".join(self.regions)
        params = {
            "fz44": "on",
            "searchString": self.text,
            "morphology": "on",
            "sortBy": "PUBLISH_DATE",
            "sortDirection": "false",
            "recordsPerPage": f"_{PAGE_SIZE}",
            "pageNumber": str(page),
            "publishDateFrom": f"{self.published_from:%d.%m.%Y}",
            "publishDateTo": f"{self.published_to:%d.%m.%Y}",
            "customerPlace": regions,
            "customerPlaceCodes": regions,
        }
        if self.price_from is not None:
            params["priceFromGeneral"] = str(self.price_from)
        if self.price_to is not None:
            params["priceToGeneral"] = str(self.price_to)
        for stage in self.stages:
            params[stage.value] = "on"
        return params

    def split(self) -> tuple[SearchQuery, SearchQuery]:
        """The same query for the first and the second half of the period."""
        middle = self.published_from + timedelta(days=(self.published_to - self.published_from).days // 2)
        return replace(self, published_to=middle), replace(self, published_from=middle + timedelta(days=1))


@dataclass(frozen=True)
class SearchHit:
    """A notice as the search results show it."""

    reg_number: str
    url: str
    placing_way: str = ""
    stage: str = ""
    title: str = ""
    organization: str = ""  # the customer, or the body that places the notice on its behalf
    organization_code: str = ""
    price: Decimal | None = None
    published: date | None = None
    updated: date | None = None
    deadline: date | None = None  # end of the application period


@dataclass
class SearchResult:
    hits: list[SearchHit]
    truncated: bool = False  # the site's limit of 5 000 results cut the list


class Client(Protocol):
    def get(self, path: str, params: dict[str, str] | None = None) -> bytes: ...


PageDone = Callable[[int, int], None]  # notices collected so far, the total the site reports; may raise to stop


def search(client: Client, query: SearchQuery, on_page: PageDone | None = None) -> SearchResult:
    """Every notice matching the query. A period with more than 5 000 is split in halves until each part fits."""
    hits, total = read_page(client, query, 1)
    if total > MAX_RESULTS and query.published_from < query.published_to:
        log.info("ЕИС: %d результатов больше предела %d, период делится пополам", total, MAX_RESULTS)
        parts = [search(client, part, on_page) for part in query.split()]
        merged = {hit.reg_number: hit for part in parts for hit in part.hits}
        return SearchResult(list(merged.values()), truncated=any(part.truncated for part in parts))

    found = {hit.reg_number: hit for hit in hits}
    previous = [hit.reg_number for hit in hits]
    page = 1
    if on_page:
        on_page(len(found), total)
    while len(previous) == PAGE_SIZE:
        if page == MAX_PAGES:
            truncated = total > MAX_RESULTS
            if truncated:
                log.warning("ЕИС отдаёт не больше %d результатов: часть закупок не попала, сузьте поиск", MAX_RESULTS)
            return SearchResult(list(found.values()), truncated=truncated)
        page += 1
        hits, _ = read_page(client, query, page)
        numbers = [hit.reg_number for hit in hits]
        if numbers == previous:  # past the last page the site repeats it
            break
        for hit in hits:
            found.setdefault(hit.reg_number, hit)  # new notices push older ones to the next page
        previous = numbers
        if on_page:
            on_page(len(found), total)
    return SearchResult(list(found.values()))


def read_page(client: Client, query: SearchQuery, page: int) -> tuple[list[SearchHit], int]:
    return parse_results(client.get(SEARCH_PATH, query.params(page)))


def parse_results(page: bytes) -> tuple[list[SearchHit], int]:
    """The notices on a results page and the total the site reports ("более 9 400" counts as 9 400)."""
    tree = lxml.html.document_fromstring(page, parser=HTML_PARSER)
    total = tree.find_class("search-results__total")
    if not total:
        raise SiteError("Не удалось разобрать выдачу поиска ЕИС: возможно, сайт изменил страницу")
    digits = re.sub(r"\D", "", total[0].text_content())
    return [parse_entry(block) for block in tree.find_class("search-registry-entry-block")], int(digits or 0)


def parse_entry(block: lxml.html.HtmlElement) -> SearchHit:
    links = block.xpath('.//*[contains(@class, "registry-entry__header-mid__number")]//a[@href]')
    reg_number = parse_qs(urlsplit(links[0].get("href")).query).get("regNumber", [""])[0] if links else ""
    if not reg_number.isdigit():
        raise SiteError("Не удалось разобрать выдачу поиска ЕИС: у закупки нет номера")

    title = organization = organization_code = ""
    for item in block.find_class("registry-entry__body-block"):
        label = _text(item.find_class("registry-entry__body-title"))
        if label == "Объект закупки":
            title = _text(item.find_class("registry-entry__body-value"))
        elif label in ORGANIZATION_TITLES:
            org_links = item.xpath('.//*[contains(@class, "registry-entry__body-href")]//a')
            if org_links:
                organization = _text(org_links)
                query = parse_qs(urlsplit(org_links[0].get("href", "")).query)
                organization_code = query.get("organizationCode", [""])[0]

    dates = dict(
        zip(
            (_text([el]) for el in block.find_class("data-block__title")),
            (_text([el]) for el in block.find_class("data-block__value")),
            strict=False,
        )
    )
    return SearchHit(
        reg_number=reg_number,
        url=BASE_URL + links[0].get("href"),
        placing_way=_text(block.find_class("registry-entry__header-top__title")).removeprefix("44-ФЗ").strip(),
        stage=_text(block.find_class("registry-entry__header-mid__title")),
        title=title,
        organization=organization,
        organization_code=organization_code,
        price=parse_price(_text(block.find_class("price-block__value"))),
        published=parse_date(dates.get("Размещено", "")),
        updated=parse_date(dates.get("Обновлено", "")),
        deadline=parse_date(dates.get("Окончание подачи заявок", "")),
    )


def parse_price(text: str) -> Decimal | None:
    """"78 001,16 ₽" → 78001.16."""
    number = re.sub(r"[^\d,.]", "", text).replace(",", ".")
    try:
        return Decimal(number) if number else None
    except InvalidOperation:
        return None


def parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return None


def _text(elements: list[lxml.html.HtmlElement]) -> str:
    return " ".join(elements[0].text_content().split()) if elements else ""
