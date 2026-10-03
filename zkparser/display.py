"""A notice as readable text, for the ``show`` command."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .models import Notice, Position

MAX_POSITIONS = 20
CURRENCY_SIGNS = {"RUB": "₽"}


def money(value: Decimal | None, currency: str = "RUB") -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " " + CURRENCY_SIGNS.get(currency, currency)


def quantity(value: Decimal) -> str:
    """800.00000000000 → 800, 2.500 → 2,5."""
    return format(value.normalize(), "f").replace(".", ",")


def moment(value: datetime | None) -> str:
    return f"{value:%d.%m.%Y %H:%M}" if value else "—"


def utc_offset(value: datetime | None) -> str:
    offset = value.strftime("%z") if value else ""
    return f"UTC{offset[:3]}:{offset[3:]}" if offset else ""


def format_notice(notice: Notice) -> str:
    lines = [
        f"{notice.reg_number} · {notice.placing_way} · редакция {notice.version}",
        notice.title,
        f"Начальная цена: {money(notice.max_price, notice.currency)}",
        f"Подача заявок: {moment(notice.applications_start)} – {moment(notice.applications_end)}"
        f" ({utc_offset(notice.applications_end)})",
        f"Площадка: {notice.etp}",
    ]
    customer_numbers = {part.customer.reg_number for part in notice.customers}
    if notice.placer.reg_number not in customer_numbers:
        lines.append(f"Разместил: {notice.placer.name}, ИНН {notice.placer.inn} ({notice.placer_role_name})")

    joint = len(notice.customers) > 1
    lines.append(f"Заказчики ({len(notice.customers)}):" if joint else "Заказчик:")
    for part in notice.customers:
        share = f" · {money(part.max_price, notice.currency)}" if joint else ""
        lines.append(f"  {part.customer.name}, ИНН {part.customer.inn or 'не указан'}{share}")
        lines.extend(f"    Место поставки: {place}" for place in part.delivery_places)

    lines.append(f"Позиции ({len(notice.positions)}):")
    if notice.quantity_undefined:
        lines.append("  Количество заранее не определено: цены за единицу, начальная цена — предел контракта.")
    for number, position in enumerate(notice.positions[:MAX_POSITIONS], 1):
        lines.append(f"  {number}. {_position(position, notice)}")
        labelled = (("ОКПД2", position.okpd2_code), ("КТРУ", position.ktru_code))
        codes = [f"{label} {code}" for label, code in labelled if code]
        if codes:
            lines.append("     " + " · ".join(codes))
    if len(notice.positions) > MAX_POSITIONS:
        lines.append(f"  … и ещё {len(notice.positions) - MAX_POSITIONS}")
    lines.append(notice.url)
    return "\n".join(lines)


def _position(position: Position, notice: Notice) -> str:
    if notice.quantity_undefined or position.quantity is None:
        return f"{position.name} — {money(position.price, notice.currency)} за ед. ({position.unit})"
    return (
        f"{position.name} — {quantity(position.quantity)} {position.unit} × {money(position.price, notice.currency)}"
        f" = {money(position.total, notice.currency)}"
    )
