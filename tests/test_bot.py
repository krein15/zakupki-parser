"""The bot: button presses, commands, the weekly summary and the failure protocol."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from zkparser import bot as listening
from zkparser.bot import Listener, report_check, report_delivery, summary_text, watch_bot
from zkparser.monitor import FeedbackStats, MonitorState
from zkparser.telegram import TelegramError, feedback_buttons

MSK = timezone(timedelta(hours=3))
MONDAY = datetime(2026, 10, 12, 10, 30, tzinfo=MSK)
URL = "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=123"
OWNER, CLIENT = "42", "-100500"


class FakeTelegram:
    """Answers getUpdates round by round: a list of updates or an exception per round."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.offsets, self.sent, self.answers, self.edits = [], [], [], []

    def updates(self, offset, timeout):
        self.offsets.append(offset)
        item = self.rounds.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send_message(self, chat, text, buttons=None):
        self.sent.append((chat, text))
        return len(self.sent)

    def answer_callback(self, callback_id, text=""):
        self.answers.append((callback_id, text))

    def edit_buttons(self, chat, message_id, buttons):
        self.edits.append((chat, message_id, buttons))


@pytest.fixture
def state(tmp_path):
    with MonitorState(tmp_path / "state.sqlite3") as store:
        yield store


def listen(state, telegram, now=MONDAY - timedelta(days=1)):
    clock, sleeps = [now], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    listener = Listener(telegram, state, OWNER, clock=lambda: clock[0], sleep=sleep)
    code = listener.run(stop=lambda: not telegram.rounds)
    return code, sleeps


def press(update_id, data, chat=OWNER, message_id=7):
    markup = feedback_buttons("123", URL)
    message = {"message_id": message_id, "chat": {"id": int(chat)}, "reply_markup": markup}
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "data": data, "message": message}}


def text(update_id, words, chat=OWNER):
    return {"update_id": update_id, "message": {"chat": {"id": int(chat)}, "text": words}}


def test_press_is_recorded_answered_and_ticked(state):
    state.record_message(OWNER, 7, "ГСМ", "123")
    telegram = FakeTelegram([press(1, "take:123")], [press(2, "skip:123")])
    assert listen(state, telegram)[0] == 0
    assert telegram.offsets == [None, 2]
    assert telegram.answers == [("cb1", "Отмечено: беру"), ("cb2", "Отмечено: не моё")]
    chat, message_id, buttons = telegram.edits[-1]
    assert (chat, message_id) == (OWNER, 7)
    assert buttons == feedback_buttons("123", URL, "skip")  # the link to the notice stays
    (row,) = state.stats(datetime(2000, 1, 1, tzinfo=MSK))
    assert (row.profile, row.sent, row.taken, row.skipped) == ("ГСМ", 1, 0, 1)  # the latest press counts


def test_a_press_on_a_card_from_before_the_buttons_is_kept_too(state):
    listen(state, FakeTelegram([press(1, "take:123", message_id=99)]))
    (row,) = state.stats(datetime(2000, 1, 1, tzinfo=MSK))
    assert (row.profile, row.taken) == ("", 1)


def test_start_tells_the_chat_number_and_stats_sums_up_the_week(state):
    state.record_message(CLIENT, 7, "Мебель", "123")
    state.record_feedback(CLIENT, 7, "123", "take", MONDAY)
    telegram = FakeTelegram([text(1, "/start"), text(2, "/stats@tenders_demo_bot", chat=CLIENT)])
    listen(state, telegram)
    (start_chat, start), (stats_chat, stats) = telegram.sent
    assert start_chat == OWNER and "Номер этого чата: 42" in start
    assert stats_chat == CLIENT and "«Мебель»: прислано 1 · ✅ 1 · ❌ 0" in stats


def test_plain_messages_get_no_answer(state):
    telegram = FakeTelegram([text(1, "привет")])
    listen(state, telegram)
    assert telegram.sent == []


def test_a_crash_is_reported_once_and_the_recovery_too(state):
    broken = {"update_id": 1, "callback_query": {"data": "nonsense"}}  # no id: answering it fails
    telegram = FakeTelegram([broken], [], [])
    code, sleeps = listen(state, telegram)
    assert code == 0
    assert telegram.offsets == [None, 2, 2]  # the broken update is not fetched again
    assert sleeps == [listening.RESTART_PAUSE]
    (crash, back) = [message for chat, message in telegram.sent if chat == OWNER]
    assert crash.startswith("⚠️ Бот упал: KeyError")
    assert back.startswith("✅ Бот снова работает")
    assert state.incident(listening.BOT_INCIDENT) is None
    assert state.heartbeat() is not None


def test_a_long_outage_is_reported_when_telegram_is_back(state):
    down = TelegramError("Telegram недоступен: timed out")
    telegram = FakeTelegram(down, down, down, down, down, [])
    _, sleeps = listen(state, telegram)
    assert sleeps == [5, 10, 20, 40, 60]
    (message,) = [message for _, message in telegram.sent]
    assert message.startswith("✅ Связь с Telegram восстановлена")


def test_a_short_outage_is_not_worth_a_message(state):
    telegram = FakeTelegram(TelegramError("Telegram недоступен"), [])
    listen(state, telegram)
    assert telegram.sent == []


@pytest.mark.parametrize(("error", "code"), [("Conflict: terminated by other getUpdates request", 0),
                                             ("Unauthorized", 1)])
def test_another_bot_or_a_revoked_token_ends_the_process(state, error, code):
    telegram = FakeTelegram(TelegramError(f"Telegram отклонил запрос: {error}"), [])
    assert listen(state, telegram)[0] == code
    assert len(telegram.rounds) == 1


def test_weekly_summary_once_on_monday_owner_gets_every_profile(state):
    state.record_message(OWNER, 1, "ГСМ", "1")
    state.record_message(CLIENT, 2, "Мебель", "2")
    state.record_feedback(CLIENT, 2, "2", "skip", MONDAY)
    telegram = FakeTelegram([], [])
    listen(state, telegram, now=MONDAY)
    (client_chat, client), (owner_chat, owner) = telegram.sent
    assert client_chat == CLIENT and "Мебель" in client and "ГСМ" not in client
    assert owner_chat == OWNER and "Мебель" in owner and "ГСМ" in owner
    listen(state, FakeTelegram([]), now=MONDAY + timedelta(hours=1))
    assert state.flag("weekly") == "2026-W42"


def test_no_summary_before_monday_morning(state):
    state.record_message(OWNER, 1, "ГСМ", "1")
    telegram = FakeTelegram([])
    listen(state, telegram, now=MONDAY - timedelta(hours=2))
    assert telegram.sent == []


def test_summary_text():
    rows = [FeedbackStats(OWNER, "ГСМ", 10, 3, 1), FeedbackStats(OWNER, "Мебель", 4, 0, 0)]
    text = summary_text(rows, MONDAY - timedelta(days=7), MONDAY)
    assert text.splitlines() == [
        "📊 Итоги 05.10–12.10",
        "«ГСМ»: прислано 10 · ✅ 3 · ❌ 1 · без отметки 6",
        "«Мебель»: прислано 4 · ✅ 0 · ❌ 0 · без отметки 4",
        "Точность отбора: 75% (3 из 4 отмеченных)",
    ]
    assert "Отметок пока нет" in summary_text([FeedbackStats(OWNER, "ГСМ", 2, 0, 0)], MONDAY, MONDAY)
    assert "не было" in summary_text([], MONDAY, MONDAY)


def test_failing_checks_are_reported_once_and_their_return_too(state):
    told = []
    report_check(state, told.append, "ЕИС не отвечает", MONDAY)
    report_check(state, told.append, "ЕИС не отвечает", MONDAY + timedelta(hours=1))
    report_check(state, told.append, "", MONDAY + timedelta(hours=2))
    report_check(state, told.append, "", MONDAY + timedelta(hours=3))
    assert len(told) == 2
    assert told[0].startswith("⚠️ Проверка в") and "ЕИС не отвечает" in told[0]
    assert told[1].startswith("✅ Проверки снова идут") and "ЕИС не отвечает" in told[1]


def test_a_client_chat_that_stopped_taking_notices(state):
    told = []
    report_delivery(state, told.append, CLIENT, "Мебель", "chat not found", MONDAY)
    report_delivery(state, told.append, CLIENT, "Мебель", "chat not found", MONDAY)
    report_delivery(state, told.append, CLIENT, "Мебель", "", MONDAY)
    assert told == [f"⚠️ «Мебель»: закупки не доходят в чат {CLIENT}. chat not found",
                    f"✅ «Мебель»: закупки снова доходят в чат {CLIENT}."]


class Watchdog:
    def __init__(self, state, enabled=True, comes_back=False):
        self.state, self.enabled, self.comes_back = state, enabled, comes_back
        self.starts, self.told = 0, []

    def start(self):
        self.starts += 1
        if self.comes_back:
            self.state.beat(MONDAY)

    def run(self):
        watch_bot(self.state, self.told.append, enabled=lambda: self.enabled, start=self.start,
                  clock=lambda: MONDAY, sleep=lambda _: None)


def test_watchdog_leaves_a_listening_bot_alone(state):
    state.beat(MONDAY - timedelta(minutes=1))
    watchdog = Watchdog(state)
    watchdog.run()
    assert (watchdog.starts, watchdog.told) == (0, [])


def test_watchdog_wakes_a_silent_bot_without_alarm_when_it_comes_back(state):
    state.beat(MONDAY - timedelta(hours=12))  # the computer slept overnight
    watchdog = Watchdog(state, comes_back=True)
    watchdog.run()
    assert (watchdog.starts, watchdog.told) == (1, [])


def test_watchdog_warns_once_about_a_bot_that_stays_silent(state):
    state.beat(MONDAY - timedelta(hours=1))
    watchdog = Watchdog(state)
    watchdog.run()
    watchdog.run()
    assert watchdog.starts == 2
    (warning,) = watchdog.told
    assert warning.startswith("⚠️ Бот не отвечает на кнопки с")


def test_watchdog_ignores_a_bot_switched_off_with_monitoring(state):
    watchdog = Watchdog(state, enabled=False)
    watchdog.run()
    assert (watchdog.starts, watchdog.told) == (0, [])
