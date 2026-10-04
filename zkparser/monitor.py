"""Monitoring: catch up since the previous check and report every notice Telegram has not heard about yet.

State lives in SQLite: the notices each profile has seen and reported, and the last day each profile was checked
in full. The first run of a day starts from that day (at most CATCH_UP_DAYS back), so monitoring switched on after
a break picks up what appeared meanwhile; later runs of the same day keep that start, so the day's report keeps the
catch-up too. A notice counts as reported only after Telegram accepts it, so a failed send is retried by the next
run; a notice whose application deadline has passed is not sent any more.
"""

from __future__ import annotations

import html
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .cache import NoticeCache
from .excel import daily_file_name, export_profile
from .fetch import Progress
from .pipeline import Found, ProfileResult, RegionDone, RunResult, run_profiles
from .profiles import Profile
from .telegram import TelegramBot, TelegramError, notice_message
from .website.search import Client

log = logging.getLogger("zkparser.monitor")

CATCH_UP_DAYS = 7
MAX_MESSAGES = 10  # per profile and run; the rest go into one summary with the Excel report attached


class MonitorState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS seen (profile TEXT NOT NULL, reg_number TEXT NOT NULL,"
                " first_seen TEXT NOT NULL, reported_at TEXT, PRIMARY KEY (profile, reg_number))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS checked (profile TEXT PRIMARY KEY, day TEXT NOT NULL, at TEXT NOT NULL,"
                " period_start TEXT)"
            )
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(checked)")}
            if "period_start" not in columns:  # a state file from before the day's start was kept
                self._db.execute("ALTER TABLE checked ADD COLUMN period_start TEXT")

    def __enter__(self) -> MonitorState:
        return self

    def __exit__(self, *exc: object) -> None:
        self._db.close()

    def last_checked(self, profile: str) -> date | None:
        row = self._db.execute("SELECT day FROM checked WHERE profile = ?", (profile,)).fetchone()
        return date.fromisoformat(row[0]) if row else None

    def period_start(self, profile: str) -> date | None:
        """Where the checks of the last checked day started."""
        row = self._db.execute("SELECT period_start FROM checked WHERE profile = ?", (profile,)).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def mark_checked(self, profile: str, day: date, period_start: date | None = None) -> None:
        start = (period_start or day).isoformat()
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO checked (profile, day, at, period_start) VALUES (?, ?, ?, ?)",
                (profile, day.isoformat(), _now(), start),
            )

    def add_seen(self, profile: str, reg_numbers: list[str]) -> set[str]:
        """Remember the notices; return the ones seen for the first time."""
        new = set()
        with self._db:
            for number in reg_numbers:
                cursor = self._db.execute(
                    "INSERT OR IGNORE INTO seen (profile, reg_number, first_seen) VALUES (?, ?, ?)",
                    (profile, number, _now()),
                )
                if cursor.rowcount:
                    new.add(number)
        return new

    def unreported(self, profile: str) -> set[str]:
        rows = self._db.execute("SELECT reg_number FROM seen WHERE profile = ? AND reported_at IS NULL", (profile,))
        return {row[0] for row in rows}

    def mark_reported(self, profile: str, reg_numbers: list[str]) -> None:
        with self._db:
            self._db.executemany(
                "UPDATE seen SET reported_at = ? WHERE profile = ? AND reg_number = ?",
                [(_now(), profile, number) for number in reg_numbers],
            )


@dataclass
class ProfileOutcome:
    profile: Profile
    matches: int = 0
    new: int = 0  # seen for the first time in this run
    sent: int = 0  # reported to Telegram in this run, including a summary of the overflow
    expired: int = 0  # unreported notices dropped because their application deadline has passed
    report: Path | None = None
    error: str = ""  # why Telegram did not get everything


@dataclass
class MonitorResult:
    start: date
    end: date
    run: RunResult
    outcomes: list[ProfileOutcome] = field(default_factory=list)


def monitoring_period(
    profiles: list[Profile], state: MonitorState, today: date, catch_up_days: int = CATCH_UP_DAYS
) -> tuple[date, date]:
    """From the earliest day some profile has not finished checking, but no further back than catch_up_days.

    A profile already checked today keeps the start of today's first run.
    """
    starts = []
    for profile in profiles:
        last = state.last_checked(profile.name)
        starts.append((state.period_start(profile.name) or today) if last == today else (last or today))
    return max(min(starts, default=today), today - timedelta(days=catch_up_days)), today


def monitor(
    client: Client,
    cache: NoticeCache,
    state: MonitorState,
    profiles: list[Profile],
    *,
    today: date,
    now: datetime,
    bot: TelegramBot | None = None,
    default_chat: str = "",
    out_dir: Path | None = None,
    progress: Progress | None = None,
    region_done: RegionDone | None = None,
) -> MonitorResult:
    start, end = monitoring_period(profiles, state, today)
    run = run_profiles(client, cache, profiles, start, end, progress=progress, region_done=region_done)
    result = MonitorResult(start, end, run)

    for profile_result in run.profiles:
        profile = profile_result.profile
        outcome = ProfileOutcome(profile, matches=len(profile_result.matches))
        result.outcomes.append(outcome)
        found = {item.notice.reg_number: item for item in profile_result.matches}
        outcome.new = len(state.add_seen(profile.name, list(found)))
        if out_dir is not None:
            outcome.report = _save_report(out_dir, profile_result, start, end, today, now)

        chat = profile.telegram_chat or default_chat
        if bot is not None and chat:
            pending = [item for number, item in found.items() if number in state.unreported(profile.name)]
            _report(bot, chat, state, pending, outcome, now)
        if not run.error:
            state.mark_checked(profile.name, end, start)
    return result


def _report(
    bot: TelegramBot, chat: str, state: MonitorState, pending: list[Found], outcome: ProfileOutcome, now: datetime
) -> None:
    name = outcome.profile.name
    expired = [item for item in pending if _deadline_passed(item, now)]
    state.mark_reported(name, [item.notice.reg_number for item in expired])
    outcome.expired = len(expired)
    fresh = [item for item in pending if item not in expired]
    try:
        for item in fresh[:MAX_MESSAGES]:
            bot.send_message(chat, notice_message(name, item))
            state.mark_reported(name, [item.notice.reg_number])
            outcome.sent += 1
        rest = fresh[MAX_MESSAGES:]
        if rest:
            caption = f"<b>{_escape(name)}</b>: ещё {len(rest)} новых закупок — список в отчёте"
            if outcome.report:
                bot.send_document(chat, outcome.report, caption)
            else:
                bot.send_message(chat, caption)
            state.mark_reported(name, [item.notice.reg_number for item in rest])
            outcome.sent += len(rest)
    except TelegramError as error:
        outcome.error = str(error)
        log.warning("«%s»: %s", name, error)


def _deadline_passed(item: Found, now: datetime) -> bool:
    deadline = item.notice.applications_end
    return deadline is not None and deadline <= now  # both carry their offsets


def _save_report(
    out_dir: Path, result: ProfileResult, start: date, end: date, today: date, now: datetime
) -> Path | None:
    """The day's report of a profile, rewritten by every run; if it is open in Excel, a copy is made beside it."""
    path = out_dir / daily_file_name(result.profile, today)
    for candidate in (path, path.with_name(f"{path.stem} {now:%H-%M}.xlsx")):
        try:
            return export_profile(candidate, result, start, end, now.replace(tzinfo=None, microsecond=0))
        except PermissionError:
            log.warning("Отчёт %s открыт в другой программе", candidate.name)
    return None


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
