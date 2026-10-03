"""A parsed notice. Money is Decimal; moments keep the offset of the customer's region (+05:00 for Tyumen)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

# Who placed the notice (purchaseResponsibleInfo/responsibleRole). "Организатор совместной закупки" is the role
# of whoever runs a joint purchase under art. 25 of 44-ФЗ.
PLACER_ROLES = {
    "CU": "заказчик",
    "OCU": "заказчик, организатор совместной закупки",
    "RA": "уполномоченный орган",
    "ORA": "уполномоченный орган, организатор совместной закупки",
    "AI": "уполномоченное учреждение",
    "OAI": "уполномоченное учреждение, организатор совместной закупки",
    "OA": "организация с полномочиями заказчика по договору",
    "OOA": "организация с полномочиями заказчика, организатор совместной закупки",
    "CS": "заказчик по ч. 5 ст. 15 44-ФЗ",
    "OCS": "заказчик по ч. 5 ст. 15 44-ФЗ, организатор совместной закупки",
    "CC": "заказчик, не разместивший положение по 223-ФЗ",
    "OCC": "заказчик, не разместивший положение по 223-ФЗ, организатор совместной закупки",
    "AU": "заказчик обязательного аудита",
    "OAU": "заказчик обязательного аудита, организатор совместной закупки",
    "RO": "региональный оператор",
    "CN": "заказчик по ч. 4.1 ст. 15 44-ФЗ",
    "OCN": "заказчик по ч. 4.1 ст. 15 44-ФЗ, организатор совместной закупки",
    "CU5CH26": "заказчик — орган власти по ч. 5 ст. 26 44-ФЗ",
}


@dataclass(frozen=True)
class Organization:
    name: str
    reg_number: str = ""  # number in the EIS register of organizations
    inn: str = ""
    kpp: str = ""


@dataclass(frozen=True)
class CustomerPart:
    """One customer's share of a notice; a joint purchase has several."""

    customer: Organization
    ikz: str = ""  # identification code of the purchase
    max_price: Decimal | None = None
    delivery_places: tuple[str, ...] = ()


@dataclass(frozen=True)
class Position:
    name: str
    okpd2_code: str = ""
    okpd2_name: str = ""
    ktru_code: str = ""
    ktru_name: str = ""
    quantity: Decimal | None = None  # None when the notice says the quantity cannot be set in advance
    unit: str = ""
    price: Decimal | None = None
    total: Decimal | None = None
    is_drug: bool = False


@dataclass(frozen=True)
class Notice:
    reg_number: str
    version: int
    document_type: str  # the XML root: epNotificationEF2020, epNotificationEZK2020…
    url: str
    title: str
    placing_way: str
    placing_way_code: str
    etp: str
    published: datetime | None
    applications_start: datetime | None
    applications_end: datetime | None
    max_price: Decimal | None
    currency: str
    placer: Organization
    placer_role: str
    customers: tuple[CustomerPart, ...]
    positions: tuple[Position, ...]
    # The quantity cannot be set in advance: the maximum price caps the contract and positions carry unit prices,
    # so their totals do not add up to it.
    quantity_undefined: bool = False

    @property
    def placer_role_name(self) -> str:
        return PLACER_ROLES.get(self.placer_role, self.placer_role)

    @property
    def customer_inns(self) -> tuple[str, ...]:
        return _unique(part.customer.inn for part in self.customers)

    @property
    def okpd2_codes(self) -> tuple[str, ...]:
        return _unique(position.okpd2_code for position in self.positions)

    @property
    def ktru_codes(self) -> tuple[str, ...]:
        return _unique(position.ktru_code for position in self.positions)


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))
