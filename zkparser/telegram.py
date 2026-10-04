"""Telegram notifications through the Bot API.

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
from pathlib import Path
from typing import Any

import requests

from .display import moment, money
from .excel import moscow_offset
from .pipeline import Found

API = "https://api.telegram.org"
TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
MESSAGE_LIMIT = 4096
REASONS_LIMIT = 1000
SEND_INTERVAL = 1.1  # Telegram asks for no more than one message a second to the same chat
RETRIES = 3
TIMEOUT = 30


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

    def send_message(self, chat: str, text: str) -> None:
        self._wait_turn(chat)
        self._call("sendMessage", chat_id=chat, text=text[:MESSAGE_LIMIT], parse_mode="HTML",
                   link_preview_options={"is_disabled": True})

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

    def _call(self, method: str, files: dict | None = None, data: dict | None = None, **params: Any) -> Any:
        url = f"{API}/bot{self._token}/{method}"
        for _ in range(RETRIES):
            try:
                if files:
                    response = self._session.post(url, data=data, files=files, timeout=TIMEOUT)
                else:
                    response = self._session.post(url, json=params, timeout=TIMEOUT)
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


def notice_message(profile_name: str, found: Found) -> str:
    """One new notice as an HTML message. Everything taken from the notice is escaped."""
    notice = found.notice
    customers = notice.customers
    if len(customers) == 1:
        customer = customers[0].customer.name
    else:
        customer = f"совместная закупка, заказчиков: {len(customers)}"
    deadline = (
        f"заявки до {moment(notice.applications_end)} ({moscow_offset(notice.applications_end)})"
        if notice.applications_end
        else "срок подачи не указан"
    )
    reasons = "; ".join(found.verdict.reasons)
    if len(reasons) > REASONS_LIMIT:
        reasons = reasons[: REASONS_LIMIT - 1] + "…"
    lines = [
        f"<b>{_e(profile_name)}</b> · новая закупка",
        f'<a href="{_e(notice.url)}">{_e(notice.title)}</a>',
        f"{_e(money(notice.max_price, notice.currency))} · {_e(deadline)}",
        f"Заказчик: {_e(customer)}",
        f"Почему: {_e(reasons)}",
        f"№ {_e(notice.reg_number)} · {_e(notice.placing_way)}",
    ]
    return "\n".join(lines)[:MESSAGE_LIMIT]


def _e(text: str) -> str:
    return html.escape(text, quote=True)
