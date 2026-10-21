"""Summaries of stored run reports and observed claim state."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import math
import re


ZENI = re.compile(r"^(\d[\d,]*) Zeni$", re.I)
MASTERY = re.compile(r"^\+(\d[\d,]*) Mastery Points(?: \(.+\))?$", re.I)
BOOST = re.compile(r"^2X (EXP|XP|Zeni|Drops?) for (\d+) min(?: (\d+) s)?$", re.I)
BALL = re.compile(r"^(?:Received\s+)?(?:\+(\d+)\s+)?([1-7])-Star(?: .+)? Dragon Ball\.?$", re.I)
ITEM = re.compile(r"^(\d+)x (.+)$", re.I)


def _local_date(stamp: object) -> date | None:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp).astimezone().date()
    except (OSError, OverflowError, ValueError):
        return None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def summarize(records: list[dict], claim_state: dict, slot_count: int, today: date | None = None,
              *, item_limit: int | None = 8) -> dict:
    today = today or date.today()
    collected = {key: 0 for key in (
        "zeni", "mastery_points", "xp_boost_minutes", "zeni_boost_minutes",
        "drop_boost_minutes", "dragon_balls", "dragon_balls_unique",
        "consumables", "addon_packs",
    )}
    item_counts: Counter[str] = Counter()
    ball_counts: Counter[int] = Counter()
    daily: dict[date, dict] = defaultdict(lambda: {"claims": 0, "run_seconds": 0.0, "timed_runs": 0})
    slot_data = {slot: {"slot": slot, "claims": 0, "missed": 0, "verified_pct": 0, "zeni": 0, "streak_day": None} for slot in range(1, slot_count + 1)}
    slot_verified: Counter[int] = Counter()
    claim_dates: dict[int, set[date]] = defaultdict(set)
    run_times: list[float] = []
    slot_times: list[float] = []
    claims = verified = unread = 0

    for record in records:
        run_date = _local_date(record.get("started_utc"))
        run_seconds = _number(record.get("total_seconds"))
        if run_seconds is not None:
            run_times.append(run_seconds)
        if run_date is not None:
            if run_seconds is not None:
                daily[run_date]["run_seconds"] += run_seconds
                daily[run_date]["timed_runs"] += 1
        results = record.get("slots")
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, dict):
                continue
            slot = result.get("slot")
            if isinstance(slot, bool) or not isinstance(slot, int) or slot not in slot_data:
                continue
            seconds = _number(result.get("seconds"))
            if seconds is not None:
                slot_times.append(seconds)
            status = result.get("status")
            claimed = isinstance(status, str) and status.startswith("claimed_")
            if not claimed:
                if status not in ("held", "skipped"):
                    slot_data[slot]["missed"] += 1
                continue
            claims += 1
            slot_data[slot]["claims"] += 1
            if run_date is not None:
                daily[run_date]["claims"] += 1
                claim_dates[slot].add(run_date)
            reward = result.get("received_reward")
            read = isinstance(reward, str) and bool(reward.strip())
            if status == "claimed_verified" or (status == "claimed_global_locked" and read):
                verified += 1
                slot_verified[slot] += 1
            if not read:
                unread += 1
                continue
            for part in reward.split(";"):
                part = part.strip()
                if match := ZENI.fullmatch(part):
                    amount = int(match[1].replace(",", ""))
                    collected["zeni"] += amount
                    slot_data[slot]["zeni"] += amount
                elif match := MASTERY.fullmatch(part):
                    collected["mastery_points"] += int(match[1].replace(",", ""))
                elif match := BOOST.fullmatch(part):
                    key = {"exp": "xp_boost_minutes", "xp": "xp_boost_minutes", "zeni": "zeni_boost_minutes", "drop": "drop_boost_minutes", "drops": "drop_boost_minutes"}[match[1].lower()]
                    collected[key] += int(match[2]) + (int(match[3]) / 60 if match[3] else 0)
                elif match := BALL.fullmatch(part):
                    quantity = int(match[1]) if match[1] is not None else 1
                    collected["dragon_balls"] += quantity
                    if quantity:
                        ball_counts[int(match[2])] += quantity
                elif match := ITEM.fullmatch(part):
                    quantity, name = int(match[1]), match[2].strip()
                    item_counts[name] += quantity
                    collected["consumables"] += quantity
                elif "add-on pack" in part.lower() or "addon pack" in part.lower():
                    collected["addon_packs"] += 1
                    item_counts[part] += 1

    longest = 0
    longest_slots: list[int] = []
    for slot, dates in claim_dates.items():
        length = 0
        previous = None
        best = 0
        for claimed_date in sorted(dates):
            length = length + 1 if previous == claimed_date - timedelta(days=1) else 1
            best = max(best, length)
            previous = claimed_date
        if best > longest:
            longest, longest_slots = best, [slot]
        elif best == longest and best:
            longest_slots.append(slot)

    for slot, summary in slot_data.items():
        summary["verified_pct"] = round(slot_verified[slot] * 100 / summary["claims"]) if summary["claims"] else 0
        state = claim_state.get(str(slot), claim_state.get(slot, {}))
        day = state.get("claimed_day") if isinstance(state, dict) else None
        summary["streak_day"] = day if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 14 else None
    collected["dragon_balls_unique"] = len(ball_counts)
    days = []
    for offset in range(13, -1, -1):
        day = today - timedelta(days=offset)
        data = daily.get(day)
        days.append({"date": day.isoformat(), "claims": data["claims"] if data else 0,
                     "run_seconds": round(data["run_seconds"], 2) if data and data["timed_runs"] else None})
    return {
        "totals": {"runs": len(records), "claims": claims, "verified": verified, "unread": unread,
                   "verified_pct": round(verified * 100 / claims) if claims else 0,
                   "avg_run_seconds": round(sum(run_times) / len(run_times), 2) if run_times else None,
                   "avg_slot_seconds": round(sum(slot_times) / len(slot_times), 2) if slot_times else None,
                   "longest_streak_days": longest, "longest_streak_slots": sorted(longest_slots)},
        "days": days, "collected": collected,
        "dragon_balls_detail": [[f"{star}-Star", count] for star, count in sorted(ball_counts.items())],
        "items": [{"name": name, "count": count} for name, count in sorted(item_counts.items(), key=lambda item: (-item[1], item[0].casefold()))[:item_limit]],
        "slots": list(slot_data.values()),
    }


def validate_slot(slot: int) -> None:
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 12:
        raise ValueError("Slot must be an integer between 1 and 12")


def _utc_stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def summarize_slot(records: list[dict], claim_state: dict, slot: int) -> dict:
    validate_slot(slot)
    filtered = []
    attempts = []
    last_claimed = None
    for record in records:
        results = record.get("slots")
        results = [result for result in results if isinstance(result, dict)
                   and isinstance(result.get("slot"), int) and not isinstance(result["slot"], bool)
                   and result["slot"] == slot] if isinstance(results, list) else []
        if not results:
            continue
        filtered.append({**record, "slots": results})
        stamp = _utc_stamp(record.get("started_utc"))
        local_day = _local_date(record.get("started_utc"))
        for result in results:
            status = result.get("status")
            if isinstance(status, str) and status.startswith("claimed_") and stamp is not None:
                last_claimed = max(last_claimed, stamp) if last_claimed else stamp
            day = result.get("day")
            reward = result.get("received_reward")
            attempts.append((stamp or datetime.min.replace(tzinfo=timezone.utc), len(attempts), {
                "date": local_day.isoformat() if local_day else "",
                "day": day if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 14 else None,
                "status": status if isinstance(status, str) else None,
                "received_reward": reward if isinstance(reward, str) else None,
                "seconds": _number(result.get("seconds")),
            }))
    summary = summarize(filtered, claim_state, 12, item_limit=None)
    totals = summary["totals"]
    state = claim_state.get(str(slot), claim_state.get(slot, {}))
    state_stamp = _utc_stamp(state.get("claimed_utc")) if isinstance(state, dict) else None
    if state_stamp is not None:
        last_claimed = max(last_claimed, state_stamp) if last_claimed else state_stamp
    return {
        "slot": slot,
        "claims": totals["claims"],
        "verified": totals["verified"],
        "verified_pct": totals["verified_pct"],
        "current_day": summary["slots"][slot - 1]["streak_day"],
        "longest_streak": totals["longest_streak_days"],
        "last_claimed_utc": last_claimed.isoformat() if last_claimed else None,
        "avg_seconds": totals["avg_slot_seconds"],
        "collected": summary["collected"],
        "items": [[item["name"], item["count"]] for item in summary["items"]],
        "dragon_balls_detail": summary["dragon_balls_detail"],
        "recent": [attempt[2] for attempt in sorted(attempts, key=lambda attempt: attempt[:2], reverse=True)[:14]],
    }
