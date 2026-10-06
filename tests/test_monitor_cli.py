"""The monitoring command around a check: the failure protocol, the bot's status line, one bot at a time."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from zkparser import monitor_cli
from zkparser.config import TelegramSettings
from zkparser.monitor import MonitorResult, MonitorState, ProfileOutcome
from zkparser.pipeline import RunResult
from zkparser.profiles import Profile
from zkparser.scheduler import TaskStatus

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone(timedelta(hours=5)))
SETTINGS = TelegramSettings(token="1234567890:AAEabcdefghijklmnopqrstuvwxyz012345", chat="42")


def outcome(name: str, chat: str = "", error: str = "") -> ProfileOutcome:
    profile = Profile(name=name, regions=("72000000000",), keywords=("x",), telegram_chat=chat, path=Path("p.toml"))
    return ProfileOutcome(profile, error=error)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_cli.scheduler, "bot_status", lambda: TaskStatus(exists=True, enabled=False))
    with MonitorState(tmp_path / "state.sqlite3") as store:
        yield store


def protocol(state, run_error="", outcomes=()):
    told = []
    result = MonitorResult(date(2026, 10, 6), date(2026, 10, 6), RunResult([], error=run_error), list(outcomes))
    monitor_cli._protocol(result, state, told.append, SETTINGS, NOW)
    return told


def test_a_failed_check_is_reported_to_the_owner(state):
    (warning,) = protocol(state, run_error="ЕИС не отвечает")
    assert warning.startswith("⚠️ Проверка в") and "ЕИС не отвечает" in warning
    (recovery,) = protocol(state)
    assert recovery.startswith("✅ Проверки снова идут")


def test_a_stop_on_request_is_not_a_failure(state):
    assert protocol(state, run_error=monitor_cli.STOPPED) == []


def test_only_clients_chats_are_reported_as_unreachable(state):
    told = protocol(state, outcomes=[outcome("Мебель", chat="-100500", error="chat not found"),
                                     outcome("Своё", error="Telegram недоступен")])
    assert told == ["⚠️ «Мебель»: закупки не доходят в чат -100500. chat not found"]


def test_bot_line(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_cli, "state_path", lambda: tmp_path / "state.sqlite3")
    assert "не настроен" in monitor_cli.bot_line(TaskStatus(exists=False))
    assert "выключен" in monitor_cli.bot_line(TaskStatus(exists=True, enabled=False))
    assert "ещё не выходил" in monitor_cli.bot_line(TaskStatus(exists=True, enabled=True))
    with MonitorState(tmp_path / "state.sqlite3") as store:
        store.beat(datetime.now().astimezone())
    assert "слушает кнопки" in monitor_cli.bot_line(TaskStatus(exists=True, enabled=True))


@pytest.mark.skipif(sys.platform != "win32", reason="the lock is a Windows file lock, like the scheduler itself")
def test_one_bot_at_a_time(tmp_path):
    first = monitor_cli._single_instance(tmp_path / "bot.lock")
    assert first is not None
    assert monitor_cli._single_instance(tmp_path / "bot.lock") is None
    first.close()
    again = monitor_cli._single_instance(tmp_path / "bot.lock")
    assert again is not None
    again.close()
