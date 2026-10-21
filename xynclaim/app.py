"""XynClaim desktop entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from xynclaim import engine
from xynclaim import schedule
from xynclaim.settings import SettingsError, default_settings, load_settings
from xynclaim.bridge import XynClaimAPI, launch_webview


def scheduled_start(api: XynClaimAPI) -> None:
    try:
        if api._close_requested:
            return
        settings = load_settings(api.settings_path)
        if not settings.schedule_enabled:
            api._safe_log("Scheduled run skipped because automatic runs are off")
            return
        if settings.access_mode != "owner":
            api._safe_log("Scheduled run needs your own server; saved access mode is shared")
            return
        if engine.DBOGRunner.roblox_running():
            if settings.schedule_roblox_busy == "skip":
                api._safe_log("Scheduled run skipped because Roblox is already running")
                api.window_action("close")
                return
            with api._lock:
                if api._close_requested or (api._worker is not None and api._worker.is_alive()):
                    return
                api._scheduled_waiting = True
                api._status = "waiting"
            api._safe_log("Roblox is running; scheduled run will wait up to 2 hours")
            try:
                for _ in range(24):
                    if api._stop_event.wait(300):
                        api._safe_log("Scheduled run cancelled")
                        return
                    if not engine.DBOGRunner.roblox_running():
                        break
                else:
                    api._safe_log("Scheduled run gave up after 2 hours because Roblox stayed busy")
                    with api._lock:
                        api._close_requested = True
                    api.window_action("close")
                    return
            finally:
                with api._lock:
                    api._scheduled_waiting = False
                    if api._status in {"waiting", "stopping"}:
                        api._status = "idle"
        if api._close_requested or api._stop_event.is_set():
            return
        # Preferences can change during the two-hour wait.
        settings = load_settings(api.settings_path)
        if not settings.schedule_enabled or settings.access_mode != "owner":
            api._safe_log("Scheduled run cancelled because saved preferences changed")
            return
        api.start_run({"slot_count": settings.slot_count, "slots": list(settings.slots),
                       "access_mode": "owner", "close_when_done": settings.close_when_done,
                       "public_fallback": settings.public_fallback,
                       "scheduled_day1": settings.schedule_day1})
    except (OSError, RuntimeError, ValueError) as error:
        api._safe_log(f"Scheduled run could not start: {error}")


def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else argv
    if any(argument != "--scheduled" for argument in arguments):
        raise SystemExit("Usage: xynclaim_app.py [--scheduled]")
    scheduled = "--scheduled" in arguments
    engine.migrate_legacy_data()
    schedule_error = ""
    settings_valid = True
    try:
        settings = load_settings(engine.SETTINGS_PATH)
    except SettingsError as error:
        settings = default_settings()
        schedule_error = str(error)
        settings_valid = False
    if settings.schedule_enabled:
        app_path = (
            Path(sys.executable) if getattr(sys, "frozen", False)
            else Path(__file__).resolve().with_name("__main__.py")
        )
        try:
            schedule.sync_task(settings, app_path, frozen=bool(getattr(sys, "frozen", False)))
        except RuntimeError as error:
            schedule_error = f"Scheduled task could not be updated: {error}"

    def ready(api: XynClaimAPI) -> None:
        if schedule_error:
            api._safe_log(schedule_error)
        if scheduled and settings_valid:
            scheduled_start(api)

    launch_webview(scheduled=scheduled, on_ready=ready if scheduled or schedule_error else None)


if __name__ == "__main__":
    main()
