"""Hourly monitoring through Windows Task Scheduler.

The task is described in full as XML (``schtasks /Create /XML``), which the short form of schtasks cannot do:
* the working folder is the project folder, so the program finds its .env;
* it starts without a console window (pythonw.exe);
* a run missed while the computer was off starts as soon as it is back ("StartWhenAvailable");
* a run still going when the next one is due is not doubled ("IgnoreNew").

Hours are given in Moscow time, as the EIS shows them, and turned into the computer's local time. Monitoring runs on
demand: ``schedule on`` enables the task, ``schedule off`` disables it and keeps its settings.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from .settings import app_data_dir

TASK_FOLDER = "\\Zakupki Parser\\"
TASK_NAME = "Мониторинг"
TASK_PATH = TASK_FOLDER + TASK_NAME
MOSCOW = timezone(timedelta(hours=3))
PROJECT_DIR = Path(__file__).resolve().parents[1]
TIME_LIMIT = "PT3H"  # a long catch-up after a week off may take a while; the next start is skipped meanwhile
NEVER_RAN = 267011  # SCHED_S_TASK_HAS_NOT_RUN

Runner = Callable[..., subprocess.CompletedProcess]


class SchedulerError(RuntimeError):
    """A problem with Task Scheduler. The message can be shown to the user as is."""


@dataclass(frozen=True)
class TaskStatus:
    exists: bool
    enabled: bool = False
    next_run: str = ""
    last_run: str = ""
    last_result: str = ""


def local_start(start: time, now: datetime) -> datetime:
    """Today's start in the computer's local time for a Moscow time ``start``."""
    local_zone = now.tzinfo or now.astimezone().tzinfo
    moscow = datetime.combine(now.astimezone(MOSCOW).date(), start, tzinfo=MOSCOW)
    return moscow.astimezone(local_zone).replace(tzinfo=None)


def repetition(start: time, end: time, every_minutes: int) -> str:
    """How long the hourly repetition lasts, so that the last run falls on ``end`` itself."""
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    if minutes <= 0:
        raise SchedulerError("Конец периода проверок должен быть позже начала")
    if every_minutes < 15:
        raise SchedulerError("Проверять чаще раза в 15 минут не стоит: сайт ЕИС ограничивает частоту запросов")
    return f"PT{minutes + 1}M"


def task_xml(
    profiles: list[Path], start: time, end: time, every_minutes: int, now: datetime, python: Path | None = None
) -> str:
    python = python or windowless_python()
    arguments = " ".join(["-m", "zkparser", "monitor", *(f'"{path.resolve()}"' for path in profiles)])
    first = local_start(start, now)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Zakupki Parser: новые закупки по профилям, уведомления в Telegram. \
С {start:%H:%M} до {end:%H:%M} по Москве, каждые {every_minutes} мин.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{first:%Y-%m-%dT%H:%M:%S}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
      <Repetition>
        <Interval>PT{every_minutes}M</Interval>
        <Duration>{repetition(start, end, every_minutes)}</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>{TIME_LIMIT}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(PROJECT_DIR))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def windowless_python() -> Path:
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    return windowless if windowless.exists() else python


def create(
    profiles: list[Path], start: time, end: time, every_minutes: int, *, run: Runner = subprocess.run
) -> None:
    """Create or replace the task; it starts enabled."""
    definition = app_data_dir() / "monitoring-task.xml"
    definition.write_text(task_xml(profiles, start, end, every_minutes, datetime.now().astimezone()), encoding="utf-16")
    _check(_run(run, ["schtasks", "/Create", "/F", "/TN", TASK_PATH, "/XML", str(definition)]), "создать задачу")


def set_enabled(enabled: bool, *, run: Runner = subprocess.run) -> None:
    if not status(run=run).exists:
        raise SchedulerError("Задача мониторинга ещё не создана: python -m zkparser schedule on <профили>")
    switch = "/ENABLE" if enabled else "/DISABLE"
    _check(_run(run, ["schtasks", "/Change", "/TN", TASK_PATH, switch]), "изменить задачу")


def remove(*, run: Runner = subprocess.run) -> None:
    if status(run=run).exists:
        _check(_run(run, ["schtasks", "/Delete", "/F", "/TN", TASK_PATH]), "удалить задачу")


def status(*, run: Runner = subprocess.run) -> TaskStatus:
    """Read the task through PowerShell: its answer does not depend on the language of Windows."""
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8;"
        f"$t = Get-ScheduledTask -TaskPath '{TASK_FOLDER}' -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
        "function f($d) { if ($d) { $d.ToString('dd.MM.yyyy HH:mm') } else { '' } };"
        "if ($t) { $i = $t | Get-ScheduledTaskInfo;"
        " [pscustomobject]@{state=\"$($t.State)\"; next=(f $i.NextRunTime);"
        " last=(f $i.LastRunTime); result=$i.LastTaskResult} | ConvertTo-Json -Compress }"
    )
    answer = _run(run, ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], encoding="utf-8")
    text = (answer.stdout or "").strip()
    if answer.returncode != 0 or not text:
        return TaskStatus(exists=False)
    data = json.loads(text)
    never_ran = data.get("result") == NEVER_RAN  # Windows then reports a made-up date in 1999
    return TaskStatus(
        exists=True,
        enabled=data.get("state") != "Disabled",
        next_run=data.get("next") or "",
        last_run="" if never_ran else data.get("last") or "",
        last_result="" if never_ran else _result(data.get("result")),
    )


def _result(code: int | None) -> str:
    known = {0: "успешно", 1: "ошибка, подробности в журнале", 267009: "выполняется"}
    if code is None:
        return ""
    return known.get(code, f"код {code}")


def _run(run: Runner, args: list[str], encoding: str = "cp866") -> subprocess.CompletedProcess:
    try:
        return run(args, capture_output=True, text=True, encoding=encoding, errors="replace",
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError as error:
        raise SchedulerError("Планировщик задач Windows недоступен на этом компьютере") from error


def _check(result: subprocess.CompletedProcess, action: str) -> None:
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise SchedulerError(f"Не удалось {action} в Планировщике Windows. {details}".strip())
