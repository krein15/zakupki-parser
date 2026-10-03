"""Notice XML → Notice, on real notices with the contact person's details replaced (tools/anonymize_notice.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from conftest import FIXTURES

from zkparser.notice_xml import NoticeFormatError, parse_notice, split_customer_code

TYUMEN_TIME = timezone(timedelta(hours=5))


def notice(name: str):
    return parse_notice((FIXTURES / f"notice_{name}.xml").read_bytes())


def test_auction_with_ktru_positions():
    n = notice("auction_ktru")
    assert n.reg_number == "0167100004126000028"
    assert (n.version, n.document_type) == (1, "epNotificationEF2020")
    assert n.title == "Приобретение горюче-смазочных материалов"
    assert (n.placing_way, n.placing_way_code) == ("Электронный аукцион", "EAP20")
    assert n.etp == "РОСЭЛТОРГ (АО«ЕЭТП»)"
    assert n.url == "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0167100004126000028"
    assert n.published == datetime(2026, 9, 29, 16, 18, 15, 242000, tzinfo=TYUMEN_TIME)
    assert n.applications_end == datetime(2026, 10, 9, 10, 0, tzinfo=TYUMEN_TIME)
    assert (n.max_price, n.currency) == (Decimal("2359233.00"), "RUB")
    assert not n.quantity_undefined

    assert len(n.positions) == 4
    fuel = n.positions[0]
    assert fuel.name == "Топливо дизельное (розничная реализация)"
    assert (fuel.ktru_code, fuel.ktru_name) == ("19.20.21.300-00000009", "Топливо дизельное (розничная реализация)")
    assert (fuel.okpd2_code, fuel.okpd2_name) == ("19.20.21.300", "Топливо дизельное")  # taken from inside KTRU
    assert (fuel.quantity, fuel.unit) == (Decimal("800"), "Литр; кубический дециметр")
    assert (fuel.price, fuel.total) == (Decimal("91.58"), Decimal("73264.00"))
    assert sum(p.total for p in n.positions) == n.max_price
    assert n.ktru_codes == ("19.20.21.300-00000009", "19.20.21.100-00000007")


def test_customer_placing_its_own_notice():
    n = notice("auction_ktru")
    assert n.placer_role == "CU"
    assert n.placer_role_name == "заказчик"
    (part,) = n.customers
    assert part.customer == n.placer
    assert part.customer.inn == "7202215241"
    assert part.ikz == "261720221524172030100100210041920244"
    assert part.max_price == n.max_price
    assert part.delivery_places


def test_customer_inn_comes_from_the_ikz_when_an_authorized_body_places_the_notice():
    n = notice("drugs")
    assert n.placer.name == "УПРАВЛЕНИЕ ГОСУДАРСТВЕННЫХ ЗАКУПОК ТЮМЕНСКОЙ ОБЛАСТИ"
    assert (n.placer.inn, n.placer_role) == ("7202203221", "RA")
    (part,) = n.customers
    assert part.customer.name == "ДЕПАРТАМЕНТ ЗДРАВООХРАНЕНИЯ ТЮМЕНСКОЙ ОБЛАСТИ"
    assert (part.customer.inn, part.customer.kpp) == ("7202161807", "720301001")
    assert n.customer_inns == ("7202161807",)


def test_drug_positions():
    (drug,) = notice("drugs").positions
    assert drug.is_drug
    assert drug.name == "ЛЕВОДОПА+ЭНТАКАПОН+КАРБИДОПА"
    assert (drug.okpd2_code, drug.ktru_code) == ("21.20.10.234", "21.20.10.234-00019")
    assert (drug.quantity, drug.unit, drug.price, drug.total) == (
        Decimal("1560"), "шт", Decimal("149.99"), Decimal("233984.40")
    )


def test_quotation_with_undefined_quantity():
    n = notice("quotation")
    assert (n.document_type, n.placing_way_code) == ("epNotificationEZK2020", "ZKP20")
    assert n.quantity_undefined
    (service,) = n.positions
    assert service.quantity is None
    assert service.ktru_code == ""
    assert service.okpd2_code == "52.10.19.900"
    assert service.price == Decimal("25.94")
    assert n.max_price == Decimal("1000000.00")  # the cap of the contract, not quantity × price


def test_joint_purchase_has_a_part_per_customer():
    n = notice("joint")
    assert n.placer_role == "ORA"
    assert len(n.customers) == 4
    assert n.customer_inns == ("7224009250", "7216001666", "7228000177", "7215004008")
    assert sum(part.max_price for part in n.customers) == n.max_price
    assert all(part.ikz and part.delivery_places for part in n.customers)


def test_notice_without_ikz_takes_the_inn_from_the_customer_placing_it():
    n = notice("audit_contest")
    assert (n.document_type, n.placer_role) == ("epNotificationEOK2020", "AU")
    (part,) = n.customers
    assert part.ikz == ""
    assert part.customer.reg_number == n.placer.reg_number
    assert part.customer.inn == "7203514501"


def test_soap_archive_wrapper_and_other_namespaces_are_read_the_same():
    xml = (FIXTURES / "notice_drugs.xml").read_bytes()
    body = xml.split(b"?>", 1)[1]
    wrapped = b'<?xml version="1.0"?><export xmlns="http://zakupki.gov.ru/oos/export/1">' + body + b"</export>"
    assert parse_notice(wrapped) == parse_notice(xml)


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        (b"<not xml", "Повреждённый"),
        (b"<?xml version='1.0'?><contract><number>1</number></contract>", "не извещение"),
        (b"<?xml version='1.0'?><epNotificationEF2020><commonInfo/></epNotificationEF2020>", "нет номера"),
    ],
)
def test_format_errors(xml, message):
    with pytest.raises(NoticeFormatError, match=message):
        parse_notice(xml)


def test_entities_are_not_expanded():
    xml = (
        b'<?xml version="1.0"?><!DOCTYPE n [<!ENTITY a "AAAAAAAAAA"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b"<epNotificationEF2020><commonInfo><purchaseNumber>1</purchaseNumber>"
        b"<purchaseObjectInfo>&b;</purchaseObjectInfo></commonInfo></epNotificationEF2020>"
    )
    assert "AAAA" not in parse_notice(xml).title


@pytest.mark.parametrize(
    ("code", "inn_kpp"),
    [
        ("27202161807720301001", ("7202161807", "720301001")),
        ("", ("", "")),
        ("123", ("", "")),
        ("2720216180772030100X", ("", "")),
    ],
)
def test_split_customer_code(code, inn_kpp):
    assert split_customer_code(code) == inn_kpp
