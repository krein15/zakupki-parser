"""Telegram: requests to the Bot API, limits, and a token that leaks neither into errors nor into logs."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
import requests
from conftest import FIXTURES

from zkparser.matching import Verdict
from zkparser.notice_xml import parse_notice
from zkparser.pipeline import Found
from zkparser.telegram import (
    MESSAGE_LIMIT,
    SEND_INTERVAL,
    SecretFilter,
    TelegramBot,
    TelegramError,
    feedback_buttons,
    notice_message,
)
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
        self.calls.append({"url": url, "json": json, "data": data, "files": files, "timeout": timeout})
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
        {"message": {"chat": {"id": 42, "type": "private", "first_name": "Иван"}}},
        {"message": {"chat": {"id": 42, "type": "private", "first_name": "Иван"}}},
        {"my_chat_member": {"chat": {"id": -100123, "type": "group", "title": "Тендеры"}}},
    ]
    bot, session, _ = make_bot(ok({"username": "tenders_demo_bot"}), ok(updates))
    assert bot.username() == "tenders_demo_bot"
    assert bot.chats() == [(42, "private", "Иван"), (-100123, "group", "Тендеры")]
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


TYUMEN_NOON = datetime(2026, 10, 6, 12, 0, tzinfo=timezone(timedelta(hours=5)))


def card(fixture: str, matched_by=("«томатн*»",), positions=(1,), now=TYUMEN_NOON, **changes) -> str:
    notice = replace(parse_notice((FIXTURES / f"notice_{fixture}.xml").read_bytes()), **changes)
    found = Found(notice, SearchHit(notice.reg_number, notice.url, published=date(2026, 9, 30)),
                  Verdict(True, ("название: «томатн*»",), positions, matched_by))
    return notice_message("Продукты <клиент>", found, now)


def test_card_says_what_how_much_until_when_for_whom_and_why():
    lines = card("joint", matched_by=("«томатн*»", "ОКПД2 10.39.17.112")).split("\n")
    assert lines == [
        "🟡 <b>поставка продуктов питания (томатная паста)</b>",  # two days left
        "💰 390 592 ₽ · ⏳ до 08.10 08:00 МСК+2 (2 дн.)",
        "🏛 совместная закупка, 4 заказчика",
        "📦 Томатная паста — 2 872 кг",
        "🎯 «томатн*», ОКПД2 10.39.17.112",
        "Электронный аукцион · № 0167200003426008053 · Продукты &lt;клиент&gt;",
    ]


def test_card_escapes_everything_from_the_notice():
    text = card("drugs", title="Препараты <script>alert(1)</script> & прочее")
    assert "<b>Препараты &lt;script&gt;alert(1)&lt;/script&gt; &amp; прочее</b>" in text
    assert "🏛 ДЕПАРТАМЕНТ ЗДРАВООХРАНЕНИЯ ТЮМЕНСКОЙ ОБЛАСТИ" in text
    assert "<script>" not in text


def test_card_lists_matching_positions_first_and_counts_the_rest():
    text = card("auction_ktru", matched_by=("«бензин*»",), positions=(2, 3))
    assert "📦 Бензин автомобильный (розничная реализация) — 23 000 л\n" in text
    assert "   +2 позиции" in text


@pytest.mark.parametrize(("hours", "signal", "left"), [(-48, "🟢", "(4 дн.)"), (42, "🔴", "(сегодня)")])
def test_card_signals_the_time_left(hours, signal, left):
    text = card("joint", now=TYUMEN_NOON + timedelta(hours=hours))
    assert text.startswith(signal)
    assert left in text


def test_card_without_a_fixed_quantity_shows_no_quantity():
    text = card("quotation")
    assert "📦 Услуги хранения" in text
    assert " — " not in text.split("📦")[1].split("\n")[0]


def test_feedback_buttons():
    url = "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=1"
    (row,) = feedback_buttons("1", url)["inline_keyboard"]
    assert row == [
        {"text": "✅ Беру", "callback_data": "take:1"},
        {"text": "❌ Не моё", "callback_data": "skip:1"},
        {"text": "Открыть в ЕИС", "url": url},
    ]
    (chosen,) = feedback_buttons("1", url, "skip")["inline_keyboard"]
    assert [button["text"] for button in chosen] == ["✅ Беру", "✔ Не моё", "Открыть в ЕИС"]


def test_send_message_with_buttons_returns_its_id():
    bot, session, _ = make_bot(ok({"message_id": 77}))
    buttons = feedback_buttons("1", "")
    assert bot.send_message("42", "текст", buttons) == 77
    assert session.calls[0]["json"]["reply_markup"] == buttons


def test_long_polling_waits_longer_than_telegram_holds_the_request():
    bot, session, _ = make_bot(ok([{"update_id": 5}]))
    assert bot.updates(5, timeout=50) == [{"update_id": 5}]
    call = session.calls[0]
    assert call["url"].endswith("/getUpdates")
    assert call["json"] == {"timeout": 50, "allowed_updates": ["message", "callback_query"], "offset": 5}
    assert call["timeout"] > 50


def test_answer_and_edit_buttons():
    bot, session, _ = make_bot(ok(), ok())
    bot.answer_callback("cb1", "Отмечено")
    bot.edit_buttons("42", 77, feedback_buttons("1", "", "take"))
    first, second = session.calls
    assert first["json"] == {"callback_query_id": "cb1", "text": "Отмечено"}
    assert second["url"].endswith("/editMessageReplyMarkup")
    assert second["json"]["message_id"] == 77
