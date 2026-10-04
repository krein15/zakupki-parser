"""The window's logic that needs no screen: remembered settings, periods, form text."""

from __future__ import annotations

from datetime import date
from pathlib import PureWindowsPath

import pytest

from zkparser.gui.forms import period_text, safe_file_name, short_path, split_list, without_path
from zkparser.gui.preferences import CUSTOM, THREE_DAYS, TODAY, WEEK, Preferences, period_dates
from zkparser.profiles import ProfileError

DAY = date(2026, 10, 4)


def test_preferences_round_trip(tmp_path):
    path = tmp_path / "window.json"
    prefs = Preferences(profile="канцтовары.toml", period=WEEK, monitored=["a.toml"], schedule_every=30)
    prefs.save(path)
    assert Preferences.load(path) == prefs


def test_preferences_survive_a_missing_or_broken_file(tmp_path):
    assert Preferences.load(tmp_path / "none.json") == Preferences()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert Preferences.load(broken) == Preferences()


def test_preferences_ignore_unknown_keys(tmp_path):
    path = tmp_path / "window.json"
    path.write_text('{"period": "Неделя", "from_a_future_version": 1}', encoding="utf-8")
    assert Preferences.load(path).period == WEEK


@pytest.mark.parametrize(
    ("period", "start"),
    [(TODAY, date(2026, 10, 4)), (THREE_DAYS, date(2026, 10, 2)), (WEEK, date(2026, 9, 28))],
)
def test_period_presets(period, start):
    assert period_dates(period, DAY) == (start, DAY)


def test_custom_period():
    assert period_dates(CUSTOM, DAY, "01.09.2026", " 30.09.2026 ") == (date(2026, 9, 1), date(2026, 9, 30))


@pytest.mark.parametrize(
    ("date_from", "date_to", "message"),
    [("", "30.09.2026", "ДД.ММ.ГГГГ"), ("2026-09-01", "30.09.2026", "ДД.ММ.ГГГГ"),
     ("30.09.2026", "01.09.2026", "позже"), ("01.10.2026", "05.10.2026", "будущем")],
)
def test_custom_period_errors(date_from, date_to, message):
    with pytest.raises(ValueError, match=message):
        period_dates(CUSTOM, DAY, date_from, date_to)


def test_split_list():
    assert split_list(" 17.23, 32.99.12;25.99.23  22.29 ") == ["17.23", "32.99.12", "25.99.23", "22.29"]
    assert split_list("") == []


def test_safe_file_name():
    assert safe_file_name('Канцтовары: "Тюмень"/2026') == "Канцтовары Тюмень 2026"
    assert safe_file_name("???") == "профиль"


def test_without_path_drops_only_a_file_name():
    assert without_path(ProfileError("C:\\profiles\\a.toml: укажите регион")) == "укажите регион"
    assert without_path(ValueError("Начало периода позже конца")) == "Начало периода позже конца"
    assert without_path(ValueError("Цена: число")) == "Цена: число"


def test_period_text():
    assert period_text(DAY, DAY) == "04.10.2026"
    assert period_text(date(2026, 9, 28), DAY) == "28.09–04.10.2026"


def test_short_path():
    documents = PureWindowsPath(r"C:\Users\Я\Documents\Zakupki Parser")
    assert short_path(documents) == str(documents)
    long = PureWindowsPath(r"C:\Users\Я\AppData\Local\Temp\claude\session\scratchpad\reports")
    assert short_path(long) == r"C:\…\scratchpad\reports"
