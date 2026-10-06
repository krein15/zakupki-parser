"""Telegram notifications through the Bot API: a card per notice with "Беру / Не моё" buttons (bot.py listens).

The bot token is a password. It lives in .env, is never printed, and is cut out of every error: the Bot API puts the
token into each URL, so an unhandled requests error would carry it into the log. ``SecretFilter`` does the same for
anything that still reaches logging.
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from .display import money, plural
from .excel import moscow_offset
from .models import Notice
from .pipeline import Found

API = "https://api.telegram.org"
TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
MESSAGE_LIMIT = 4096
REASONS_LIMIT = 300
TITLE_LIMIT = 250
POSITION_NAME = 60
CARD_POSITIONS = 2  # the card names what is bought; the full list is in Excel
MATCHED_LIMIT = 6
SEND_INTERVAL = 1.1  # Telegram asks for no more than one message a second to the same chat
RETRIES = 3
TIMEOUT = 30
POLL_TIMEOUT = 50  # long polling of getUpdates: the bot waits for a button press up to this long per request
TAKE, SKIP = "take", "skip"  # callback data of the buttons, "take:<registry number>"
UNITS = {
    "штука": "шт.", "шт": "шт.", "килограмм": "кг", "литр": "л", "час": "ч", "месяц": "мес.", "год": "год",
    "квадратный метр": "м²", "кубический метр": "м³", "метр": "м", "тонна": "т", "пачка": "пач.", "упаковка": "уп.",
    "комплект": "компл.", "набор": "набор", "рулон": "рул.", "флакон": "фл.", "ампула": "амп.",
    "условная единица": "усл. ед.", "сутки": "сут.", "день": "дн.", "услуга": "усл.",
}


class TelegramError(RuntimeError):
    """Telegram refused or could not be reached. The message never contains the token."""


class SecretFilter(logging.Filter):
    """Replaces secrets in every log record that passes through the handler it is attached to."""

    def __init__(self, *secrets: str) -> None:
        super().__init__()
        self.secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message, *self.secrets)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


def redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return TOKEN.sub("***", text)


class TelegramBot:
    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not TOKEN.fullmatch(token.strip()):
            raise TelegramError("TELEGRAM_BOT_TOKEN в .env не похож на токен бота (вида 123456789:AAE…)")
        self._token = token.strip()
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    def __repr__(self) -> str:
        return "TelegramBot(token=***)"

    def username(self) -> str:
        return self._call("getMe")["username"]

    def chats(self) -> list[tuple[int, str, str]]:
        """Chats that wrote to the bot recently: id, type and a name to tell them apart."""
        found: dict[int, tuple[int, str, str]] = {}
        for update in self._call("getUpdates"):
            message = update.get("message") or update.get("channel_post") or update.get("my_chat_member") or {}
            chat = message.get("chat")
            if chat:
                name = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                found[chat["id"]] = (chat["id"], chat.get("type", ""), name)
        return list(found.values())

    def send_message(self, chat: str, text: str, buttons: dict | None = None) -> int:
        """Send and return the message's id, by which a button press is matched to its notice later."""
        self._wait_turn(chat)
        params: dict[str, Any] = {"chat_id": chat, "text": text[:MESSAGE_LIMIT], "parse_mode": "HTML",
                                  "link_preview_options": {"is_disabled": True}}
        if buttons:
            params["reply_markup"] = buttons
        result = self._call("sendMessage", **params)
        return int(result.get("message_id", 0)) if isinstance(result, dict) else 0

    def updates(self, offset: int | None, timeout: int = POLL_TIMEOUT) -> list[dict]:
        """Button presses and messages to the bot, waiting up to ``timeout`` seconds for the first one."""
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", request_timeout=timeout + 15, **params)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def edit_buttons(self, chat: str, message_id: int, buttons: dict) -> None:
        self._call("editMessageReplyMarkup", chat_id=chat, message_id=message_id, reply_markup=buttons)

    def send_document(self, chat: str, path: Path, caption: str = "") -> None:
        self._wait_turn(chat)
        with path.open("rb") as file:
            self._call("sendDocument", files={"document": (path.name, file)},
                       data={"chat_id": chat, "caption": caption[:1024], "parse_mode": "HTML"})

    def _wait_turn(self, chat: str) -> None:
        last = self._last_sent.get(chat)
        if last is not None:
            delay = last + SEND_INTERVAL - self._clock()
            if delay > 0:
                self._sleep(delay)
        self._last_sent[chat] = self._clock()

    def _call(
        self, method: str, files: dict | None = None, data: dict | None = None, request_timeout: int = TIMEOUT,
        **params: Any,
    ) -> Any:
        url = f"{API}/bot{self._token}/{method}"
        for _ in range(RETRIES):
            try:
                if files:
                    response = self._session.post(url, data=data, files=files, timeout=request_timeout)
                else:
                    response = self._session.post(url, json=params, timeout=request_timeout)
                answer = response.json()
            except (requests.RequestException, ValueError) as error:
                # "from None": the original exception holds the URL, and with it the token
                raise TelegramError(f"Telegram недоступен: {redact(str(error), self._token)}") from None
            if answer.get("ok"):
                return answer["result"]
            if response.status_code == 429:
                self._sleep(int(answer.get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            description = redact(str(answer.get("description", response.status_code)), self._token)
            raise TelegramError(f"Telegram отклонил запрос: {description}{_hint(description)}")
        raise TelegramError("Telegram просит подождать и не принимает сообщения")


def _hint(description: str) -> str:
    if "chat not found" in description or "bot can't initiate" in description:
        return ". Напишите боту любое сообщение из нужного чата и проверьте номер чата: python -m zkparser telegram"
    if "Unauthorized" in description:
        return ". Проверьте TELEGRAM_BOT_TOKEN в .env"
    return ""


def notice_message(profile_name: str, found: Found, now: datetime) -> str:
    """A new notice as a compact HTML card: what, how much, until when, for whom, what is bought, why it matched.

    Everything taken from the notice is escaped. The dot in front is the time left: 🟢 more than two days,
    🟡 one or two, 🔴 the last day.
    """
    notice = found.notice
    days = _days_left(notice, now)
    if notice.applications_end:
        end = notice.applications_end
        deadline = f"до {end:%d.%m %H:%M} {moscow_offset(end)} ({'сегодня' if days == 0 else f'{days} дн.'})"
    else:
        deadline = "срок подачи не указан"
    customers = notice.customers
    if len(customers) == 1:
        customer = customers[0].customer.name
    else:
        customer = f"совместная закупка, {plural(len(customers), 'заказчик', 'заказчика', 'заказчиков')}"
    matched = ", ".join(found.verdict.matched_by[:MATCHED_LIMIT]) or "; ".join(found.verdict.reasons)
    lines = [
        f"{_signal(days)} <b>{_e(_cut(notice.title, TITLE_LIMIT))}</b>",
        f"💰 {_e(_price(notice.max_price, notice.currency))} · ⏳ {_e(deadline)}",
        f"🏛 {_e(customer)}",
    ]
    positions = _positions(notice, found.verdict.positions)
    if positions:
        lines.append("📦 " + "\n   ".join(positions))
    lines += [
        f"🎯 {_e(_cut(matched, REASONS_LIMIT))}",
        f"{_e(notice.placing_way)} · № {_e(notice.reg_number)} · {_e(profile_name)}",
    ]
    return "\n".join(lines)[:MESSAGE_LIMIT]


def feedback_buttons(reg_number: str, url: str, chosen: str = "") -> dict:
    """The buttons under a card; after a press the chosen one is ticked, and the other can still change the mind."""
    take = "✔ Беру" if chosen == TAKE else "✅ Беру"
    skip = "✔ Не моё" if chosen == SKIP else "❌ Не моё"
    row = [{"text": take, "callback_data": f"{TAKE}:{reg_number}"},
           {"text": skip, "callback_data": f"{SKIP}:{reg_number}"}]
    if url:
        row.append({"text": "Открыть в ЕИС", "url": url})
    return {"inline_keyboard": [row]}


def _days_left(notice: Notice, now: datetime) -> int | None:
    """Calendar days to the end of applications, by the customer's own clock."""
    end = notice.applications_end
    return None if end is None else (end.date() - now.astimezone(end.tzinfo).date()).days


def _signal(days: int | None) -> str:
    if days is None:
        return "⚪"
    return "🔴" if days <= 0 else "🟡" if days <= 2 else "🟢"


def _price(value: Decimal | None, currency: str) -> str:
    """672 700,00 ₽ → 672 700 ₽: kopecks only when there are some."""
    text = money(value, currency)
    return text.replace(",00 ", " ") if value is not None else text


def _positions(notice: Notice, matched: tuple[int, ...]) -> list[str]:
    """The first positions, the matching ones first, with quantities; then how many more there are."""
    count = len(notice.positions)
    numbers = [number for number in matched if 1 <= number <= count]
    numbers += [number for number in range(1, count + 1) if number not in numbers]
    lines = []
    for number in numbers[:CARD_POSITIONS]:
        position = notice.positions[number - 1]
        line = _e(_cut(position.name, POSITION_NAME))
        if position.quantity is not None and not notice.quantity_undefined:
            line += f" — {_amount(position.quantity)} {_e(_unit(position.unit))}".rstrip()
        lines.append(line)
    if count > CARD_POSITIONS:
        lines.append("+" + plural(count - CARD_POSITIONS, "позиция", "позиции", "позиций"))
    return lines


def _amount(value: Decimal) -> str:
    """23000.000 → "23 000", 2.500 → "2,5"."""
    return format(value.normalize(), ",f").replace(",", " ").replace(".", ",")


def _unit(name: str) -> str:
    """"Литр; кубический дециметр" → "л", "Штука" → "шт."; an unknown unit stays as the notice names it."""
    first = name.split(";")[0].strip()
    return UNITS.get(first.casefold(), first.casefold())


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _e(text: str) -> str:
    return html.escape(text, quote=True)
