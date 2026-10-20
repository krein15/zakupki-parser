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
        Decimal("1560"), "Штука", Decimal("149.99"), Decimal("233984.40")
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


def test_an_e_shop_purchase_is_named_after_its_positions():
    """ч. 12 ст. 93: no title and no application period; goods straight in purchaseObjectsInfo."""
    notice = parse_notice((FIXTURES / "notice_eshop.xml").read_bytes())
    assert notice.document_type == "epNotificationEZT2020"
    assert notice.title == "Шкаф деревянный для документов"
    assert notice.applications_end is None
    (position,) = notice.positions
    assert (position.name, position.okpd2_code, position.quantity, position.unit) == (
        "Шкаф деревянный для документов", "31.01.12.139", 5, "Штука")
    assert notice.customers[0].customer.inn == "0711027218"


def test_an_e_shop_drug_purchase_keeps_its_drugs_in_the_notification():
    notice = parse_notice((FIXTURES / "notice_eshop_drug.xml").read_bytes())
    (position,) = notice.positions
    assert position.is_drug and position.okpd2_code == "21.20.10.134"
    assert position.unit == "Кубический сантиметр; миллилитр"  # the notice says "см[3*];^мл"
    assert notice.title.startswith("КАЛИЯ ХЛОРИД")


def test_a_title_made_of_many_positions_counts_the_rest():
    from zkparser.models import Position
    from zkparser.notice_xml import _title_from

    positions = tuple(Position(name=name) for name in ("Стол", "Стул", "Стол", "Шкаф", "Тумба", "Полка"))
    assert _title_from(positions) == "Стол; Стул; Шкаф и ещё 2 позиции"


@pytest.mark.parametrize(
    ("code", "name", "unit"),
    [
        ("111", "см[3*];^мл", "Кубический сантиметр; миллилитр"),
        ("796", "Штука (шт)", "Штука"),
        ("9912", "млн. МЕ", "млн. МЕ"),
        ("1", "дм[3*];^л (дм[3*];^л)", "дм³; л"),
    ],
)
def test_a_drug_unit_is_named_in_words(code, name, unit):
    from lxml import etree

    from zkparser.notice_xml import _drug_unit

    element = etree.fromstring(f"<manualUserOKEI><code>{code}</code><name>{name}</name></manualUserOKEI>")
    assert _drug_unit(element) == unit



def test_a_capital_repair_notice_of_pp_615():
    """No positions with codes: the kinds of works and the subject stand in for them."""
    n = notice("615")
    assert (n.law, n.reg_number, n.document_type) == ("615", "206750000012600293", "pprf615NotificationEF")
    assert n.placing_way.startswith("ПП РФ 615 Электронный аукцион")
    assert n.max_price == Decimal("81337583.14")
    assert n.applications_end == datetime(2026, 10, 28, 9, 0, tzinfo=TYUMEN_TIME)
    assert n.customer_inns == ("7204201389",)  # the regional fund of capital repairs
    assert n.etp == "АО «Сбербанк-АСТ»"
    assert len(n.positions) == 3 and not any(position.okpd2_code for position in n.positions)
    assert n.positions[0].name.startswith("Ремонт внутридомовых")


def test_a_223_notice_keeps_its_moments_in_the_customers_zone():
    n = notice("223")
    assert (n.law, n.reg_number, n.document_type) == ("223", "32616488407", "purchaseNotice")
    assert n.title == "Поставка и настил сценического покрытия."
    assert n.placing_way.startswith("223-ФЗ Запрос предварительных предложений")
    assert n.applications_end == datetime(2026, 10, 22, 8, 0, tzinfo=TYUMEN_TIME)  # written as 08:00, МСК+2
    assert (n.max_price, n.currency, n.etp) == (Decimal("650000"), "RUB", "РТС-тендер")
    assert n.customer_inns == ("7207010960",)
    assert [(p.okpd2_code, p.quantity, p.unit) for p in n.positions] == [
        ("22.23.15.000", Decimal("160"), "Квадратный метр"), ("43.33.29.110", Decimal("1"), "Условная единица")]
    assert n.url == "https://zakupki.gov.ru/223/purchase/public/purchase/info/common-info.html?regNumber=32616488407"


def test_a_223_quotation_keeps_its_data_under_a_name_of_its_own():
    n = notice("223_zk")
    assert n.document_type == "purchaseNoticeZKESMBO"
    assert n.max_price == Decimal("222000")  # initialSum, not maxContractPrice
    (position,) = n.positions
    assert position.name.startswith("Оказание услуг по техническому обслуживанию")  # the only item: the lot's subject
    assert position.okpd2_code == "80.20.10.000"
