"""Monitoring: what counts as new, what gets sent, retries after a failure, catch-up after a break."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import NOTICES, RegionSite

from zkparser import monitor as monitoring
from zkparser.cache import NoticeCache
from zkparser.monitor import MonitorState, monitor, monitoring_period
from zkparser.profiles import Profile
from zkparser.telegram import TelegramError

TYUMEN = "72000000000"
TODAY = date(2026, 10, 4)
NOW = datetime(2026, 10, 4, 10, 0, tzinfo=timezone(timedelta(hours=5)))
FUEL = "0167100004126000028"  # applications until 09.10 10:00
FOOD = "0167200003426008053"  # until 08.10 08:00


class FakeBot:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.messages: list[tuple[str, str]] = []
        self.documents: list[tuple[str, Path, str]] = []

    def send_message(self, chat, text):
        if self.fail:
            raise TelegramError("Telegram недоступен")
        self.messages.append((chat, text))

    def send_document(self, chat, path, caption=""):
        self.documents.append((chat, path, caption))


def profile(name="ГСМ", **rules) -> Profile:
    rules.setdefault("keywords", ("топливо",))
    return Profile(name=name, regions=(TYUMEN,), path=Path(f"{name}.toml"), **rules)


@pytest.fixture
def stores(tmp_path):
    with NoticeCache(tmp_path / "cache") as cache, MonitorState(tmp_path / "state.sqlite3") as state:
        yield cache, state


def check(stores, profiles, bot=None, now=NOW, site=None, out_dir=None, default_chat="42"):
    cache, state = stores
    site = site or RegionSite({TYUMEN: list(NOTICES)})
    return monitor(site, cache, state, profiles, today=now.date(), now=now, bot=bot, default_chat=default_chat,
                   out_dir=out_dir)


def test_state_remembers_seen_and_reported(tmp_path):
    with MonitorState(tmp_path / "s.sqlite3") as state:
        assert state.add_seen("a", ["1", "2"]) == {"1", "2"}
        assert state.add_seen("a", ["2", "3"]) == {"3"}
        assert state.add_seen("b", ["1"]) == {"1"}  # profiles keep separate histories
        state.mark_reported("a", ["1"])
        assert state.unreported("a") == {"2", "3"}
        state.mark_checked("a", TODAY)
    with MonitorState(tmp_path / "s.sqlite3") as state:
        assert state.last_checked("a") == TODAY
        assert state.last_checked("b") is None


@pytest.mark.parametrize(
    ("last", "start"),
    [(None, TODAY), (date(2026, 10, 1), date(2026, 10, 1)), (date(2026, 9, 1), date(2026, 9, 27))],
)
def test_monitoring_period_catches_up_but_not_forever(tmp_path, last, start):
    with MonitorState(tmp_path / "s.sqlite3") as state:
        if last:
            state.mark_checked("ГСМ", last)
        assert monitoring_period([profile()], state, TODAY) == (start, TODAY)


def test_later_runs_of_the_day_keep_its_start(stores):
    _, state = stores
    state.mark_checked("ГСМ", date(2026, 10, 2))
    first = check(stores, [profile()], FakeBot())
    second = check(stores, [profile()], FakeBot())
    assert (first.start, second.start) == (date(2026, 10, 2), date(2026, 10, 2))
    assert state.last_checked("ГСМ") == TODAY
    tomorrow = check(stores, [profile()], FakeBot(), now=NOW + timedelta(days=1))
    assert tomorrow.start == TODAY  # a new day starts from the last checked one


def test_state_from_an_older_version_gets_the_new_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE checked (profile TEXT PRIMARY KEY, day TEXT NOT NULL, at TEXT NOT NULL)")
        db.execute("INSERT INTO checked VALUES ('ГСМ', '2026-10-03', '2026-10-03T10:00:00')")
    db.close()
    with MonitorState(path) as state:
        assert state.last_checked("ГСМ") == date(2026, 10, 3)
        assert state.period_start("ГСМ") is None
        state.mark_checked("ГСМ", TODAY, date(2026, 10, 1))
        assert state.period_start("ГСМ") == date(2026, 10, 1)


def test_the_earliest_profile_sets_the_period(tmp_path):
    with MonitorState(tmp_path / "s.sqlite3") as state:
        state.mark_checked("a", date(2026, 10, 3))
        state.mark_checked("b", date(2026, 10, 1))
        assert monitoring_period([profile("a"), profile("b")], state, TODAY)[0] == date(2026, 10, 1)


def test_new_notices_are_sent_once(stores):
    bot = FakeBot()
    first = check(stores, [profile()], bot)
    second = check(stores, [profile()], bot)
    (outcome,) = first.outcomes
    assert (outcome.matches, outcome.new, outcome.sent) == (1, 1, 1)
    assert [chat for chat, _ in bot.messages] == ["42"]
    assert FUEL in bot.messages[0][1]
    assert (second.outcomes[0].new, second.outcomes[0].sent) == (0, 0)
    assert stores[1].last_checked("ГСМ") == TODAY


def test_failed_send_is_retried_next_time(stores):
    failing = FakeBot(fail=True)
    outcome = check(stores, [profile()], failing).outcomes[0]
    assert outcome.sent == 0
    assert "недоступен" in outcome.error
    working = FakeBot()
    retry = check(stores, [profile()], working).outcomes[0]
    assert (retry.new, retry.sent) == (0, 1)  # already seen, but not yet reported
    assert len(working.messages) == 1


def test_notices_past_their_deadline_are_not_sent(stores):
    bot = FakeBot()
    later = datetime(2026, 10, 8, 12, 0, tzinfo=timezone(timedelta(hours=5)))
    outcome = check(stores, [profile(keywords=("томатн*", "топливо"))], bot, now=later).outcomes[0]
    assert outcome.matches == 2
    assert (outcome.sent, outcome.expired) == (1, 1)  # food closed at 08:00, fuel is open until the 9th
    assert FUEL in bot.messages[0][1]
    assert stores[1].unreported("ГСМ") == set()


def test_overflow_goes_into_one_summary_with_the_report(stores, tmp_path, monkeypatch):
    monkeypatch.setattr(monitoring, "MAX_MESSAGES", 1)
    bot = FakeBot()
    everything = profile("Всё", keywords=(), okpd2=("10", "19", "21", "52", "69"))
    outcome = check(stores, [everything], bot, out_dir=tmp_path / "reports").outcomes[0]
    assert outcome.sent == 5
    assert len(bot.messages) == 1
    ((chat, path, caption),) = bot.documents
    assert (chat, path) == ("42", outcome.report)
    assert "ещё 4 новых закупок" in caption


def test_a_profile_can_have_its_own_chat(stores):
    bot = FakeBot()
    check(stores, [profile(telegram_chat="-1001234567890")], bot)
    assert bot.messages[0][0] == "-1001234567890"


def test_without_a_chat_nothing_is_sent_and_nothing_is_lost(stores):
    bot = FakeBot()
    outcome = check(stores, [profile()], bot, default_chat="").outcomes[0]
    assert (outcome.new, outcome.sent) == (1, 0)
    assert stores[1].unreported("ГСМ") == {FUEL}


def test_stopped_download_does_not_move_the_checked_day(stores):
    class LimitedSite(RegionSite):
        def get(self, path, params=None):
            raise_limit = path.endswith("viewXml.html")
            if raise_limit:
                from zkparser.website.client import RateLimited

                raise RateLimited("Сайт ЕИС ограничил частоту запросов")
            return super().get(path, params)

    result = check(stores, [profile()], FakeBot(), site=LimitedSite({TYUMEN: list(NOTICES)}))
    assert result.run.error
    assert stores[1].last_checked("ГСМ") is None


def test_daily_report_is_rewritten_and_survives_an_open_file(stores, tmp_path, monkeypatch):
    out = tmp_path / "reports"
    first = check(stores, [profile()], out_dir=out).outcomes[0].report
    assert first.name == "ГСМ_мониторинг_2026-10-04.xlsx"
    original = monitoring.export_profile
    calls = []

    def locked_once(path, *args):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError(path)
        return original(path, *args)

    monkeypatch.setattr(monitoring, "export_profile", locked_once)
    second = check(stores, [profile()], out_dir=out).outcomes[0].report
    assert second.name == "ГСМ_мониторинг_2026-10-04 10-00.xlsx"
