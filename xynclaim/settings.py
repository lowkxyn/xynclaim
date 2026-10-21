"""Planner settings and decisions based on observed streaks."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path

from xynclaim.hotkey import normalize_hotkey


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


SEQUENCE_MODES = {"always", "align", "hold", "sync"}


class SettingsError(ValueError):
    """An existing settings file is unreadable or structurally unsafe."""


@dataclass(frozen=True)
class PlannerSettings:
    # Legacy plan fields stay readable so an upgrade never discards a user's file.
    # They deliberately do not affect the sequence planner.
    follow_schedule: bool
    missed_day_rule: str
    claim_weekdays: dict[int, tuple[int, ...]]
    day14_targets: dict[int, int | None]
    day14_week_parity: dict[int, int | None]
    webhook_enabled: bool
    webhook_blob: str
    sequence_modes: dict[int, str] = field(default_factory=dict)
    sequence_align_targets: dict[int, int] = field(default_factory=dict)
    run_monitor: str = "current"
    mute_roblox: bool = False
    low_graphics: bool = False
    low_cpu_priority: bool = False
    slot_count: int = 12
    slots: tuple[int, ...] = tuple(range(1, 13))
    access_mode: str = "owner"
    close_when_done: bool = False
    public_fallback: bool = False
    schedule_enabled: bool = False
    schedule_offset_minutes: int = 30
    schedule_catch_up: bool = True
    schedule_day1: str = "claim"
    schedule_roblox_busy: str = "wait"
    hotkey: str = "F6"


@dataclass(frozen=True)
class SequenceDecision:
    """The explicit per-slot decision made without guessing reset semantics."""

    due_slots: tuple[int, ...]
    held_slots: tuple[int, ...]
    reasons: dict[int, str]


def default_settings(slot_count: int = 12) -> PlannerSettings:
    return PlannerSettings(
        follow_schedule=False,
        missed_day_rule="unknown",
        claim_weekdays={slot: tuple(range(7)) for slot in range(1, slot_count + 1)},
        day14_targets={slot: None for slot in range(1, slot_count + 1)},
        day14_week_parity={slot: None for slot in range(1, slot_count + 1)},
        webhook_enabled=False,
        webhook_blob="",
        sequence_modes={slot: "always" if slot == 1 else "align" for slot in range(1, slot_count + 1)},
        sequence_align_targets={slot: 1 for slot in range(2, slot_count + 1)},
        run_monitor="current",
        mute_roblox=False,
        low_graphics=False,
        low_cpu_priority=False,
        slot_count=slot_count,
        slots=tuple(range(1, slot_count + 1)),
    )


def load_settings(path: Path, slot_count: int = 12) -> PlannerSettings:
    defaults = default_settings(slot_count)
    if not path.exists():
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SettingsError(
            f"Settings are unreadable; preserve or remove {path.name} before running"
        ) from error
    if not isinstance(raw, dict):
        raise SettingsError("Settings must contain a JSON object")

    try:
        hotkey = normalize_hotkey(raw.get("hotkey", "F6"))
    except ValueError as error:
        raise SettingsError(str(error)) from error

    missed_day_rule = raw.get("missed_day_rule", "unknown")
    if missed_day_rule not in ("unknown", "pauses", "resets"):
        raise SettingsError("Settings contain an invalid missed-day model")
    if "follow_schedule" in raw and not isinstance(raw["follow_schedule"], bool):
        raise SettingsError("Settings contain an invalid follow-schedule flag")
    weekdays_raw = raw.get("claim_weekdays", {})
    targets_raw = raw.get("day14_targets", {})
    parity_raw = raw.get("day14_week_parity", {})
    sequence_modes_raw = raw.get("sequence_modes")
    sequence_targets_raw = raw.get("sequence_align_targets")
    if not all(isinstance(value, dict) for value in (weekdays_raw, targets_raw, parity_raw)):
        raise SettingsError("Settings contain an invalid per-slot plan")
    if sequence_modes_raw is not None and not isinstance(sequence_modes_raw, dict):
        raise SettingsError("Settings contain invalid sequence modes")
    if sequence_targets_raw is not None and not isinstance(sequence_targets_raw, dict):
        raise SettingsError("Settings contain invalid alignment target slots")
    if sequence_targets_raw is not None:
        for key, value in sequence_targets_raw.items():
            if key not in {str(slot) for slot in range(1, 13)}:
                raise SettingsError("Alignment target slot keys must be 1-12")
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
                raise SettingsError("Alignment target slots must be integers from 1 to 12")
    runtime = raw.get("runtime", {})
    if not isinstance(runtime, dict):
        raise SettingsError("Settings contain invalid runtime preferences")
    run_prefs = raw.get("run_prefs", {})
    schedule = raw.get("schedule", {})
    if not isinstance(run_prefs, dict) or not isinstance(schedule, dict):
        raise SettingsError("Settings contain invalid run or schedule preferences")
    configured_count = run_prefs.get("slot_count", slot_count)
    selected_slots = run_prefs.get("slots", list(range(1, configured_count + 1))) if isinstance(configured_count, int) and not isinstance(configured_count, bool) and 1 <= configured_count <= 12 else None
    if selected_slots is None or not isinstance(selected_slots, list) or not selected_slots or any(isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= configured_count for slot in selected_slots):
        raise SettingsError("Settings contain invalid selected slots")
    if run_prefs.get("access_mode", "owner") not in ("owner", "shared"):
        raise SettingsError("Settings contain invalid access mode")
    for flag in ("close_when_done", "public_fallback"):
        if flag in run_prefs and not isinstance(run_prefs[flag], bool):
            raise SettingsError(f"Settings contain invalid {flag} preference")
    for flag in ("enabled", "catch_up"):
        if flag in schedule and not isinstance(schedule[flag], bool):
            raise SettingsError(f"Settings contain invalid schedule {flag}")
    offset = schedule.get("offset_minutes", 30)
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 240:
        raise SettingsError("Settings contain invalid schedule offset")
    if schedule.get("day1", "claim") not in ("hold", "claim") or schedule.get("roblox_busy", "wait") not in ("wait", "skip"):
        raise SettingsError("Settings contain invalid schedule choices")
    run_monitor = runtime.get("run_monitor", "current")
    if run_monitor not in ("current", "primary", "secondary"):
        raise SettingsError("Settings contain an invalid run display")
    for flag in ("mute_roblox", "low_graphics", "low_cpu_priority"):
        if flag in runtime and not isinstance(runtime[flag], bool):
            raise SettingsError(f"Settings contain an invalid {flag} preference")
    claim_weekdays: dict[int, tuple[int, ...]] = {}
    day14_targets: dict[int, int | None] = {}
    day14_week_parity: dict[int, int | None] = {}
    sequence_modes: dict[int, str] = {}
    sequence_align_targets: dict[int, int] = {}
    for slot in range(1, slot_count + 1):
        values = weekdays_raw.get(str(slot), list(range(7)))
        if not isinstance(values, list):
            raise SettingsError(f"Slot {slot} claim weekdays must be a list")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6
            for value in values
        ):
            raise SettingsError(f"Slot {slot} contains an invalid claim weekday")
        cleaned = tuple(sorted(set(values)))
        claim_weekdays[slot] = cleaned
        target = targets_raw.get(str(slot))
        if target is not None and (
            isinstance(target, bool) or not isinstance(target, int) or not 0 <= target <= 6
        ):
            raise SettingsError(f"Slot {slot} contains an invalid Day 14 target")
        day14_targets[slot] = target
        parity = parity_raw.get(str(slot))
        if parity is not None and (isinstance(parity, bool) or parity not in (0, 1)):
            raise SettingsError(f"Slot {slot} contains an invalid two-week cycle")
        day14_week_parity[slot] = parity
        mode = (sequence_modes_raw or {}).get(str(slot), "always")
        if not isinstance(mode, str) or mode not in SEQUENCE_MODES:
            raise SettingsError(f"Slot {slot} contains an invalid sequence mode")
        if slot == 1 and mode == "align" and sequence_targets_raw is None:
            # Files from before target slots used "align" on slot 1 for target days; slot 1 is
            # now the slot others line up with, so it claims instead of failing to load.
            mode = "always"
        sequence_modes[slot] = mode
        sequence_target = (sequence_targets_raw or {}).get(str(slot), 1 if slot != 1 else None)
        if sequence_target is not None:
            sequence_align_targets[slot] = sequence_target
    validate_sequence(sequence_modes, sequence_align_targets, configured_count)
    notifications = raw.get("notifications", {})
    if not isinstance(notifications, dict):
        raise SettingsError("Settings contain invalid notification preferences")
    if "enabled" in notifications and not isinstance(notifications["enabled"], bool):
        raise SettingsError("Settings contain an invalid notification flag")
    if "webhook_dpapi" in notifications and not isinstance(
        notifications["webhook_dpapi"], str
    ):
        raise SettingsError("Settings contain invalid protected webhook data")
    return PlannerSettings(
        follow_schedule=raw.get("follow_schedule") is True,
        missed_day_rule=missed_day_rule,
        claim_weekdays=claim_weekdays,
        day14_targets=day14_targets,
        day14_week_parity=day14_week_parity,
        webhook_enabled=notifications.get("enabled") is True,
        webhook_blob=notifications.get("webhook_dpapi", ""),
        sequence_modes=sequence_modes,
        sequence_align_targets=sequence_align_targets,
        run_monitor=run_monitor,
        mute_roblox=runtime.get("mute_roblox", False),
        low_graphics=runtime.get("low_graphics", False),
        low_cpu_priority=runtime.get("low_cpu_priority", False),
        slot_count=configured_count,
        slots=tuple(sorted(set(selected_slots))),
        access_mode=run_prefs.get("access_mode", "owner"),
        close_when_done=run_prefs.get("close_when_done", False),
        public_fallback=run_prefs.get("public_fallback", False),
        schedule_enabled=schedule.get("enabled", False),
        schedule_offset_minutes=offset,
        schedule_catch_up=schedule.get("catch_up", True),
        schedule_day1=schedule.get("day1", "claim"),
        schedule_roblox_busy=schedule.get("roblox_busy", "wait"),
        hotkey=hotkey,
    )


def save_settings(path: Path, settings: PlannerSettings) -> None:
    hotkey = normalize_hotkey(settings.hotkey)
    validate_sequence(settings.sequence_modes, settings.sequence_align_targets, settings.slot_count)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "hotkey": hotkey,
        "follow_schedule": settings.follow_schedule,
        "missed_day_rule": settings.missed_day_rule,
        "claim_weekdays": {
            str(slot): list(days) for slot, days in sorted(settings.claim_weekdays.items())
        },
        "day14_targets": {
            str(slot): target for slot, target in sorted(settings.day14_targets.items())
        },
        "day14_week_parity": {
            str(slot): parity for slot, parity in sorted(settings.day14_week_parity.items())
        },
        "sequence_modes": {
            str(slot): mode for slot, mode in sorted(settings.sequence_modes.items())
        },
        "sequence_align_targets": {
            str(slot): target for slot, target in sorted(settings.sequence_align_targets.items())
        },
        "runtime": {
            "run_monitor": settings.run_monitor,
            "mute_roblox": settings.mute_roblox,
            "low_graphics": settings.low_graphics,
            "low_cpu_priority": settings.low_cpu_priority,
        },
        "notifications": {
            "enabled": settings.webhook_enabled,
            "webhook_dpapi": settings.webhook_blob,
        },
        "run_prefs": {
            "slot_count": settings.slot_count,
            "slots": list(settings.slots),
            "access_mode": settings.access_mode,
            "close_when_done": settings.close_when_done,
            "public_fallback": settings.public_fallback,
        },
        "schedule": {
            "enabled": settings.schedule_enabled,
            "offset_minutes": settings.schedule_offset_minutes,
            "catch_up": settings.schedule_catch_up,
            "day1": settings.schedule_day1,
            "roblox_busy": settings.schedule_roblox_busy,
        },
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as settings_file:
            settings_file.write(json.dumps(payload, indent=2) + "\n")
            settings_file.flush()
            os.fsync(settings_file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def slots_due_today(settings: PlannerSettings, slots: list[int], today: date | None = None) -> list[int]:
    """Compatibility wrapper: dates and reset assumptions never suppress a slot."""
    del settings, today
    return list(slots)


def validate_sequence(modes: dict[int, str], targets: dict[int, int], slot_count: int) -> None:
    for slot, mode in modes.items():
        if not isinstance(mode, str) or mode not in SEQUENCE_MODES:
            raise SettingsError(f"Slot {slot} has an unsupported sequence mode")
    for slot, target in targets.items():
        if (isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 12
                or isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= 12):
            raise SettingsError("Alignment targets must map slots 1-12 to slots 1-12")
        if slot == target:
            raise SettingsError(f"Slot {slot} cannot Align with itself; pick another slot")
        if slot <= slot_count and target > slot_count:
            raise SettingsError(f"Slot {slot} must Align with a slot within slot count {slot_count}")
    for slot in range(1, slot_count + 1):
        seen = set()
        current = slot
        while modes.get(current, "always") == "align":
            if current in seen:
                raise SettingsError("Align targets form a cycle; pick a Claim slot as the target")
            seen.add(current)
            target = targets.get(current, 1)
            if current == target:
                raise SettingsError(f"Slot {current} cannot Align with itself; pick another slot")
            if not 1 <= target <= slot_count:
                raise SettingsError(f"Slot {current} must Align with an existing slot")
            if modes.get(target, "always") == "sync":
                raise SettingsError(
                    f"Slot {current} can't Align with slot {target} while slot {target} uses Day 1; "
                    f"set slot {current} to Day 1 too or pick another target"
                )
            current = target


def order_sequence_slots(slots: list[int], modes: dict[int, str], targets: dict[int, int]) -> list[int]:
    """Visit selected dependencies first, preserving the user's order elsewhere."""
    selected = set(slots)
    ordered: list[int] = []
    visiting: set[int] = set()

    def visit(slot: int) -> None:
        if slot in ordered:
            return
        if slot in visiting:
            raise SettingsError("Align targets form a cycle")
        visiting.add(slot)
        if modes.get(slot) == "align":
            target = targets.get(slot, 1)
            if target in selected:
                visit(target)
        visiting.remove(slot)
        ordered.append(slot)

    for slot in slots:
        visit(slot)
    return ordered


def recorded_day_today(record: object, now_utc: datetime | None = None) -> tuple[int | None, bool]:
    """Return today's claimed day or a recent expected day, plus whether it was claimed."""
    if not isinstance(record, dict) or "pending_claim" in record:
        return None, False
    now = now_utc or datetime.now(timezone.utc)
    try:
        claimed_at = datetime.fromisoformat(record["claimed_utc"])
        if claimed_at.tzinfo is None or claimed_at > now:
            return None, False
        # Comparing reset boundaries also handles the 23/25-hour DST cycles.
        reset = next_reported_reset(now)
        claimed_reset = next_reported_reset(claimed_at)
        claimed_today = claimed_reset == reset
        previous_reset = next_reported_reset(reset - timedelta(hours=36))
        if not claimed_today and claimed_reset != previous_reset:
            return None, False
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, False
    day = record.get("claimed_day" if claimed_today else "next_day")
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 14:
        return None, False
    return day, claimed_today


def alignment_reason(slot: int, day: int, target: int, target_day: int) -> str:
    return (f"Slot {slot}: waiting to line up with slot {target} "
            f"(slot {slot} is on Day {day}, slot {target} on Day {target_day})")


def plan_sequence_claims(
    settings: PlannerSettings,
    slots: list[int],
    claim_state: dict[object, object],
    now_utc: datetime | None = None,
) -> SequenceDecision:
    validate_sequence(settings.sequence_modes, settings.sequence_align_targets, settings.slot_count)
    selected = order_sequence_slots(slots, settings.sequence_modes, settings.sequence_align_targets)
    due: list[int] = []
    held: list[int] = []
    reasons: dict[int, str] = {}
    now = now_utc or datetime.now(timezone.utc)
    for slot in selected:
        mode = settings.sequence_modes.get(slot, "always")
        if mode == "hold":
            held.append(slot)
            reasons[slot] = "Held by slot mode"
        elif mode == "sync":
            due.append(slot)
            reasons[slot] = "Day 1 line-up: claims until Day 1, then waits for the other Day 1 slots"
        elif mode == "always":
            due.append(slot)
            reasons[slot] = "Claim anyway"
        else:
            target = settings.sequence_align_targets.get(slot, 1)
            target_day, target_claimed = recorded_day_today(claim_state.get(str(target), claim_state.get(target)), now)
            day, claimed = recorded_day_today(claim_state.get(str(slot), claim_state.get(slot)), now)
            # Yesterday's next_day cannot prove today's panel after a missed day.
            if claimed and target_claimed and day != target_day:
                held.append(slot)
                reasons[slot] = alignment_reason(slot, day, target, target_day)
            else:
                due.append(slot)
                reasons[slot] = (f"Align with slot {target}: check the live panel against Day {target_day}"
                                 if target_day is not None else
                                 f"Slot {target}'s day today is unknown; claim unless the live run resolves it")
    return SequenceDecision(tuple(due), tuple(held), reasons)



def projected_day14(
    claimed_day: int | None,
    last_claimed_utc: str | None,
    claim_days: tuple[int, ...],
    missed_day_rule: str,
    start: date | None = None,
) -> tuple[date | None, str]:
    if claimed_day is None or not 1 <= claimed_day <= 14 or not claim_days:
        return None, "No observed streak day"
    current = start or date.today()
    if claimed_day == 14:
        claims_needed = 14
    else:
        claims_needed = 14 - claimed_day
    if missed_day_rule == "resets" and len(claim_days) < 7:
        return None, "A skipped day may reset the streak; no safe projection"
    if missed_day_rule == "unknown" and len(claim_days) < 7:
        return None, "Missed-day behavior is unverified; no skip-based projection"

    projected = current
    counted = 0
    for offset in range(1, 120):
        candidate = current + timedelta(days=offset)
        if candidate.weekday() in claim_days:
            counted += 1
            if counted == claims_needed:
                projected = candidate
                break
    else:
        return None, "Schedule did not produce a claim within 120 days"
    note = "Assumes the observed streak continues"
    if last_claimed_utc:
        try:
            last_date = datetime.fromisoformat(last_claimed_utc).astimezone().date()
            if last_date < current - timedelta(days=1):
                note = "History has a gap; confirm the in-game day before relying on this"
        except ValueError:
            pass
    return projected, note


def next_reported_reset(now_utc: datetime | None = None) -> datetime:
    """Return the next reported 8 PM US-Eastern reset without requiring tzdata."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for day_offset in range(3):
        eastern_day = (now - timedelta(hours=5)).date() + timedelta(days=day_offset)
        march = calendar_month(eastern_day.year, 3)
        november = calendar_month(eastern_day.year, 11)
        dst_start = next(day for day in march if day.weekday() == 6 and 8 <= day.day <= 14)
        dst_end = next(day for day in november if day.weekday() == 6 and day.day <= 7)
        utc_hour = 0 if dst_start <= eastern_day < dst_end else 1
        reset_utc = datetime.combine(
            eastern_day + timedelta(days=1),
            datetime.min.time(),
            timezone.utc,
        ).replace(hour=utc_hour)
        if reset_utc > now:
            return reset_utc
    raise RuntimeError("Couldn't calculate the next reported reset")


def calendar_month(year: int, month: int) -> list[date]:
    first = date(year, month, 1)
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    return [first + timedelta(days=offset) for offset in range((following - first).days)]
