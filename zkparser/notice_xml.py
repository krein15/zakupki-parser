"""Notice XML (epNotification* documents of 44-ФЗ, schema 16.x; 223-ФЗ and ПП РФ 615 notices below) → Notice.

The site's viewXml and the SOAP archives carry the same document under different root namespaces, so the parser
reads local names only. Where each field lives is described in docs/recon.md.

The customer block has no INN: it is taken from the customer code inside the purchase identification code (ИКЗ),
``[level][INN, 10 digits][KPP, 9 digits]``. A notice without an ИКЗ (e.g. a mandatory audit contest) gets the INN
from the placing organization when that organization is the customer itself.

A purchase through an electronic shop (ч. 12 ст. 93, epNotificationEZT2020) has no title, no application period and
keeps its positions straight in purchaseObjectsInfo; even the site shows such a purchase without a name. The title is
made of its position names, so the matching and the reports still see what is bought. The end of its offers is known
only to the platform: neither the XML, nor the search, nor the notice page of the site has it.

A drug names its unit in OKEI's symbol notation ("см[3*];^мл") or with the symbol in brackets ("Штука (шт)"), while
the other positions name the same OKEI codes in words. Drugs get the words too, so the reports show one spelling.

A ПП РФ 615 notice (capital repairs, pprf615Notification*) has no positions with codes: its kinds of works stand in
for them. A 223-ФЗ notice (purchaseNotice*, the data under body/item/<root>Data) keeps its positions in lots, named
by their OKPD2 class only; its moments come without an offset, the customer's time zone is given apart as МСК+N.

The XML comes from outside, so it is parsed without DTDs, entity expansion or network access.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from lxml import etree

from .display import plural
from .models import CustomerPart, Notice, Organization, Position

PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, remove_comments=True)
ROOT_PREFIX = "epNotification"
ROOT_615, ROOT_223 = "pprf615Notification", "purchaseNotice"
MOSCOW_HOURS = 3
NOTICE_223_URL = "https://zakupki.gov.ru/223/purchase/public/purchase/info/common-info.html?regNumber={}"
TITLE_POSITIONS = 3  # a notice without a title is named after its first positions
# The OKEI codes of drug units as the other positions name them (12 thousand notices of Ural, October 2026).
OKEI_NAMES = {
    "111": "Кубический сантиметр; миллилитр", "113": "Кубический метр", "161": "Миллиграмм", "163": "Грамм",
    "164": "Микрограмм", "639": "Доза", "796": "Штука", "876": "Условная единица",
    "9910": "Международная единица биологической активности", "9920": "Единица действия биологической активности",
}
SYMBOL = re.compile(r"\s*\([^()]*\)$")  # "Штука (шт)"


class NoticeFormatError(ValueError):
    """The XML is not a notice this parser understands. The message can be shown to the user as is."""


def parse_notice(xml: bytes) -> Notice:
    try:
        root = etree.fromstring(xml, PARSER)
    except etree.XMLSyntaxError as error:
        raise NoticeFormatError(f"Повреждённый XML извещения: {error}") from error
    for element in root.iter():
        if isinstance(element.tag, str):
            element.tag = etree.QName(element).localname
    roots = (ROOT_PREFIX, ROOT_615, ROOT_223)
    notice = next((el for el in root.iter() if isinstance(el.tag, str) and el.tag.startswith(roots)), None)
    if notice is None:
        raise NoticeFormatError(f"Это не извещение 44-ФЗ, 223-ФЗ или ПП РФ 615: корневой элемент {root.tag}")
    if notice.tag.startswith(ROOT_615):
        return _parse_615(notice)
    if notice.tag.startswith(ROOT_223):
        return _parse_223(notice)

    common = notice.find("commonInfo")
    reg_number = _text(common, "purchaseNumber")
    if not reg_number:
        raise NoticeFormatError(f"В извещении {notice.tag} нет номера закупки")

    info = notice.find("notificationInfo")
    responsible = notice.find("purchaseResponsibleInfo")
    org = responsible.find("responsibleOrgInfo") if responsible is not None else None
    placer = Organization(
        name=_text(org, "fullName"),
        reg_number=_text(org, "regNum"),
        inn=_text(org, "INN"),
        kpp=_text(org, "KPP"),
    )
    positions = _positions(info)
    return Notice(
        reg_number=reg_number,
        version=int(_text(notice, "versionNumber") or 1),
        document_type=notice.tag,
        url=_text(common, "href"),
        title=_text(common, "purchaseObjectInfo") or _title_from(positions),
        placing_way=_text(common, "placingWay/name"),
        placing_way_code=_text(common, "placingWay/code"),
        etp=_text(common, "ETP/name"),
        published=_moment(common, "publishDTInEIS"),
        applications_start=_moment(info, "procedureInfo/collectingInfo/startDT"),
        applications_end=_moment(info, "procedureInfo/collectingInfo/endDT"),
        max_price=_decimal(info, "contractConditionsInfo/maxPriceInfo/maxPrice"),
        currency=_text(info, "contractConditionsInfo/maxPriceInfo/currency/code"),
        placer=placer,
        placer_role=_text(responsible, "responsibleRole"),
        customers=tuple(
            _customer_part(part, placer)
            for part in _findall(info, "customerRequirementsInfo/customerRequirementInfo")
        ),
        positions=positions,
        quantity_undefined=any(
            _text(info, path) == "true"
            for path in ("purchaseObjectsInfo/notDrugPurchaseObjectsInfo/quantityUndefined",
                         "purchaseObjectsInfo/drugPurchaseObjectsInfo/quantityUndefined",
                         "purchaseObjectsInfo/quantityUndefined", "drugPurchaseObjectsInfo/quantityUndefined")
        ),
    )


def _parse_615(notice: etree._Element) -> Notice:
    """A capital repair of apartment buildings: the regional fund is the customer, the kinds of works are positions."""
    common = notice.find("commonInfo")
    reg_number = _text(common, "purchaseNumber")
    if not reg_number:
        raise NoticeFormatError(f"В извещении {notice.tag} нет номера закупки")
    info = notice.find("notificationInfo")
    responsible = notice.find("purchaseResponsibleInfo")
    org = responsible.find("responsibleOrgInfo") if responsible is not None else None
    fund = Organization(name=_text(org, "fullName"), reg_number=_text(org, "regNum"), inn=_text(org, "INN"),
                        kpp=_text(org, "KPP"))
    condition = info.find("contractCondition") if info is not None else None
    places = tuple(dict.fromkeys(
        ", ".join(part for part in (_text(place, "kladr/fullName"), _text(place, "deliveryPlace")) if part)
        for place in _findall(condition, "kladrPlacesInfo/kladrPlace")
    ))
    works = [Position(name=_text(work, "name")) for work in _findall(
        info, "purchaseObjectsInfo/servicesWorksKindCh1St166Info/serviceWorkKindCh1St166Info")]
    subject = _text(info, "purchaseSubjectInfo/name")
    positions = tuple([*works, Position(name=subject)] if subject else works)
    max_price = _decimal(condition, "maxPriceInfo/maxPrice")
    return Notice(
        reg_number=reg_number,
        version=int(_text(notice, "versionNumber") or 1),
        document_type=notice.tag,
        url=_text(common, "href"),
        title=_text(common, "purchaseObjectInfo") or _title_from(positions),
        placing_way=f"ПП РФ 615 {_text(notice, 'placingWayInfo/name')}".strip(),
        placing_way_code=_text(notice, "placingWayInfo/code"),
        etp=_text(info, "ETPInfo/name"),
        published=_moment(common, "docPublishDate"),
        applications_start=None,
        applications_end=_moment(info, "procedureInfo/collectingEndDate"),
        max_price=max_price,
        currency="RUB",
        placer=fund,
        placer_role=_text(responsible, "publisherRole"),
        customers=(CustomerPart(fund, max_price=max_price, delivery_places=places),),
        positions=positions,
        law="615",
    )


def _parse_223(notice: etree._Element) -> Notice:
    """A 223-ФЗ purchase: lots with their prices and positions; moments in the customer's zone, МСК+N."""
    item = notice.find("body/item")
    data = next((child for child in item if isinstance(child.tag, str) and child.tag.endswith("Data")), None) \
        if item is not None else None
    reg_number = _text(data, "registrationNumber")
    if data is None or not reg_number:
        raise NoticeFormatError(f"В извещении {notice.tag} нет номера закупки")
    offset = _text(data, "customer/mainInfo/timeZone/offset")
    zone = timezone(timedelta(hours=MOSCOW_HOURS + (int(offset) if offset.lstrip("-").isdigit() else 0)))
    customer = _organization_223(data.find("customer/mainInfo"))
    placer = _organization_223(data.find("placer/mainInfo")) if data.find("placer/mainInfo") is not None else customer
    lots = _findall(data, "lots/lot")
    prices = [_decimal(lot, "lotData/maxContractPrice") or _decimal(lot, "lotData/initialSum") for lot in lots]
    known = [price for price in prices if price is not None]
    max_price = sum(known, Decimal(0)) if known else None
    places = tuple(dict.fromkeys(
        _text(lot, "lotData/deliveryPlace/address") for lot in lots if _text(lot, "lotData/deliveryPlace/address")))
    positions = []
    for lot in lots:
        items = _findall(lot, "lotData/lotItems/lotItem")
        for position in items:
            # Items have no names of their own: a lot of one item is named by its subject, others by their class.
            name = _text(lot, "lotData/subject") if len(items) == 1 else ""
            unit_price = _decimal(position, "commodityItemPrice")
            positions.append(Position(
                name=name or _text(position, "okpd2/name") or _text(lot, "lotData/subject"),
                okpd2_code=_text(position, "okpd2/code"),
                okpd2_name=_text(position, "okpd2/name"),
                quantity=_decimal(position, "qty"),
                unit=_text(position, "okei/name"),
                price=unit_price or None,
            ))
    return Notice(
        reg_number=reg_number,
        version=int(_text(data, "version") or 1),
        document_type=notice.tag,
        url=NOTICE_223_URL.format(reg_number),
        title=_text(data, "name") or _title_from(tuple(positions)),
        placing_way=f"223-ФЗ {_text(data, 'purchaseCodeName')}".strip(),
        placing_way_code=_text(data, "purchaseMethodCode"),
        etp=_text(data, "electronicPlaceInfo/name"),
        published=_local_moment(data, "publicationDateTime", zone),
        applications_start=_local_moment(data, "applSubmisionStartDate", zone),
        applications_end=_local_moment(data, "submissionCloseDateTime", zone),
        max_price=max_price,
        currency=_text(lots[0], "lotData/currency/code") if lots else "RUB",
        placer=placer,
        placer_role="",
        customers=(CustomerPart(customer, max_price=max_price, delivery_places=places),),
        positions=tuple(positions),
        law="223",
    )


def _organization_223(info: etree._Element | None) -> Organization:
    return Organization(name=_text(info, "fullName"), inn=_text(info, "inn"), kpp=_text(info, "kpp"))


def _local_moment(element: etree._Element | None, path: str, zone: timezone) -> datetime | None:
    """A moment written without an offset, in the given zone."""
    moment = _moment(element, path)
    return moment.replace(tzinfo=zone) if moment and moment.tzinfo is None else moment


def _title_from(positions: tuple[Position, ...]) -> str:
    """"Шкаф для документов; Стол письменный; Стул и ещё 2 позиции"."""
    names = list(dict.fromkeys(position.name for position in positions if position.name))
    title = "; ".join(names[:TITLE_POSITIONS])
    if len(names) > TITLE_POSITIONS:
        title += " и ещё " + plural(len(names) - TITLE_POSITIONS, "позиция", "позиции", "позиций")
    return title


def split_customer_code(code: str) -> tuple[str, str]:
    """INN and KPP from the 20-digit customer code of an ИКЗ: ``2 7202161807 720301001``."""
    if len(code) == 20 and code.isdigit():
        return code[1:11], code[11:]
    return "", ""


def _customer_part(part: etree._Element, placer: Organization) -> CustomerPart:
    customer = part.find("customer")
    reg_number = _text(customer, "regNum")
    conditions = part.find("contractConditionsInfo")
    inn, kpp = split_customer_code(_text(conditions, "IKZInfo/customerCode"))
    if not inn and reg_number and reg_number == placer.reg_number:
        inn, kpp = placer.inn, placer.kpp
    places = []
    for place in _findall(conditions, "deliveryPlacesInfo/byGARInfo"):
        address = _text(place, "GARInfo/GARAddress") or _text(place, "deliveryPlace")
        if address and address not in places:
            places.append(address)
    return CustomerPart(
        customer=Organization(name=_text(customer, "fullName"), reg_number=reg_number, inn=inn, kpp=kpp),
        ikz=_text(conditions, "IKZInfo/purchaseCode"),
        max_price=_decimal(conditions, "maxPriceInfo/maxPrice"),
        delivery_places=tuple(places),
    )


def _positions(info: etree._Element | None) -> tuple[Position, ...]:
    if info is None:
        return ()
    objects = info.find("purchaseObjectsInfo")
    positions = []
    # An auction or a quotation nests its goods and services in notDrugPurchaseObjectsInfo and its drugs in
    # purchaseObjectsInfo; an e-shop purchase keeps goods straight in purchaseObjectsInfo and drugs in notificationInfo.
    goods = [*_findall(objects, "notDrugPurchaseObjectsInfo/purchaseObject"), *_findall(objects, "purchaseObject")]
    drugs = [*_findall(objects, "drugPurchaseObjectsInfo/drugPurchaseObjectInfo"),
             *_findall(info, "drugPurchaseObjectsInfo/drugPurchaseObjectInfo")]
    for item in goods:
        ktru = item.find("KTRU")
        okpd2 = item.find("OKPD2")
        if okpd2 is None and ktru is not None:
            okpd2 = ktru.find("OKPD2")
        positions.append(
            Position(
                name=_text(item, "name"),
                okpd2_code=_text(okpd2, "OKPDCode"),
                okpd2_name=_text(okpd2, "OKPDName"),
                ktru_code=_text(ktru, "code"),
                ktru_name=_text(ktru, "name"),
                quantity=_decimal(item, "quantity/value"),
                unit=_text(item, "OKEI/name"),
                price=_decimal(item, "price"),
                total=_decimal(item, "sum"),
            )
        )
    for item in drugs:
        # A drug is described through a reference (or interchangeable variants); its codes sit deep inside.
        okpd2 = item.find(".//OKPD2")
        ktru = item.find(".//KTRU")
        unit = item.find(".//manualUserOKEI")
        positions.append(
            Position(
                name=_text(item, "name"),
                okpd2_code=_text(okpd2, "OKPDCode"),
                okpd2_name=_text(okpd2, "OKPDName"),
                ktru_code=_text(ktru, "code"),
                ktru_name=_text(ktru, "name"),
                quantity=_decimal(item, "drugQuantityCustomersInfo/total"),
                unit=_drug_unit(unit),
                price=_decimal(item, "pricePerUnit"),
                total=_decimal(item, "positionPrice"),
                is_drug=True,
            )
        )
    return tuple(positions)


def _drug_unit(unit: etree._Element | None) -> str:
    """"см[3*];^мл" (OKEI 111) → "Кубический сантиметр; миллилитр"; an unknown code only loses the notation."""
    name = OKEI_NAMES.get(_text(unit, "code"))
    if name:
        return name
    name = SYMBOL.sub("", _text(unit, "name"))
    return name.replace("[3*]", "³").replace("[2*]", "²").replace(";^", "; ")


def _findall(element: etree._Element | None, path: str) -> list[etree._Element]:
    return element.findall(path) if element is not None else []


def _text(element: etree._Element | None, path: str) -> str:
    if element is None:
        return ""
    return " ".join((element.findtext(path) or "").split())


def _decimal(element: etree._Element | None, path: str) -> Decimal | None:
    try:
        return Decimal(_text(element, path)) if _text(element, path) else None
    except InvalidOperation:
        return None


def _moment(element: etree._Element | None, path: str) -> datetime | None:
    try:
        return datetime.fromisoformat(_text(element, path)) if _text(element, path) else None
    except ValueError:
        return None
