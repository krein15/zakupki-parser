"""Notice XML (epNotification* documents of 44-ФЗ, schema 16.x) → Notice.

The site's viewXml and the SOAP archives carry the same document under different root namespaces, so the parser
reads local names only. Where each field lives is described in docs/recon.md.

The customer block has no INN: it is taken from the customer code inside the purchase identification code (ИКЗ),
``[level][INN, 10 digits][KPP, 9 digits]``. A notice without an ИКЗ (e.g. a mandatory audit contest) gets the INN
from the placing organization when that organization is the customer itself.

The XML comes from outside, so it is parsed without DTDs, entity expansion or network access.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from lxml import etree

from .models import CustomerPart, Notice, Organization, Position

PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, remove_comments=True)
ROOT_PREFIX = "epNotification"


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
    notice = next((el for el in root.iter() if isinstance(el.tag, str) and el.tag.startswith(ROOT_PREFIX)), None)
    if notice is None:
        raise NoticeFormatError(f"Это не извещение 44-ФЗ: корневой элемент {root.tag}")

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
    return Notice(
        reg_number=reg_number,
        version=int(_text(notice, "versionNumber") or 1),
        document_type=notice.tag,
        url=_text(common, "href"),
        title=_text(common, "purchaseObjectInfo"),
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
        positions=_positions(info),
        quantity_undefined=any(
            _text(info, f"purchaseObjectsInfo/{kind}/quantityUndefined") == "true"
            for kind in ("notDrugPurchaseObjectsInfo", "drugPurchaseObjectsInfo")
        ),
    )


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
    objects = info.find("purchaseObjectsInfo") if info is not None else None
    if objects is None:
        return ()
    positions = []
    for item in objects.iterfind("notDrugPurchaseObjectsInfo/purchaseObject"):
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
    for item in objects.iterfind("drugPurchaseObjectsInfo/drugPurchaseObjectInfo"):
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
                unit=_text(unit, "name"),
                price=_decimal(item, "pricePerUnit"),
                total=_decimal(item, "positionPrice"),
                is_drug=True,
            )
        )
    return tuple(positions)


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
