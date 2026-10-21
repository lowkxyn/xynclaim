"""Per-user interactive Windows Task Scheduler registration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import getpass
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET

from xynclaim.settings import PlannerSettings, next_reported_reset


TASK_NAME = "XynClaim Daily"
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
ET.register_namespace("", NS)


def _run_task_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def task_present() -> bool:
    return _run_task_command(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0


def action_command(app_path: Path, frozen: bool = False, executable: str | None = None) -> tuple[str, str]:
    if frozen:
        return executable or sys.executable, "--scheduled"
    interpreter = Path(executable or sys.executable)
    return str(interpreter.with_name("pythonw.exe")), f'"{app_path}" --scheduled'


def next_schedule_time(offset_minutes: int, now_utc: datetime | None = None) -> tuple[datetime, datetime]:
    now = now_utc or datetime.now(timezone.utc)
    reset = next_reported_reset(now - timedelta(hours=5))
    scheduled = reset + timedelta(minutes=offset_minutes)
    while scheduled <= now:
        reset = next_reported_reset(reset + timedelta(seconds=1))
        scheduled = reset + timedelta(minutes=offset_minutes)
    return reset.astimezone(), scheduled.astimezone()


def task_xml(settings: PlannerSettings, app_path: Path, frozen: bool = False,
             executable: str | None = None, now_utc: datetime | None = None) -> str:
    _, scheduled = next_schedule_time(settings.schedule_offset_minutes, now_utc)
    command, arguments = action_command(app_path, frozen, executable)
    root = ET.Element(f"{{{NS}}}Task", version="1.4")
    triggers = ET.SubElement(root, f"{{{NS}}}Triggers")
    trigger = ET.SubElement(triggers, f"{{{NS}}}CalendarTrigger")
    # Local wall clock on purpose: the reset follows US Eastern DST, and a fixed-UTC trigger would fire
    # before the reset when US DST ends. Every launch re-registers, so the user's own DST changes stay correct.
    ET.SubElement(trigger, f"{{{NS}}}StartBoundary").text = scheduled.replace(tzinfo=None).isoformat(timespec="seconds")
    ET.SubElement(trigger, f"{{{NS}}}Enabled").text = "true"
    daily = ET.SubElement(trigger, f"{{{NS}}}ScheduleByDay")
    ET.SubElement(daily, f"{{{NS}}}DaysInterval").text = "1"
    principal = ET.SubElement(ET.SubElement(root, f"{{{NS}}}Principals"), f"{{{NS}}}Principal", id="Author")
    ET.SubElement(principal, f"{{{NS}}}UserId").text = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
    ET.SubElement(principal, f"{{{NS}}}LogonType").text = "InteractiveToken"
    ET.SubElement(principal, f"{{{NS}}}RunLevel").text = "LeastPrivilege"
    options = ET.SubElement(root, f"{{{NS}}}Settings")
    ET.SubElement(options, f"{{{NS}}}StartWhenAvailable").text = str(settings.schedule_catch_up).lower()
    ET.SubElement(options, f"{{{NS}}}MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(options, f"{{{NS}}}ExecutionTimeLimit").text = "PT4H"
    action = ET.SubElement(ET.SubElement(root, f"{{{NS}}}Actions", Context="Author"), f"{{{NS}}}Exec")
    ET.SubElement(action, f"{{{NS}}}Command").text = command
    ET.SubElement(action, f"{{{NS}}}Arguments").text = arguments
    ET.SubElement(action, f"{{{NS}}}WorkingDirectory").text = str(app_path.parent)
    # sync_task writes this as UTF-16; schtasks rejects a file whose declaration disagrees.
    return ET.tostring(root, encoding="unicode")


def sync_task(settings: PlannerSettings, app_path: Path, frozen: bool = False,
              executable: str | None = None) -> None:
    if not settings.schedule_enabled:
        if task_present():
            result = _run_task_command(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "Couldn't delete scheduled task")
        return
    xml = task_xml(settings, app_path, frozen, executable)
    with tempfile.TemporaryDirectory(prefix="xynclaim-task-") as folder:
        path = Path(folder) / "task.xml"
        path.write_text('<?xml version="1.0" encoding="UTF-16"?>\n' + xml, encoding="utf-16")
        result = _run_task_command(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"])
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Couldn't register scheduled task")


def schedule_info(settings: PlannerSettings) -> dict:
    now = datetime.now(timezone.utc)
    _, scheduled = next_schedule_time(settings.schedule_offset_minutes, now)
    reset = next_reported_reset(now)
    return {"enabled": settings.schedule_enabled, "offset_minutes": settings.schedule_offset_minutes,
            "catch_up": settings.schedule_catch_up, "day1": settings.schedule_day1,
            "roblox_busy": settings.schedule_roblox_busy, "task_present": task_present(),
            "next_run_local": scheduled.isoformat() if settings.schedule_enabled else None,
            "reset_local": reset.astimezone().strftime("%H:%M"),
            "next_reset_utc": reset.isoformat()}
