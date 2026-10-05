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


def plural(count: int, one: str, few: str, many: str) -> str:
    """plural(2, "регион", "региона", "регионов") → "2 региона"."""
    tens, units = count % 100, count % 10
    if units == 1 and tens != 11:
        word = one
    elif 2 <= units <= 4 and not 12 <= tens <= 14:
        word = few
    else:
        word = many
    return f"{count} {word}"


def regions_count(count: int) -> str:
    return plural(count, "регион", "региона", "регионов")


def days_left(days: int) -> str:
    """0 → "последний день", 1 → "остался 1 день", 3 → "осталось 3 дня"."""
    if days <= 0:
        return "последний день"
    text = plural(days, "день", "дня", "дней")
    return f"остался {text}" if text.endswith(" день") else f"осталось {text}"


def short_money(value: int) -> str:
    """1 500 000 → "1,5 млн", 300 000 → "300 тыс.", 950 → "950": a price range in a file name or a title."""
    for size, unit in ((1_000_000_000, "млрд"), (1_000_000, "млн"), (1_000, "тыс.")):
        if value >= size:
            return f"{value / size:.2f}".rstrip("0").rstrip(".").replace(".", ",") + f" {unit}"
    return str(value)


def price_range(price_from: int | None, price_to: int | None, short: bool = False) -> str:
    """"от 1 000 000,00 ₽ до 5 000 000,00 ₽", or "1 млн – 5 млн ₽" when short; empty for any price."""
    if short:
        if price_from is not None and price_to is not None:
            return f"{short_money(price_from)} – {short_money(price_to)} ₽"
        if price_from is not None:
            return f"от {short_money(price_from)} ₽"
        return f"до {short_money(price_to)} ₽" if price_to is not None else ""
    parts = []
    if price_from is not None:
        parts.append(f"от {money(Decimal(price_from))}")
    if price_to is not None:
        parts.append(f"до {money(Decimal(price_to))}")
    return " ".join(parts)


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


def format_match(notice: Notice, reasons: tuple[str, ...], stage: str = "") -> str:
    """A matched notice in a few lines: what, how much, until when, at which stage, for whom and why it matched."""
    customers = notice.customers
    if len(customers) == 1:
        customer = f"{customers[0].customer.name} (ИНН {customers[0].customer.inn or 'не указан'})"
    else:
        customer = f"{len(customers)} заказчиков, совместная закупка"
    deadline = f"заявки до {moment(notice.applications_end)}" if notice.applications_end else "срок подачи не указан"
    status = f" · {stage}" if stage else ""
    return "\n".join(
        [
            f"{notice.reg_number} · {money(notice.max_price, notice.currency)} · {deadline}{status}",
            notice.title,
            f"Заказчик: {customer}",
            f"Почему: {'; '.join(reasons)}",
            notice.url,
        ]
    )


def _position(position: Position, notice: Notice) -> str:
    if notice.quantity_undefined or position.quantity is None:
        return f"{position.name} — {money(position.price, notice.currency)} за ед. ({position.unit})"
    return (
        f"{position.name} — {quantity(position.quantity)} {position.unit} × {money(position.price, notice.currency)}"
        f" = {money(position.total, notice.currency)}"
    )
