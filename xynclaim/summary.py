"""Per-slot text for the daily run summary."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

CLAIMED = {"claimed_verified", "claimed_global_locked", "claimed_unverified"}


def reports_since(report_dir: Path, started: datetime) -> list[dict]:
    """Run reports written after `started`, oldest first."""
    reports = []
    for path in sorted(report_dir.glob("run_*.json"), key=lambda item: item.stat().st_mtime):
        if datetime.fromtimestamp(path.stat().st_mtime, started.tzinfo) < started:
            continue
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            continue
    return reports


def latest_slots(reports: list[dict]) -> list[dict]:
    """One entry per slot. A catch-up can scan all slots and then claim only some,
    so a later report's entry replaces the earlier one for the same slot."""
    slots = {}
    for report in reports:
        for slot in report.get("slots", []):
            slots[slot["slot"]] = slot
    return [slots[number] for number in sorted(slots)]


def slot_line(slot: dict) -> str:
    number, status, day = slot["slot"], slot.get("status"), slot.get("day")
    received = slot.get("received_reward")
    label = f"`{number:>2}`"
    if status == "claimed_verified":
        return f"{label} Day {day}: {received}"
    if status == "claimed_global_locked":
        locked = received or "+10 Mastery Points"
        if "global reward locked" not in locked:
            locked += " (global reward locked)"
        return f"{label} Day {day}: {locked}"
    if status == "claimed_unverified":
        return f"{label} Day {day}: claimed, reward not read (expected {slot.get('reward') or '?'})"
    if status == "inspected":
        return f"{label} on Day {day}, not claimed"
    if status == "no_daily":
        return f"{label} no daily panel, skipped"
    return f"{label} {str(status).replace('_', ' ')} on Day {day or '?'}"


def summarize(slots: list[dict]) -> tuple[str, list[str], list[str]]:
    claimed = [str(slot["slot"]) for slot in slots if slot.get("status") in CLAIMED]
    held = [
        str(slot["slot"])
        for slot in slots
        if slot.get("status") not in CLAIMED and slot.get("status") != "inspected"
    ]
    return "\n".join(slot_line(slot) for slot in slots), claimed, held


def next_days(slots: list[dict]) -> str:
    """"Day 4 (30 min 2x XP)" when every claimed slot lines up, otherwise the spread."""
    upcoming = {
        (slot.get("next_day"), slot.get("next_reward"))
        for slot in slots
        if slot.get("status") in CLAIMED and slot.get("next_day")
    }
    if not upcoming:
        return "none"
    if len(upcoming) == 1:
        day, reward = upcoming.pop()
        return f"Day {day} ({reward})" if reward else f"Day {day}"
    return ", ".join(f"Day {day}" for day, _ in sorted(upcoming, key=lambda item: item[0]))


def embed_fields(slots: list[dict], reports: list[dict], skipped=()) -> dict[str, str]:
    _, claimed, held = summarize(slots)
    # --skip-slots slots never reach the claimer, so they aren't in any report.
    held = sorted({*held, *map(str, skipped)}, key=int)
    errors = sum(len(report.get("errors") or []) for report in reports)
    seconds = sum(report.get("total_seconds") or 0 for report in reports)
    return {
        "Claimed": ", ".join(claimed) or "none",
        "Held / skipped": ", ".join(held) or "none",
        "Next": next_days(slots),
        "Errors": str(errors),
        "Duration": f"{round(seconds / 60, 1)} min",
    }
