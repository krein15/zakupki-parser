"""The bot that listens: "Беру / Не моё" under the cards, /stats, the weekly summary — and the failure protocol.

Monitoring sends each notice as a card with buttons (monitor.py); this process records the presses, which give the
precision of a profile: how many of the notices sent were worth taking. It runs as its own Task Scheduler task while
monitoring is on (scheduler.py) and keeps the service up:

* Telegram unreachable — the bot retries with a growing pause; when the link is back after an outage it tells the
  owner when it was lost;
* an unexpected error — logged with its traceback; the owner gets "⚠️ Бот упал: <причина>", the bot restarts its
  loop a minute later and says "✅ Бот снова работает" once a round goes through;
* the process gone (killed, the computer asleep) — the task starts it again within five minutes, and the next
  monitoring check sees the stale heartbeat, starts the bot and warns the owner if it stays silent (``watch_bot``);
* a monitoring check that fails — one warning when it starts failing and one when checks are back (``report_check``).

Technical messages go to the owner's chat (TELEGRAM_CHAT_ID) only, never to clients' chats.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .display import plural
from .monitor import FeedbackStats, MonitorState
from .scheduler import MOSCOW
from .telegram import SKIP, TAKE, TelegramBot, TelegramError, feedback_buttons

log = logging.getLogger("zkparser.bot")

FIRST_PAUSE = 5  # seconds before retrying an unreachable Telegram; doubles up to MAX_PAUSE
MAX_PAUSE = 60
OUTAGE_WORTH_TELLING = timedelta(minutes=2)
RESTART_PAUSE = 60  # after an unexpected error
WEEKLY_HOUR = 10  # Monday, Moscow time: the summary of the past week
STALE = timedelta(minutes=10)  # a heartbeat older than this means the bot is not listening
BOT_START_WAIT = 30  # seconds the watchdog gives a restarted bot to report in
BOT_INCIDENT, CHECKS_INCIDENT = "bot", "checks"
CHAT_INCIDENT = "chat:"  # + chat id: notices could not be delivered to a client's chat

Clock = Callable[[], datetime]
Notify = Callable[[str], None]

START_TEXT = (
    "Zakupki Parser на связи. Номер этого чата: {chat}.\n"
    "Сюда приходят новые закупки по вашим профилям. Отмечайте их кнопками «Беру» и «Не моё» — так видно, "
    "насколько точно работает отбор. /stats — итоги за неделю."
)


class Listener:
    def __init__(
        self,
        bot: TelegramBot,
        state: MonitorState,
        owner_chat: str = "",
        *,
        clock: Clock = lambda: datetime.now().astimezone(),
        sleep: Callable[[float], None] = time.sleep,
        poll_timeout: int = 50,
    ) -> None:
        self.bot = bot
        self.state = state
        self.owner = owner_chat
        self.clock = clock
        self.sleep = sleep
        self.poll_timeout = poll_timeout

    def run(self, stop: Callable[[], bool] = lambda: False) -> int:
        """Listen until ``stop`` says so; the exit code tells Task Scheduler how it ended."""
        offset: int | None = None
        pause = 0
        outage_since: datetime | None = None
        while not stop():
            try:
                updates = self.bot.updates(offset, self.poll_timeout)
            except TelegramError as error:
                text = str(error)
                if "Conflict" in text:
                    log.warning("Бот уже слушает в другом процессе: этот завершается")
                    return 0
                if "Unauthorized" in text:
                    log.error("Telegram не принимает токен бота: %s", text)
                    return 1
                outage_since = outage_since or self.clock()
                pause = min(max(pause * 2, FIRST_PAUSE), MAX_PAUSE)
                log.warning("%s. Повтор через %d с", text, pause)
                self.sleep(pause)
                continue
            except Exception as error:  # a bug: the failure protocol, then the loop starts over
                self._crashed(error)
                continue
            pause = 0
            if outage_since is not None:
                self._outage_over(outage_since)
                outage_since = None
            try:
                for update in updates:
                    offset = update["update_id"] + 1  # moved first: an update that breaks the bot is not retried
                    self.handle(update)
                self.weekly()
            except Exception as error:
                if isinstance(error, TelegramError):  # an answer did not go through; the press itself is saved
                    log.warning("%s", error)
                else:
                    self._crashed(error)
                    continue
            self.state.beat(self.clock())
            incident = self.state.close_incident(BOT_INCIDENT)
            if incident:
                self.tell_owner(f"✅ Бот снова работает. Был недоступен с {_when(incident.since)}: {incident.reason}")
        return 0

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._press(update["callback_query"])
        elif "message" in update:
            self._message(update["message"])

    def weekly(self) -> None:
        """On Monday morning: the past week by chat; the owner gets every profile, clients their own."""
        now = self.clock()
        moscow = now.astimezone(MOSCOW)
        week = "{}-W{:02d}".format(*moscow.isocalendar()[:2])
        if moscow.weekday() != 0 or moscow.hour < WEEKLY_HOUR or self.state.flag("weekly") == week:
            return
        self.state.set_flag("weekly", week)  # first: a failed send is not repeated every 50 seconds
        since = now - timedelta(days=7)
        rows = self.state.stats(since)
        for chat in sorted({row.chat for row in rows} - {self.owner}):
            self.bot.send_message(chat, summary_text([row for row in rows if row.chat == chat], since, now))
        if self.owner and rows:
            self.bot.send_message(self.owner, summary_text(rows, since, now))

    def tell_owner(self, text: str) -> None:
        if not self.owner:
            log.warning("Сообщение владельцу не отправлено — не задан TELEGRAM_CHAT_ID: %s", text)
            return
        try:
            self.bot.send_message(self.owner, text)
        except TelegramError as error:
            log.warning("Не удалось сообщить владельцу: %s", error)

    def _press(self, query: dict[str, Any]) -> None:
        action, _, reg_number = str(query.get("data", "")).partition(":")
        message = query.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        message_id = message.get("message_id")
        if action not in (TAKE, SKIP) or not chat or not message_id:
            self.bot.answer_callback(query["id"])
            return
        profile = self.state.record_feedback(chat, message_id, reg_number, action, self.clock())
        log.info("«%s» %s: %s", profile, reg_number, "беру" if action == TAKE else "не моё")
        self.bot.answer_callback(query["id"], "Отмечено: беру" if action == TAKE else "Отмечено: не моё")
        try:
            self.bot.edit_buttons(chat, message_id, feedback_buttons(reg_number, _url(message), action))
        except TelegramError as error:
            if "not modified" not in str(error):  # the same button pressed twice
                raise

    def _message(self, message: dict[str, Any]) -> None:
        chat = str((message.get("chat") or {}).get("id", ""))
        command = str(message.get("text", "")).split("@")[0].split(" ")[0].strip().casefold()
        if not chat or not command.startswith("/"):
            return
        if command == "/stats":
            now = self.clock()
            since = now - timedelta(days=7)
            rows = self.state.stats(since, None if chat == self.owner else chat)
            self.bot.send_message(chat, summary_text(rows, since, now))
        else:  # /start, /help and anything unknown
            self.bot.send_message(chat, START_TEXT.format(chat=chat))

    def _crashed(self, error: Exception) -> None:
        log.exception("Бот упал")
        reason = f"{type(error).__name__}: {error}"
        if self.state.open_incident(BOT_INCIDENT, reason, self.clock()):
            self.tell_owner(f"⚠️ Бот упал: {reason}\nПерезапускаюсь через минуту. Подробности — в журнале bot.log.")
        self.sleep(RESTART_PAUSE)

    def _outage_over(self, since: datetime) -> None:
        now = self.clock()
        if now - since >= OUTAGE_WORTH_TELLING:
            self.tell_owner(f"✅ Связь с Telegram восстановлена. Перерыв: {_when(since)} – {_when(now)}.")


def summary_text(rows: list[FeedbackStats], since: datetime, until: datetime) -> str:
    """The cards of the period and their marks by profile, with the precision of the marked ones."""
    period = f"{since.astimezone(MOSCOW):%d.%m}–{until.astimezone(MOSCOW):%d.%m}"
    if not rows:
        return f"📊 {period}: новых закупок по профилям не было."
    lines = [f"📊 Итоги {period}"]
    for row in rows:
        unmarked = row.sent - row.taken - row.skipped
        lines.append(f"«{row.profile or 'без профиля'}»: прислано {row.sent} · ✅ {row.taken} · ❌ {row.skipped}"
                     f" · без отметки {unmarked}")
    taken, skipped = sum(row.taken for row in rows), sum(row.skipped for row in rows)
    if taken + skipped:
        lines.append(f"Точность отбора: {round(100 * taken / (taken + skipped))}% "
                     f"({taken} из {plural(taken + skipped, 'отмеченной', 'отмеченных', 'отмеченных')})")
    else:
        lines.append("Отметок пока нет: нажимайте «Беру» или «Не моё» под закупками — так видно, насколько точно "
                     "работает отбор.")
    return "\n".join(lines)


def report_check(state: MonitorState, notify: Notify, failure: str, now: datetime) -> None:
    """The failure protocol of a monitoring check: a warning when checks start failing, a note when they are back."""
    if failure:
        if state.open_incident(CHECKS_INCIDENT, failure, now):
            notify(f"⚠️ Проверка в {_when(now)} не удалась: {failure}\nСледующая — по расписанию; когда проверки "
                   "пойдут снова, напишу.")
        return
    incident = state.close_incident(CHECKS_INCIDENT)
    if incident:
        notify(f"✅ Проверки снова идут. Сбой с {_when(incident.since)}: {incident.reason}")


def report_delivery(state: MonitorState, notify: Notify, chat: str, profile: str, error: str, now: datetime) -> None:
    """A client's chat that stopped taking notices; the owner hears once and again when it is fixed."""
    key = CHAT_INCIDENT + chat
    if error:
        if state.open_incident(key, error, now):
            notify(f"⚠️ «{profile}»: закупки не доходят в чат {chat}. {error}")
        return
    if state.close_incident(key):
        notify(f"✅ «{profile}»: закупки снова доходят в чат {chat}.")


def watch_bot(
    state: MonitorState,
    notify: Notify,
    *,
    enabled: Callable[[], bool],
    start: Callable[[], None],
    clock: Clock = lambda: datetime.now().astimezone(),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Run by every monitoring check: a bot that stopped listening is started again, the owner warned if it stays
    silent. A computer that just woke up does not cause a false alarm: the bot is given a chance to report in first."""
    if not _fresh(state.heartbeat(), clock()) and enabled():
        start()
        sleep(BOT_START_WAIT)
        beat = state.heartbeat()
        if not _fresh(beat, clock()):
            since = f" с {_when(beat)}" if beat else ""
            if state.open_incident(BOT_INCIDENT, f"не отвечает{since}", clock()):
                notify(f"⚠️ Бот не отвечает на кнопки{since}. Перезапустил задачу; если не поднимется — "
                       "смотрите журнал bot.log.")


def _fresh(beat: datetime | None, now: datetime) -> bool:
    return beat is not None and now - beat < STALE


def _url(message: dict[str, Any]) -> str:
    """The notice's link, from the "Открыть в ЕИС" button of the card being pressed."""
    for row in (message.get("reply_markup") or {}).get("inline_keyboard", []):
        for button in row:
            if button.get("url"):
                return button["url"]
    return ""


def _when(moment: datetime) -> str:
    """Moscow time, as the schedule is set: "14:05 МСК", with the date if it is not today's."""
    moscow = moment.astimezone(MOSCOW)
    today = datetime.now(MOSCOW).date()
    return f"{moscow:%H:%M} МСК" if moscow.date() == today else f"{moscow:%d.%m %H:%M} МСК"
