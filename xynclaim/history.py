"""Read completed and interrupted local run history."""
from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path


def read_history(path: Path, limit: int = 500) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    try:
        # Decode each record separately so a truncated UTF-8 tail can't erase earlier runs.
        with path.open("rb") as history:
            for line in history:
                try:
                    record = json.loads(line, parse_constant=lambda _value: None)
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except (OSError, UnicodeError):
        pass
    completed = {
        record["started_utc"] for record in records if isinstance(record.get("started_utc"), str)
    }
    partials: list[dict[str, object]] = []
    try:
        partial_paths = list(path.parent.glob("partial_run_*.json"))
    except OSError:
        partial_paths = []
    for partial_path in partial_paths:
        try:
            record = json.loads(partial_path.read_bytes(), parse_constant=lambda _value: None)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(record, dict)
            and record.get("in_progress") is True
            and isinstance(record.get("started_utc"), str)
            and isinstance(record.get("slots"), list)
            and record["started_utc"] not in completed
        ):
            partials.append(record)
    if partials:
        records.extend(partials)
        records.sort(key=lambda record: str(record.get("started_utc", "")))
    return records[-limit:]


def history_days(records: list[dict[str, object]]) -> dict[date, dict[str, int]]:
    days: dict[date, dict[str, int]] = {}
    for record in records:
        stamp = record.get("started_utc")
        slots = record.get("slots")
        if not isinstance(stamp, str) or not isinstance(slots, list):
            continue
        try:
            run_day = datetime.fromisoformat(stamp).astimezone().date()
        except ValueError:
            continue
        summary = days.setdefault(run_day, {"claimed": 0, "locked": 0, "failed": 0, "no_daily": 0})
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            status = slot.get("status")
            if status == "claimed_global_locked":
                summary["claimed"] += 1
                summary["locked"] += 1
            elif isinstance(status, str) and status.startswith("claimed"):
                summary["claimed"] += 1
            elif status == "no_daily":
                summary["no_daily"] += 1
            elif status in {"failed", "server_code_invalid"}:
                summary["failed"] += 1
    return days
