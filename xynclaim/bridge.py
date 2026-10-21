"""Local pywebview bridge for XynClaim's bundled UI.

Global hotkey presses stop runs here or request a start through the UI's normal path.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
import base64
import ctypes
from ctypes import wintypes
import csv
import json
import os
import sys
import threading
import time
from typing import Any, Callable

from xynclaim import APP_VERSION, engine
from xynclaim import schedule
from xynclaim import stats, window_icon
from xynclaim.settings import (
    PlannerSettings,
    SettingsError,
    default_settings,
    load_settings,
    next_reported_reset,
    plan_sequence_claims,
    save_settings,
    validate_sequence,
)
from xynclaim.dpapi import protect_secret, unprotect_secret
from xynclaim.history import read_history
from xynclaim.hotkey import HotkeyListener, normalize_hotkey
from xynclaim.webhook import send_webhook, validate_webhook_url, webhook_payload


APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
DISCORD_INVITE_URL = "https://discord.gg/dbogen"
SW_MINIMIZE = 6
SW_RESTORE = 9
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


def _access_label(access_mode: str) -> str:
    return "Shared private code" if access_mode == "shared" else "Owner private server"


def _coerce_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be true or false")
    return value


def _coerce_slot_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
        raise ValueError("Slot count must be between 1 and 12")
    return value


def _coerce_slots(value: object, slot_count: int) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError("Select at least one slot")
    if any(isinstance(slot, bool) or not isinstance(slot, int) for slot in value):
        raise ValueError("Slots must be whole numbers")
    slots = list(dict.fromkeys(value))
    invalid = [slot for slot in slots if slot < 1 or slot > slot_count]
    if invalid:
        raise ValueError("Selected slots are outside the configured slot count")
    return slots


class XynClaimAPI:
    """Thread-safe state and a deliberately small API exposed to local JavaScript."""

    def __init__(self, settings_path: Path | None = None) -> None:
        self.settings_path = settings_path or engine.SETTINGS_PATH
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._started_at: float | None = None
        self._elapsed_seconds = 0.0
        self._status = "idle"
        self._current_slot: int | None = None
        self._logs: list[str] = []
        self._final_report: dict[str, object] | None = None
        self._last_error = ""
        self._window: Any | None = None
        self._active_slots: list[int] = []
        self._maximized = False
        self._configured_slot_count = 12
        self._close_requested = False
        self._compact_geometry: tuple[int, int, int, int] | None = None
        self._scheduled = False
        self._runner: Any | None = None
        self._live_results: list[dict[str, object]] = []
        self._scheduled_waiting = False
        self._screens: list[Any] = []
        self._hotkey_listener: HotkeyListener | None = None
        self._hotkey_lock = threading.Lock()
        self._hotkey_start_requested = False
        self._status_before_pause: str | None = None

    def _settings(self) -> PlannerSettings:
        return load_settings(self.settings_path)

    def _mutate_settings(
        self, mutator: Callable[[PlannerSettings], PlannerSettings]
    ) -> PlannerSettings:
        """Apply one settings change without overwriting another process's update."""
        lock_path = self.settings_path.with_name(f".{self.settings_path.name}.lock")
        with engine.exclusive_file_lock(lock_path):
            settings = load_settings(self.settings_path)
            updated = mutator(settings)
            save_settings(self.settings_path, updated)
            return updated

    def _safe_log(self, message: str, secret: str = "") -> None:
        safe = engine.redact_secret(str(message), secret)
        try:
            with engine.exclusive_file_lock(engine.REPORT_DIR / ".live_log.lock"):
                with engine.LIVE_LOG_PATH.open("a", encoding="utf-8", newline="\n") as live_log:
                    live_log.write(safe + "\n")
        except OSError:
            # The in-app log remains authoritative if an external tailer locks the file.
            pass
        with self._lock:
            self._logs.append(safe)
            self._logs = self._logs[-250:]
            lowered = safe.lower()
            if self._status in {"starting", "launching", "joining", "claiming", "returning"}:
                if "launching" in lowered:
                    self._status = "launching"
                elif "opening private server" in lowered:
                    self._status = "joining"
                elif "claiming day" in lowered:
                    self._status = "claiming"
                elif "returning" in lowered:
                    self._status = "returning"
            if safe.startswith("Slot "):
                try:
                    self._current_slot = int(safe.split(":", 1)[0].split()[1])
                except (IndexError, ValueError):
                    pass

    def _set_paused(self, paused: bool) -> None:
        with self._lock:
            if paused and self._status != "stopping":
                self._status_before_pause = self._status
                self._status = "paused"
            elif not paused and self._status == "paused":
                self._status = self._status_before_pause or "claiming"

    def on_hotkey(self) -> None:
        """Global hotkey: stop an active run here; a start goes through the UI's normal Start path."""
        with self._lock:
            active = self._scheduled_waiting or (self._worker is not None and self._worker.is_alive())
            if not active:
                self._hotkey_start_requested = True
        if active:
            self.stop_run()

    def _register_hotkey(self, key: str) -> None:
        if self._hotkey_listener is None:
            self._hotkey_listener = HotkeyListener(self.on_hotkey)
        if not self._hotkey_listener.change(key):
            if key:
                raise RuntimeError(f"{key} is already used by another program")
            raise RuntimeError("Couldn't turn off the global hotkey")

    def save_hotkey(self, key: str) -> dict[str, str]:
        key = normalize_hotkey(key)
        # The message thread calls on_hotkey, so never wait for it while holding _lock.
        with self._hotkey_lock:
            previous = self._hotkey_listener.label if self._hotkey_listener else ""
            self._register_hotkey(key)
            try:
                self._mutate_settings(lambda settings: replace(settings, hotkey=key))
            except Exception:
                self._register_hotkey(previous)
                raise
        return {"hotkey": key}

    def _progress(self) -> float:
        if not self._active_slots:
            return 0.0
        if self._final_report is not None:
            return 100.0
        if self._current_slot in self._active_slots:
            index = self._active_slots.index(self._current_slot)
            return round(index * 100 / len(self._active_slots), 1)
        return 0.0

    @staticmethod
    def _public_report(report: object) -> dict[str, object]:
        data = asdict(report)
        data["server_profile"] = _access_label(str(data.get("access_mode", "owner")))
        return data

    @staticmethod
    def _public_history(records: list[dict[str, object]]) -> list[dict[str, object]]:
        public: list[dict[str, object]] = []
        for record in records[-30:]:
            slots = record.get("slots")
            errors = record.get("errors")
            public.append(
                {
                    "started_utc": record.get("started_utc"),
                    "finished_utc": record.get("finished_utc"),
                    "total_seconds": record.get("total_seconds"),
                    "access": _access_label(str(record.get("access_mode", "owner"))),
                    "slots": [slot for slot in slots if isinstance(slot, dict)] if isinstance(slots, list) else [],
                    "errors": errors if isinstance(errors, list) else [],
                }
            )
        return public

    def get_state(self) -> dict[str, object]:
        settings_error = ""
        try:
            settings = self._settings()
        except SettingsError as error:
            settings = default_settings()
            settings_error = str(error)
        try:
            with engine.exclusive_file_lock(engine.HISTORY_LOCK_PATH):
                history = read_history(engine.HISTORY_PATH, limit=30)
        except OSError:
            history = read_history(engine.HISTORY_PATH, limit=30)
        with self._lock:
            elapsed = time.monotonic() - self._started_at if self._started_at is not None else self._elapsed_seconds
            minutes, seconds = divmod(int(elapsed), 60)
            return {
                "running": self._scheduled_waiting or (self._worker is not None and self._worker.is_alive()),
                "status": self._status,
                "paused_reason": (
                    "Roblox isn't in front. Click back into Roblox to continue."
                    if self._status == "paused" else ""
                ),
                "hotkey": self._hotkey_listener.label if self._hotkey_listener else "",
                "hotkey_start_requested": self._consume_hotkey_start(),
                "current_slot": self._current_slot,
                "active_slots": list(self._active_slots),
                # Results so far, so the dashboard can show rewards before the run ends.
                "live_results": [asdict(result) for result in list(self._runner.results)] if self._runner else list(self._live_results),
                "elapsed_seconds": round(elapsed, 1),
                "elapsed": f"{minutes:02d}:{seconds:02d}",
                "progress": self._progress(),
                "ticker": self._logs[-1] if self._logs and self._worker and self._worker.is_alive() else "",
                "logs": list(self._logs),
                "error": self._last_error,
                "settings_error": settings_error,
                "close_pending": self._close_requested,
                "report": self._final_report,
                "settings": {
                    "sequence_modes": {str(slot): mode for slot, mode in settings.sequence_modes.items()},
                    "sequence_align_targets": {str(slot): target for slot, target in settings.sequence_align_targets.items()},
                    "run_monitor": settings.run_monitor,
                    "mute_roblox": settings.mute_roblox,
                    "low_graphics": settings.low_graphics,
                    "low_cpu_priority": settings.low_cpu_priority,
                    "notifications_enabled": settings.webhook_enabled,
                    "has_webhook": bool(settings.webhook_blob),
                    "configured_slot_count": self._configured_slot_count,
                    "slot_count": settings.slot_count,
                    "slots": list(settings.slots),
                    "access_mode": settings.access_mode,
                    "close_when_done": settings.close_when_done,
                    "public_fallback": settings.public_fallback,
                },
                "monitors": self._monitor_options(),
                "history": self._public_history(history),
            }

    def _consume_hotkey_start(self) -> bool:
        requested, self._hotkey_start_requested = self._hotkey_start_requested, False
        return requested

    def _clear_run_display(self, run_display: str) -> int | None:
        """Move this window off the display Roblox will use.

        Any window over a Roblox click point makes the run stop (seen on a real run 30 Sep).
        Moves to another display when there is one, otherwise minimizes. Returns the handle
        when it minimized, so the run can bring the window back afterwards.
        """
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, engine.APP_NAME)
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if not hwnd or owner.value != os.getpid():
            return None
        displays = engine.list_displays()
        roblox = engine.find_roblox_window()
        if run_display == "current" and roblox is not None:
            target = engine._display_for_window(roblox.hwnd)
        elif run_display == "secondary":
            target = next((display for display in displays if not display.is_primary), displays[0])
        else:
            # "current" with Roblox not open yet: it launches on the primary display.
            target = next(display for display in displays if display.is_primary)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        overlaps = (rect.left < target.left + target.width and rect.right > target.left
                    and rect.top < target.top + target.height and rect.bottom > target.top)
        if not overlaps:
            return None
        other = next((display for display in displays if display.device != target.device), None)
        if other is None:
            self._safe_log("Minimized XynClaim so it can't cover Roblox; it comes back when the run ends")
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            return hwnd
        if user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            self._maximized = False
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = min(rect.right - rect.left, other.work_width)
        height = min(rect.bottom - rect.top, other.work_height)
        user32.SetWindowPos(hwnd, None, other.work_left + (other.work_width - width) // 2,
                            other.work_top + (other.work_height - height) // 2,
                            width, height, SWP_NOZORDER | SWP_NOACTIVATE)
        self._safe_log("Moved XynClaim to another display so it can't cover Roblox")
        return None

    @staticmethod
    def _monitor_options() -> list[dict[str, object]]:
        displays = engine.list_displays()
        options: list[dict[str, object]] = [
            {"value": "current", "label": "Current Roblox display"}
        ]
        primary = next(display for display in displays if display.is_primary)
        options.append(
            {
                "value": "primary",
                "label": f"Primary — {primary.width}×{primary.height}",
                "width": primary.width,
                "height": primary.height,
            }
        )
        secondary = next((display for display in displays if not display.is_primary), None)
        if secondary is not None:
            options.append(
                {
                    "value": "secondary",
                    "label": f"Second display — {secondary.width}×{secondary.height}",
                    "width": secondary.width,
                    "height": secondary.height,
                }
            )
        return options

    def consume_last_error(self) -> dict[str, object]:
        with self._lock:
            error = self._last_error
            self._last_error = ""
        return {"error": error}

    def reset_settings(self) -> dict[str, object]:
        lock_path = self.settings_path.with_name(f".{self.settings_path.name}.lock")
        with engine.exclusive_file_lock(lock_path):
            try:
                load_settings(self.settings_path)
            except SettingsError:
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
                backup = self.settings_path.with_name(f"settings.corrupt_{stamp}.json")
                self.settings_path.replace(backup)
                save_settings(self.settings_path, default_settings())
                return {"reset": True, "backup": backup.name}
        return {"reset": False}

    def start_run(self, config: object) -> dict[str, object]:
        if not isinstance(config, dict):
            raise ValueError("Run configuration must be an object")
        with self._lock:
            if self._close_requested:
                raise RuntimeError("The window is closing")
            if self._scheduled_waiting or (self._worker is not None and self._worker.is_alive()):
                raise RuntimeError("A run is already active")

        access_mode = config.get("access_mode", "owner")
        if access_mode not in {"owner", "shared"}:
            raise ValueError("Access mode must be owner or shared")
        slot_count = _coerce_slot_count(config.get("slot_count", 12))
        slots = _coerce_slots(config.get("slots"), slot_count)
        close_when_done = _coerce_bool(config.get("close_when_done", False), "close_when_done")
        public_fallback = _coerce_bool(config.get("public_fallback", False), "public_fallback")
        shared_code = ""
        if access_mode == "shared":
            shared_code = engine.normalize_server_code(str(config.get("server_code", "")))
        profile = engine.normalize_profile_name(
            str(config.get("server_profile", "")), access_mode, shared_code
        )
        settings = self._settings()
        if config.get("scheduled_day1") == "hold":
            modes = dict(settings.sequence_modes)
            state = engine.load_claim_state()
            # The planner's hold mode is the existing way to suppress a Day 1 claim.
            for slot in slots:
                observed = state.get(str(slot), {})
                if isinstance(observed, dict) and observed.get("next_day") == 1:
                    modes[slot] = "hold"
            settings = replace(settings, sequence_modes=modes)
        decision = plan_sequence_claims(settings, slots, engine.load_claim_state())
        due_slots = list(decision.due_slots)
        if not due_slots:
            held = ", ".join(map(str, decision.held_slots)) or "none"
            raise ValueError(f"No selected slots are due; held slots: {held}")
        run_display = config.get("run_monitor", settings.run_monitor)
        if run_display not in {"current", "primary", "secondary"}:
            raise ValueError("Run display must be current, primary, or secondary")
        mute_roblox = _coerce_bool(
            config.get("mute_roblox", settings.mute_roblox), "mute_roblox"
        )
        low_graphics = _coerce_bool(
            config.get("low_graphics", settings.low_graphics), "low_graphics"
        )
        low_cpu_priority = _coerce_bool(
            config.get("low_cpu_priority", settings.low_cpu_priority),
            "low_cpu_priority",
        )

        def work() -> None:
            nonlocal shared_code
            minimized_hwnd = None
            try:
                minimized_hwnd = self._clear_run_display(run_display)
                runner = engine.DBOGRunner(
                    due_slots,
                    lambda message: self._safe_log(message, shared_code),
                    self._stop_event,
                    close_when_done=close_when_done,
                    access_mode=access_mode,
                    server_code=shared_code,
                    server_profile=profile,
                    configured_slot_count=slot_count,
                    allow_public_fallback=public_fallback,
                    run_display=run_display,
                    mute_roblox=mute_roblox,
                    low_graphics=low_graphics,
                    low_cpu_priority=low_cpu_priority,
                    slot_mismatch_policies={
                        slot: settings.sequence_modes.get(slot, "always")
                        for slot in due_slots
                    },
                    sequence_align_targets=settings.sequence_align_targets,
                )
                with self._lock:
                    self._runner = runner
                report = runner.run()
                with self._lock:
                    self._final_report = self._public_report(report)
                    self._status = "complete" if not report.errors else "needs_attention"
                try:
                    notification_settings = self._settings()
                    if notification_settings.webhook_enabled and notification_settings.webhook_blob:
                        send_webhook(
                            unprotect_secret(notification_settings.webhook_blob),
                            webhook_payload(report),
                        )
                        self._safe_log("Discord report delivered")
                except (OSError, ValueError) as error:
                    self._safe_log(f"Discord report failed — {error}")
            except Exception as error:
                message = engine.redact_secret(f"{type(error).__name__}: {error}", shared_code)
                with self._lock:
                    self._last_error = message
                    self._status = "failed"
                self._safe_log(f"RUN FAILED — {message}", shared_code)
            finally:
                shared_code = ""
                if minimized_hwnd:
                    ctypes.windll.user32.ShowWindow(minimized_hwnd, SW_RESTORE)
                with self._lock:
                    if self._final_report is not None:
                        self._elapsed_seconds = float(self._final_report["total_seconds"])
                    elif self._started_at is not None:
                        self._elapsed_seconds = time.monotonic() - self._started_at
                    if self._runner is not None:
                        self._live_results = [asdict(result) for result in list(self._runner.results)]
                    self._started_at = None
                    self._current_slot = None
                    self._runner = None

        # Keep cleanup alive if the user dismisses the window after requesting stop.
        # Python stays in the background until DBOGRunner.run() finishes its finally block.
        worker = threading.Thread(target=work, name="XynClaimRunner", daemon=False)
        with self._lock:
            # Validation can overlap another start or a close request.
            if self._close_requested:
                raise RuntimeError("The window is closing")
            if self._scheduled_waiting or (self._worker is not None and self._worker.is_alive()):
                raise RuntimeError("A run is already active")
            self._stop_event = threading.Event()
            self._started_at = time.monotonic()
            self._elapsed_seconds = 0.0
            self._status = "starting"
            self._current_slot = None
            self._logs = []
            self._last_error = ""
            self._final_report = None
            self._live_results = []
            self._runner = None
            self._active_slots = list(due_slots)
            self._configured_slot_count = slot_count
            self._worker = worker
            try:
                worker.start()
            except RuntimeError as error:
                self._worker = None
                self._started_at = None
                self._status = "failed"
                self._last_error = engine.redact_secret(str(error), shared_code)
                raise
        return {
            "started": True,
            "slots": due_slots,
            "held_slots": list(decision.held_slots),
            "decisions": {str(slot): reason for slot, reason in decision.reasons.items()},
            "access": _access_label(access_mode),
        }

    def stop_run(self) -> dict[str, object]:
        with self._lock:
            active = self._scheduled_waiting or (self._worker is not None and self._worker.is_alive())
            if active:
                self._stop_event.set()
                self._status = "stopping"
        return {"stopping": active}

    def get_run_prefs(self) -> dict[str, object]:
        settings = self._settings()
        return {"slot_count": settings.slot_count, "slots": list(settings.slots),
                "access_mode": settings.access_mode, "close_when_done": settings.close_when_done,
                "public_fallback": settings.public_fallback}

    def save_run_prefs(self, config: object) -> dict[str, object]:
        if not isinstance(config, dict):
            raise ValueError("Run preferences must be an object")
        slot_count = _coerce_slot_count(config.get("slot_count"))
        slots = _coerce_slots(config.get("slots"), slot_count)
        access_mode = config.get("access_mode")
        if access_mode not in {"owner", "shared"}:
            raise ValueError("Access mode must be owner or shared")
        close_when_done = _coerce_bool(config.get("close_when_done"), "close_when_done")
        public_fallback = _coerce_bool(config.get("public_fallback"), "public_fallback")
        self._mutate_settings(lambda settings: replace(settings, slot_count=slot_count,
            slots=tuple(slots), access_mode=access_mode, close_when_done=close_when_done,
            public_fallback=public_fallback))
        return self.get_run_prefs()

    def get_schedule(self) -> dict[str, object]:
        return schedule.schedule_info(self._settings())

    def save_schedule(self, config: object) -> dict[str, object]:
        if not isinstance(config, dict):
            raise ValueError("Schedule must be an object")
        enabled = _coerce_bool(config.get("enabled"), "enabled")
        catch_up = _coerce_bool(config.get("catch_up"), "catch_up")
        offset = config.get("offset_minutes")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 240:
            raise ValueError("Offset must be between 0 and 240 minutes")
        day1 = config.get("day1")
        busy = config.get("roblox_busy")
        if day1 not in {"hold", "claim"} or busy not in {"wait", "skip"}:
            raise ValueError("Invalid schedule choice")
        def update(old: PlannerSettings) -> PlannerSettings:
            settings = replace(old, schedule_enabled=enabled, schedule_offset_minutes=offset,
                               schedule_catch_up=catch_up, schedule_day1=day1, schedule_roblox_busy=busy)
            schedule.sync_task(
                settings,
                Path(sys.executable) if getattr(sys, "frozen", False)
                else Path(__file__).resolve().with_name("__main__.py"),
                frozen=bool(getattr(sys, "frozen", False)),
            )
            return settings

        settings = self._mutate_settings(update)
        return schedule.schedule_info(settings)

    @staticmethod
    def _all_history() -> list[dict[str, object]]:
        with engine.exclusive_file_lock(engine.HISTORY_LOCK_PATH):
            records = read_history(engine.HISTORY_PATH, limit=1_000_000_000)
        for record in records:
            slots = record.get("slots")
            errors = record.get("errors")
            record["slots"] = [slot for slot in slots if isinstance(slot, dict)] if isinstance(slots, list) else []
            record["errors"] = errors if isinstance(errors, list) else []
        return records

    def get_stats(self) -> dict[str, object]:
        return stats.summarize(self._all_history(), engine.load_claim_state(), self._settings().slot_count)

    def get_slot_stats(self, slot: int) -> dict[str, object]:
        stats.validate_slot(slot)
        return stats.summarize_slot(self._all_history(), engine.load_claim_state(), slot)

    def export_stats_csv(self, slot: int | None = None) -> dict[str, str]:
        if slot is not None:
            stats.validate_slot(slot)
        downloads = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        prefix = "XynClaim" if slot is None else f"XynClaim-slot{slot}"
        path = downloads / f"{prefix}-history-{datetime.now().strftime('%Y%m%d')}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(("date", "slot", "day", "status", "received_reward", "seconds"))
            for record in self._all_history():
                started = record.get("started_utc")
                try:
                    local_date = datetime.fromisoformat(started).astimezone().date().isoformat()
                except (OSError, OverflowError, TypeError, ValueError):
                    local_date = ""
                for result in record.get("slots", []):
                    if isinstance(result, dict) and (slot is None or (
                        isinstance(result.get("slot"), int) and not isinstance(result["slot"], bool)
                        and result["slot"] == slot
                    )):
                        writer.writerow((local_date, result.get("slot"), result.get("day"),
                                         result.get("status"), _csv_text(result.get("received_reward")),
                                         result.get("seconds")))
        return {"path": str(path)}

    def get_streak(self) -> dict[str, object]:
        settings = self._settings()
        state = engine.load_claim_state()
        today = datetime.now().astimezone().date()
        todays_rewards: dict[int, str | None] = {}
        for record in self._all_history():
            try:
                local_date = datetime.fromisoformat(record["started_utc"]).astimezone().date()
            except (OSError, OverflowError, KeyError, TypeError, ValueError):
                continue
            if local_date == today:
                for result in record.get("slots", []):
                    if isinstance(result, dict) and str(result.get("status", "")).startswith("claimed_"):
                        if isinstance(result.get("slot"), int) and not isinstance(result["slot"], bool):
                            reward = result.get("received_reward")
                            todays_rewards[result["slot"]] = reward if isinstance(reward, str) and reward.strip() else "Claimed (popup not read)"
        # A claim before the previous cycle started says nothing reliable about today's panel:
        # the slot may have been claimed elsewhere or missed days since.
        fresh_after = next_reported_reset() - timedelta(hours=48)
        slots = []
        for slot in range(1, settings.slot_count + 1):
            observed = state.get(str(slot), {})
            if not isinstance(observed, dict):
                observed = {}
            day = observed.get("claimed_day")
            next_day = observed.get("next_day")
            day = day if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 14 else None
            next_day = next_day if isinstance(next_day, int) and not isinstance(next_day, bool) and 1 <= next_day <= 14 else None
            try:
                claimed_at = datetime.fromisoformat(observed["claimed_utc"])
                stale = claimed_at.tzinfo is None or claimed_at < fresh_after
            except (KeyError, TypeError, ValueError):
                stale = True
            if stale:
                next_day = None
            slots.append({"slot": slot, "claimed_today": todays_rewards.get(slot),
                          "streak_day": day,
                          "stale": stale and day is not None,
                          "next_day": next_day,
                          "next_reward": engine.DAILY_REWARDS.get(next_day),
                          "mode": settings.sequence_modes.get(slot, "always"),
                          "align_target": settings.sequence_align_targets.get(slot)})
        return {"track": [{"day": day, "reward": reward} for day, reward in engine.DAILY_REWARDS.items()], "slots": slots}

    def get_run(self, started_utc: object) -> dict[str, object] | None:
        for record in reversed(self._all_history()):
            if record.get("started_utc") == started_utc:
                result = dict(record)
                result["slots"] = [dict(slot, has_receipt=self._receipt_path(slot.get("receipt_image")) is not None)
                                   for slot in record.get("slots", []) if isinstance(slot, dict)]
                return result
        return None

    @staticmethod
    def _receipt_path(name: object) -> Path | None:
        if not isinstance(name, str) or not name:
            return None
        try:
            root = engine.REPORT_DIR.resolve()
            candidate = (root / name).resolve()
            return candidate if candidate.is_relative_to(root) and candidate.is_file() else None
        except (OSError, ValueError):
            return None

    def get_receipt(self, started_utc: object, slot: object) -> dict[str, str | None]:
        record = self.get_run(started_utc)
        if record is None:
            return {"data_uri": None}
        for result in record["slots"]:
            if result.get("slot") == slot:
                path = self._receipt_path(result.get("receipt_image"))
                if path is not None:
                    try:
                        receipt = path.read_bytes()
                    except OSError:
                        return {"data_uri": None}
                    return {"data_uri": "data:image/png;base64," + base64.b64encode(receipt).decode("ascii")}
        return {"data_uri": None}

    def open_reports_folder(self) -> dict[str, str]:
        engine.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(engine.REPORT_DIR)
        return {"path": str(engine.REPORT_DIR)}

    def open_discord(self) -> dict[str, str]:
        # Fixed URL, so the page can't ask the backend to open anything else.
        os.startfile(DISCORD_INVITE_URL)
        return {"url": DISCORD_INVITE_URL}

    def setup_checks(self) -> list[dict[str, str]]:
        roblox = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "Versions"
        installed = any(roblox.glob("*/RobloxPlayerBeta.exe"))
        displays = engine.list_displays()
        fits = any(display.width >= 1920 and display.height >= 1080 for display in displays)
        try:
            from winrt.windows.media.ocr import OcrEngine
            ocr_ready = OcrEngine.try_create_from_user_profile_languages() is not None
        except (ImportError, RuntimeError):
            ocr_ready = False
        return [
            {"id": "roblox", "state": "ok" if installed else "bad", "title": "Roblox installed", "detail": "Roblox player found" if installed else "XynClaim couldn't find Roblox for this Windows user. Install Roblox from roblox.com. XynClaim never installs anything itself."},
            {"id": "display", "state": "ok" if fits else "bad", "title": "1920x1080 display", "detail": "A suitable display is available" if fits else "Connect a display that fits a 1920x1080 client area"},
            {"id": "ocr", "state": "ok" if ocr_ready else "warn", "title": "Windows OCR", "detail": "OCR language available" if ocr_ready else "Install an English OCR language in Windows Settings > Language"},
            {"id": "desktop", "state": "warn", "title": "Unlocked desktop", "detail": "Keep the desktop unlocked during runs"},
        ]

    def get_launch_info(self) -> dict[str, object]:
        return {"scheduled": self._scheduled, "version": APP_VERSION}

    def save_plan(self, plan: object) -> dict[str, object]:
        if not isinstance(plan, dict):
            raise ValueError("Plan must be an object")

        def table(name: str, validator: Any, fallback: dict[int, Any]) -> dict[int, Any]:
            raw = plan.get(name, {})
            if not isinstance(raw, dict):
                raise ValueError(f"{name} must be an object")
            result = dict(fallback)
            for key, value in raw.items():
                try:
                    slot = int(key)
                except (TypeError, ValueError) as error:
                    raise ValueError(f"{name} slot keys must be 1-12") from error
                if (isinstance(key, bool) or not isinstance(key, (int, str))
                        or str(key) != str(slot) or not 1 <= slot <= 12):
                    raise ValueError(f"{name} slot keys must be 1-12")
                result[slot] = validator(value)
            return result

        def mode(value: object) -> str:
            if not isinstance(value, str) or value not in {"always", "align", "hold", "sync"}:
                raise ValueError("Sequence mode must be Claim, Align, Hold, or Day 1")
            return str(value)

        def target_slot(value: object) -> int:
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
                raise ValueError("Alignment target slots must be integers from 1 to 12")
            return value

        def update(settings: PlannerSettings) -> PlannerSettings:
            run_monitor = plan.get("run_monitor", settings.run_monitor)
            if run_monitor not in {"current", "primary", "secondary"}:
                raise ValueError("Run display must be current, primary, or secondary")
            updated = replace(
                settings,
                follow_schedule=False,
                missed_day_rule="unknown",
                sequence_modes=table("sequence_modes", mode, settings.sequence_modes),
                sequence_align_targets=table(
                    "sequence_align_targets", target_slot, settings.sequence_align_targets
                ),
                run_monitor=run_monitor,
                mute_roblox=_coerce_bool(
                    plan.get("mute_roblox", settings.mute_roblox), "mute_roblox"
                ),
                low_graphics=_coerce_bool(
                    plan.get("low_graphics", settings.low_graphics), "low_graphics"
                ),
                low_cpu_priority=_coerce_bool(
                    plan.get("low_cpu_priority", settings.low_cpu_priority),
                    "low_cpu_priority",
                ),
            )

            validate_sequence(updated.sequence_modes, updated.sequence_align_targets, updated.slot_count)
            return updated

        self._mutate_settings(update)
        return {"saved": True}

    def save_notifications(self, config: object) -> dict[str, object]:
        if not isinstance(config, dict):
            raise ValueError("Notification settings must be an object")
        url = config.get("webhook_url", "")
        if not isinstance(url, str):
            raise ValueError("Webhook URL must be text")

        def update(settings: PlannerSettings) -> PlannerSettings:
            blob = settings.webhook_blob
            if url.strip():
                blob = protect_secret(validate_webhook_url(url))
            enabled = _coerce_bool(config.get("enabled", settings.webhook_enabled), "enabled")
            if enabled and not blob:
                raise ValueError("Enter a Discord webhook URL before enabling notifications")
            return replace(settings, webhook_enabled=enabled, webhook_blob=blob)

        updated = self._mutate_settings(update)
        return {"saved": True, "enabled": updated.webhook_enabled, "has_webhook": bool(updated.webhook_blob)}

    def test_notification(self, config: object | None = None) -> dict[str, object]:
        settings = self._settings()
        url = ""
        if config is not None:
            if not isinstance(config, dict):
                raise ValueError("Notification test settings must be an object")
            candidate = config.get("webhook_url", "")
            if not isinstance(candidate, str):
                raise ValueError("Webhook URL must be text")
            if candidate.strip():
                url = validate_webhook_url(candidate)
        if not url:
            if not settings.webhook_blob:
                raise ValueError("No Discord webhook is saved")
            url = unprotect_secret(settings.webhook_blob)

        def work() -> None:
            try:
                send_webhook(
                    url,
                    {
                        "username": "XynClaim",
                        "allowed_mentions": {"parse": []},
                        "embeds": [{"title": "XynClaim notification test", "description": "Notifications are connected.", "color": 0x7EB39A}],
                    },
                )
                self._safe_log("Discord notification test sent")
            except Exception as error:
                self._safe_log(f"Discord notification test failed — {type(error).__name__}: {error}")

        threading.Thread(target=work, name="XynClaimWebhookTest", daemon=True).start()
        return {"started": True}

    def window_action(self, action: object) -> dict[str, object]:
        if action not in {"minimize", "maximize", "restore", "toggle-maximize", "close", "compact", "expand"}:
            raise ValueError("Unknown window action")
        window = self._window
        if window is None:
            raise RuntimeError("Window is not available")
        if action == "compact":
            if self._compact_geometry is None:
                if self._maximized:
                    window.restore()
                self._compact_geometry = (window.x, window.y, window.width, window.height)
                screen = next((screen for screen in self._screens
                               if screen.x <= window.x < screen.x + screen.width
                               and screen.y <= window.y < screen.y + screen.height),
                              self._screens[0] if self._screens else None)
                width = min(520, screen.width) if screen else 520
                height = min(64, screen.height) if screen else 64
                window.resize(width, height)
                if screen:
                    window.move(max(screen.x, min(window.x, screen.x + screen.width - width)),
                                max(screen.y, min(window.y, screen.y + screen.height - height)))
                window.on_top = True
        elif action == "expand":
            if self._compact_geometry is not None:
                x, y, width, height = self._compact_geometry
                screen = next((screen for screen in self._screens
                               if screen.x <= x < screen.x + screen.width
                               and screen.y <= y < screen.y + screen.height),
                              self._screens[0] if self._screens else None)
                if screen:
                    width = min(width, max(1, screen.width - 40))
                    height = min(height, max(1, screen.height - 80))
                    x = max(screen.x, min(x, screen.x + screen.width - width))
                    y = max(screen.y, min(y, screen.y + screen.height - height))
                window.resize(width, height)
                window.move(x, y)
                window.on_top = False
                self._compact_geometry = None
                if self._maximized:
                    window.maximize()
        elif action == "close":
            with self._lock:
                active = self.stop_run()["stopping"]
                already_requested = self._close_requested
                self._close_requested = True
                self._stop_event.set()
            if active and not already_requested:
                return {"action": "stopping-before-close"}
            window.destroy()
        elif action == "toggle-maximize":
            if self._maximized:
                window.restore()
            else:
                window.maximize()
            self._maximized = not self._maximized
        else:
            getattr(window, action)()
            if action == "maximize":
                self._maximized = True
            elif action == "restore":
                self._maximized = False
        return {"action": action}

    def _bind_window(self, window: Any, screens: list[Any] | None = None) -> None:
        self._window = window
        self._screens = list(screens or [])


def _saved_window_position(screens: list[Any]) -> tuple[int, int] | None:
    """Last position the window was left at, or None to let pywebview centre it.

    Coordinates are pywebview's logical pixels, which equal physical pixels at 100%
    display scaling. On a scaled display the comparison against physical monitor
    bounds can reject a still-valid position, which only costs a centred window.
    """
    try:
        saved = json.loads(engine.WINDOW_STATE_PATH.read_text(encoding="utf-8"))
        x, y = int(saved["x"]), int(saved["y"])
    except (OSError, ValueError, TypeError, KeyError):
        return None

    # A monitor that was unplugged or rearranged would strand the window off-screen,
    # and the frameless UI has no title bar Windows could drag back into view.
    on_a_live_monitor = any(
        screen.x <= x < screen.x + screen.width and screen.y <= y < screen.y + screen.height
        for screen in screens
    )
    return (x, y) if on_a_live_monitor else None


def _csv_text(value: object) -> object:
    # Rewards read like "+10 Mastery Points"; a leading = + - @ makes Excel treat the cell as a formula.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _save_window_position(x: int, y: int) -> None:
    engine.WINDOW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine.WINDOW_STATE_PATH.write_text(json.dumps({"x": x, "y": y}), encoding="utf-8")


def launch_webview(scheduled: bool = False, on_ready: Callable[[XynClaimAPI], None] | None = None) -> None:
    """Launch the bundled local UI. Importing this module never imports pywebview."""
    window_icon.set_app_identity()
    import webview

    api = XynClaimAPI()
    api._scheduled = scheduled
    index = APP_DIR / "ui" / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"Missing bundled UI: {index}")
    position = _saved_window_position(webview.screens)
    # WebView2 can retain a file:// document across quick restarts. The mtime keeps
    # release assets fresh without disabling its cache for normal use.
    document_url = f"{index.as_uri()}#v={index.stat().st_mtime_ns}"
    # The layout is designed at 1280x860; shrink only when the screen can't fit it.
    screen = next((screen for screen in webview.screens if position
                   and screen.x <= position[0] < screen.x + screen.width
                   and screen.y <= position[1] < screen.y + screen.height),
                  webview.screens[0] if webview.screens else None)
    width = min(1280, max(1, screen.width - 40)) if screen else 1280
    height = min(860, max(1, screen.height - 80)) if screen else 860
    if position and screen:
        position = (max(screen.x, min(position[0], screen.x + screen.width - width)),
                    max(screen.y, min(position[1], screen.y + screen.height - height)))
    window = webview.create_window(
        engine.APP_NAME,
        url=document_url,
        width=width,
        height=height,
        x=position[0] if position else None,
        y=position[1] if position else None,
        min_size=(min(480, width), min(60, height)),
        frameless=True,
        easy_drag=False,
    )
    api._bind_window(window, webview.screens)
    window.expose(
        api.get_state,
        api.start_run,
        api.stop_run,
        api.save_plan,
        api.save_notifications,
        api.save_hotkey,
        api.test_notification,
        api.reset_settings,
        api.consume_last_error,
        api.window_action,
        api.get_stats,
        api.get_slot_stats,
        api.export_stats_csv,
        api.get_streak,
        api.get_run,
        api.get_receipt,
        api.open_reports_folder,
        api.setup_checks,
        api.get_run_prefs,
        api.save_run_prefs,
        api.get_schedule,
        api.save_schedule,
        api.get_launch_info,
    )

    def closing() -> bool:
        with api._lock:
            active = api.stop_run()["stopping"]
            already_requested = api._close_requested
            api._close_requested = True
            api._stop_event.set()
        if active and not already_requested:
            # Do not call window.destroy from the worker thread. The focused UI can
            # ask to close again once its normal state poll reports the run stopped.
            return False
        return True

    # window.x/y are unreadable once the window is destroyed, so track moves as they happen.
    last_position = list(position) if position else None

    def moved(x: int, y: int) -> None:
        nonlocal last_position
        last_position = [x, y]

    icons = []

    def shown() -> None:
        hwnd = window.native.Handle.ToInt64()
        icons.extend(window_icon.set_window_icons(hwnd))
        window_icon.round_corners(hwnd)

    window.events.shown += shown
    window.events.closing += closing
    window.events.moved += moved
    try:
        try:
            api._register_hotkey(api._settings().hotkey)
        except (SettingsError, RuntimeError) as error:
            api._safe_log(str(error))
        try:
            webview.start((lambda: on_ready(api)) if on_ready else None,
                          gui="edgechromium", debug=False, private_mode=True)
        except TypeError:
            webview.start((lambda: on_ready(api)) if on_ready else None,
                          gui="edgechromium", debug=False)
    finally:
        window_icon.release_icons(icons)
        if api._hotkey_listener is not None:
            api._hotkey_listener.close()

    if last_position is not None:
        try:
            _save_window_position(*last_position)
        except OSError:
            pass  # remembering placement is a nicety, never a reason to fail shutdown


if __name__ == "__main__":
    launch_webview()
