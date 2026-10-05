"""Einrichtung der Windows-Aufgabenplanung."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from .browsers import CREATE_NO_WINDOW
from .config import ScheduleRule

TASK_NAME = "Cookie-Bereinigung"

TASK_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>{logon_trigger}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{interpreter}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_directory}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

LOGON_TRIGGER = """
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
      <Delay>PT2M</Delay>
    </LogonTrigger>"""


class SchedulerError(RuntimeError):
    pass


def _windowless_interpreter() -> Path:
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    return windowless if windowless.exists() else executable


def build_task_xml(
    rule: ScheduleRule,
    user: str,
    interpreter: Path,
    working_directory: Path,
    arguments: str = "-m cookie_cleaner run",
    today: date | None = None,
) -> str:
    start = f"{(today or date.today()).isoformat()}T{rule.time}:00"
    logon = LOGON_TRIGGER.format(user=escape(user)) if rule.run_at_logon else ""
    return TASK_TEMPLATE.format(
        start=start,
        logon_trigger=logon,
        user=escape(user),
        interpreter=escape(str(interpreter)),
        arguments=escape(arguments),
        working_directory=escape(str(working_directory)),
    )


def _run(arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *arguments],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        timeout=30,
    )


def is_installed() -> bool:
    return _run(["/Query", "/TN", TASK_NAME]).returncode == 0


def install(rule: ScheduleRule) -> None:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    if getattr(sys, "frozen", False):
        interpreter, arguments = Path(sys.executable), "run"
        working_directory = interpreter.parent
    else:
        interpreter, arguments = _windowless_interpreter(), "-m cookie_cleaner run"
        working_directory = Path(__file__).resolve().parent.parent
    xml = build_task_xml(rule, user, interpreter, working_directory, arguments)
    with tempfile.TemporaryDirectory() as folder:
        definition = Path(folder) / "task.xml"
        definition.write_text(xml, encoding="utf-16")
        completed = _run(["/Create", "/TN", TASK_NAME, "/XML", str(definition), "/F"])
    if completed.returncode != 0:
        raise SchedulerError((completed.stderr or completed.stdout).strip())


def remove() -> None:
    if not is_installed():
        return
    completed = _run(["/Delete", "/TN", TASK_NAME, "/F"])
    if completed.returncode != 0:
        raise SchedulerError((completed.stderr or completed.stdout).strip())
