"""Telegram: requests to the Bot API, limits, and a token that leaks neither into errors nor into logs."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date

import pytest
import requests
from conftest import FIXTURES

from zkparser.matching import Verdict
from zkparser.notice_xml import parse_notice
from zkparser.pipeline import Found
from zkparser.telegram import MESSAGE_LIMIT, SEND_INTERVAL, SecretFilter, TelegramBot, TelegramError, notice_message
from zkparser.website.search import SearchHit

TOKEN = "1234567890:AAEabcdefghijklmnopqrstuvwxyz012345"


class FakeResponse:
    def __init__(self, answer: dict, status: int = 200):
        self.answer = answer
        self.status_code = status

    def json(self):
        return self.answer


class FakeSession:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def post(self, url, json=None, data=None, files=None, timeout=None):
        self.calls.append({"url": url, "json": json, "data": data, "files": files})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(result=True) -> FakeResponse:
    return FakeResponse({"ok": True, "result": result})


def make_bot(*answers):
    sleeps, clock = [], [0.0]

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    session = FakeSession(*answers)
    bot = TelegramBot(TOKEN, session=session, sleep=sleep, clock=lambda: clock[0])
    return bot, session, sleeps


def test_rejects_something_that_is_not_a_token():
    with pytest.raises(TelegramError, match="TELEGRAM_BOT_TOKEN"):
        TelegramBot("not-a-token")


def test_repr_hides_the_token():
    bot, _, _ = make_bot()
    assert TOKEN not in repr(bot)


def test_username_and_chats():
    updates = [
        {"message": {"chat": {"id": 42, "type": "private", "first_name": "Николай"}}},
        {"message": {"chat": {"id": 42, "type": "private", "first_name": "Николай"}}},
        {"my_chat_member": {"chat": {"id": -100123, "type": "group", "title": "Тендеры"}}},
    ]
    bot, session, _ = make_bot(ok({"username": "zakupkirus1_bot"}), ok(updates))
    assert bot.username() == "zakupkirus1_bot"
    assert bot.chats() == [(42, "private", "Николай"), (-100123, "group", "Тендеры")]
    assert session.calls[0]["url"] == f"https://api.telegram.org/bot{TOKEN}/getMe"


def test_send_message_as_html_without_previews_and_spaced_out():
    bot, session, sleeps = make_bot(ok(), ok())
    bot.send_message("42", "<b>1</b>")
    bot.send_message("42", "x" * (MESSAGE_LIMIT + 100))
    first, second = (call["json"] for call in session.calls)
    assert first == {
        "chat_id": "42", "text": "<b>1</b>", "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}
    }
    assert len(second["text"]) == MESSAGE_LIMIT
    assert sleeps == [SEND_INTERVAL]


def test_waits_when_telegram_asks():
    bot, session, sleeps = make_bot(FakeResponse({"ok": False, "parameters": {"retry_after": 3}}, 429), ok())
    bot.send_message("42", "hi")
    assert len(session.calls) == 2
    assert sleeps == [4]


def test_network_error_does_not_carry_the_token():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    bot, _, _ = make_bot(requests.ConnectionError(f"Max retries exceeded with url: {url}"))
    with pytest.raises(TelegramError) as error:
        bot.send_message("42", "hi")
    assert TOKEN not in str(error.value)
    assert "***" in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__  # the original exception, with the URL, is not chained


def test_api_error_explains_a_missing_chat():
    bot, _, _ = make_bot(FakeResponse({"ok": False, "description": "Bad Request: chat not found"}, 400))
    with pytest.raises(TelegramError, match=r"chat not found.*python -m zkparser telegram"):
        bot.send_message("1", "hi")


def test_send_document(tmp_path):
    report = tmp_path / "report.xlsx"
    report.write_bytes(b"xlsx")
    bot, session, _ = make_bot(ok())
    bot.send_document("42", report, "caption")
    call = session.calls[0]
    assert call["url"].endswith("/sendDocument")
    assert call["data"]["chat_id"] == "42"
    assert call["files"]["document"][0] == "report.xlsx"


def test_secret_filter_cleans_log_records(caplog):
    logger = logging.getLogger("zkparser.test")
    secret_filter = SecretFilter(TOKEN)
    with caplog.at_level(logging.INFO, logger="zkparser.test"):
        caplog.handler.addFilter(secret_filter)
        logger.info("url %s", f"https://api.telegram.org/bot{TOKEN}/getMe")
        logger.info("another 9876543210:BBEabcdefghijklmnopqrstuvwxyz0123456 token")
        caplog.handler.removeFilter(secret_filter)
    assert TOKEN not in caplog.text
    assert "9876543210:BBE" not in caplog.text
    assert caplog.text.count("***") == 2


def test_notice_message_escapes_everything_from_the_notice():
    notice = parse_notice((FIXTURES / "notice_drugs.xml").read_bytes())
    notice = replace(notice, title="Препараты <script>alert(1)</script> & прочее")
    found = Found(notice, SearchHit(notice.reg_number, notice.url, published=date(2026, 9, 30)),
                  Verdict(True, ("название: «препарат*»",), (1,)))
    text = notice_message("Лекарства <тест>", found)
    assert text.startswith("<b>Лекарства &lt;тест&gt;</b> · новая закупка")
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; прочее</a>" in text
    assert f'<a href="{notice.url}">' in text
    assert "233 984,40 ₽ · заявки до 08.10.2026 08:00 (МСК+2)" in text
    assert "Заказчик: ДЕПАРТАМЕНТ ЗДРАВООХРАНЕНИЯ ТЮМЕНСКОЙ ОБЛАСТИ" in text
    assert "Почему: название: «препарат*»" in text
    assert "№ 0167200003426008040 · Электронный аукцион" in text
