"""Task Scheduler: Moscow hours on the local clock, the task definition and the schtasks/PowerShell calls."""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from zkparser import scheduler
from zkparser.scheduler import SchedulerError, local_start, repetition, task_xml

UTC5 = timezone(timedelta(hours=5))
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.mark.parametrize(
    ("hours", "start", "local"),
    [(5, time(8), "2026-10-04 10:00"), (3, time(8), "2026-10-04 08:00"), (10, time(8), "2026-10-04 15:00")],
)
def test_local_start(hours, start, local):
    now = datetime(2026, 10, 4, 9, 0, tzinfo=timezone(timedelta(hours=hours)))
    assert f"{local_start(start, now):%Y-%m-%d %H:%M}" == local


def test_repetition_ends_on_the_last_hour():
    assert repetition(time(8), time(20), 60) == "PT721M"


@pytest.mark.parametrize(("start", "end", "every", "message"), [
    (time(20), time(8), 60, "позже начала"),
    (time(8), time(20), 5, "15 минут"),
])
def test_repetition_errors(start, end, every, message):
    with pytest.raises(SchedulerError, match=message):
        repetition(start, end, every)


def test_task_definition(tmp_path):
    profile = tmp_path / "мой профиль.toml"
    python = Path(r"C:\Python\pythonw.exe")
    program = (python, ["-m", "zkparser"], scheduler.PROJECT_DIR)
    text = task_xml([profile], time(8), time(20), 60, datetime(2026, 10, 4, 9, 0, tzinfo=UTC5), program)
    root = ET.fromstring(text.encode("utf-16"))
    find = lambda path: root.find(path, NS).text  # noqa: E731
    assert find("t:Triggers/t:CalendarTrigger/t:StartBoundary") == "2026-10-04T10:00:00"
    assert find("t:Triggers/t:CalendarTrigger/t:Repetition/t:Interval") == "PT60M"
    assert find("t:Triggers/t:CalendarTrigger/t:Repetition/t:Duration") == "PT721M"
    assert find("t:Settings/t:StartWhenAvailable") == "true"
    assert find("t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert find("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert find("t:Actions/t:Exec/t:Command") == str(python)
    assert find("t:Actions/t:Exec/t:Arguments") == f'-m zkparser monitor "{profile.resolve()}"'
    assert find("t:Actions/t:Exec/t:WorkingDirectory") == str(scheduler.PROJECT_DIR)


class FakeRunner:
    def __init__(self, status: dict | None = None, returncode: int = 0):
        self.status = status
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "powershell":
            stdout = json.dumps(self.status) if self.status is not None else ""
            return subprocess.CompletedProcess(args, 0, stdout, "")
        return subprocess.CompletedProcess(args, self.returncode, "", "Ошибка: отказано в доступе")


def test_create_registers_the_xml(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "app_data_dir", lambda: tmp_path)
    runner = FakeRunner()
    scheduler.create([tmp_path / "p.toml"], time(8), time(20), 60, run=runner)
    (call,) = runner.calls
    assert call[:5] == ["schtasks", "/Create", "/F", "/TN", scheduler.TASK_PATH]
    assert call[5:] == ["/XML", str(tmp_path / "monitoring-task.xml")]
    assert "<Task" in (tmp_path / "monitoring-task.xml").read_text(encoding="utf-16")


def test_create_reports_a_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "app_data_dir", lambda: tmp_path)
    with pytest.raises(SchedulerError, match="отказано в доступе"):
        scheduler.create([tmp_path / "p.toml"], time(8), time(20), 60, run=FakeRunner(returncode=1))


@pytest.mark.parametrize(("enabled", "switch", "bot"), [(True, "/ENABLE", "/Run"), (False, "/DISABLE", "/End")])
def test_switching_on_and_off_takes_the_bot_along(enabled, switch, bot):
    runner = FakeRunner({"state": "Ready", "next": "", "last": "", "result": 267011})
    scheduler.set_enabled(enabled, run=runner)
    schtasks = [call for call in runner.calls if call[0] == "schtasks"]
    assert schtasks == [
        ["schtasks", "/Change", "/TN", scheduler.TASK_PATH, switch],
        ["schtasks", "/Change", "/TN", scheduler.BOT_TASK_PATH, switch],
        ["schtasks", bot, "/TN", scheduler.BOT_TASK_PATH],
    ]


def test_bot_task_restarts_a_fallen_bot_and_never_times_out():
    program = (Path(r"C:\Python\pythonw.exe"), ["-m", "zkparser"], scheduler.PROJECT_DIR)
    text = scheduler.bot_task_xml(datetime(2026, 10, 6, 12, 0, tzinfo=UTC5), program)
    root = ET.fromstring(text.encode("utf-16"))
    find = lambda path: root.find(path, NS).text  # noqa: E731
    assert find("t:Triggers/t:TimeTrigger/t:StartBoundary") == "2026-10-06T12:00:00"
    assert find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval") == "PT5M"
    assert root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Duration", NS) is None  # repeats indefinitely
    assert find("t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"  # a running bot is left alone
    assert find("t:Settings/t:ExecutionTimeLimit") == "PT0S"
    assert find("t:Actions/t:Exec/t:Arguments") == "-m zkparser bot"


def test_create_with_the_bot_registers_and_starts_it(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "app_data_dir", lambda: tmp_path)
    runner = FakeRunner()
    scheduler.create([tmp_path / "p.toml"], time(8), time(20), 60, with_bot=True, run=runner)
    assert [call[:5] for call in runner.calls] == [
        ["schtasks", "/Create", "/F", "/TN", scheduler.TASK_PATH],
        ["schtasks", "/Create", "/F", "/TN", scheduler.BOT_TASK_PATH],
        ["schtasks", "/Run", "/TN", scheduler.BOT_TASK_PATH],
    ]


def test_switching_a_missing_task_explains_how_to_create_it():
    with pytest.raises(SchedulerError, match="schedule on"):
        scheduler.set_enabled(True, run=FakeRunner())


def test_status():
    runner = FakeRunner({"state": "Disabled", "next": "", "last": "04.10.2026 10:00:00", "result": 0})
    status = scheduler.status(run=runner)
    assert status == scheduler.TaskStatus(True, False, "", "04.10.2026 10:00:00", "успешно")
    assert scheduler.status(run=FakeRunner()).exists is False


def test_status_of_a_task_that_never_ran_hides_the_made_up_date():
    runner = FakeRunner({"state": "Ready", "next": "05.10.2026 10:00", "last": "30.11.1999 00:00", "result": 267011})
    status = scheduler.status(run=runner)
    assert (status.next_run, status.last_run, status.last_result) == ("05.10.2026 10:00", "", "")


def test_remove_only_an_existing_task():
    runner = FakeRunner()
    scheduler.remove(run=runner)
    assert all(call[0] == "powershell" for call in runner.calls)
    runner = FakeRunner({"state": "Ready", "result": 0})
    scheduler.remove(run=runner)
    deleted = [call[-1] for call in runner.calls if call[:2] == ["schtasks", "/Delete"]]
    assert deleted == [scheduler.BOT_TASK_PATH, scheduler.TASK_PATH]


def test_built_program_starts_itself(monkeypatch, tmp_path):
    exe = tmp_path / "ZakupkiParser.exe"
    monkeypatch.setattr(scheduler.sys, "frozen", True, raising=False)
    monkeypatch.setattr(scheduler.sys, "executable", str(exe))
    assert scheduler.launcher() == (exe, [], tmp_path)
