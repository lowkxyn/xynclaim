import threading
from unittest import mock

import pytest

from xynclaim import engine as claimer
from xynclaim.bridge import XynClaimAPI

ROBLOX = 100
OTHER_APP = 200


class FakeClock:
    """Stands in for time.monotonic/time.sleep so pauses take no real time."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def focus_runner(foreground, armed=True, stop_event=None):
    runner = object.__new__(claimer.DBOGRunner)
    runner.geometry = claimer.RobloxGeometry(hwnd=ROBLOX, left=0, top=0, width=1920, height=1080)
    runner.stop_event = stop_event or threading.Event()
    runner.logs = []
    runner.log = runner.logs.append
    runner._focus_stop_armed = armed
    runner._paused_seconds = 0.0
    user32 = mock.MagicMock()
    user32.GetForegroundWindow.side_effect = foreground
    return runner, user32


def run_guard(runner, user32, clock, focus_result=True):
    with mock.patch.object(claimer.ctypes, "windll") as windll, mock.patch.object(
        claimer.time, "monotonic", clock.monotonic
    ), mock.patch.object(claimer.time, "sleep", clock.sleep), mock.patch.object(
        claimer, "focus_roblox", return_value=focus_result
    ) as focus:
        windll.user32 = user32
        runner.focus_calls = focus
        runner.normal_input_guard()
    return focus


def test_tabbing_out_stops_the_run_and_never_steals_focus():
    clock = FakeClock()
    runner, user32 = focus_runner(lambda: OTHER_APP)

    with pytest.raises(claimer.FocusLost, match="switched away from Roblox"):
        run_guard(runner, user32, clock)
    runner.focus_calls.assert_not_called()
    assert clock.now == 1000.0


def test_stop_request_wins_over_a_tab_out():
    clock = FakeClock()
    stop_event = threading.Event()
    stop_event.set()
    runner, user32 = focus_runner(lambda: OTHER_APP, stop_event=stop_event)

    with pytest.raises(claimer.RunStopped):
        run_guard(runner, user32, clock)


def test_roblox_in_front_passes_the_guard():
    clock = FakeClock()
    runner, user32 = focus_runner(lambda: ROBLOX)

    focus = run_guard(runner, user32, clock)

    focus.assert_not_called()


def test_unarmed_runs_keep_pulling_roblox_to_the_front():
    # Setup and cleanup steps still bring Roblox to the front.
    clock = FakeClock()
    runner, user32 = focus_runner(lambda: OTHER_APP, armed=False)

    focus = run_guard(runner, user32, clock, focus_result=True)

    focus.assert_called_once_with(ROBLOX)


@pytest.fixture
def api(tmp_path):
    return XynClaimAPI(tmp_path / "settings.json")


def test_pause_status_round_trip(api):
    api._status = "joining"
    api._set_paused(True)
    assert api.get_state()["status"] == "paused"
    assert "Click back into Roblox" in api.get_state()["paused_reason"]
    api._set_paused(False)
    assert api.get_state()["status"] == "joining"
    assert api.get_state()["paused_reason"] == ""


def test_stop_during_pause_is_not_overwritten_by_resume(api):
    api._status = "claiming"
    api._set_paused(True)
    api._status = "stopping"
    api._set_paused(False)
    assert api._status == "stopping"


def test_hotkey_when_idle_asks_the_ui_to_start_once(api):
    api.on_hotkey()
    assert api.get_state()["hotkey_start_requested"] is True
    assert api.get_state()["hotkey_start_requested"] is False


def test_hotkey_during_a_run_stops_it(api):
    worker = mock.MagicMock()
    worker.is_alive.return_value = True
    api._worker = worker
    api._status = "claiming"

    api.on_hotkey()

    assert api._stop_event.is_set()
    assert api._status == "stopping"
    assert api._hotkey_start_requested is False


def click_input(make_guard):
    direct = claimer.DirectInput.__new__(claimer.DirectInput)
    direct.geometry = claimer.RobloxGeometry(hwnd=ROBLOX, left=0, top=0, width=1920, height=1080)
    direct._aim = mock.MagicMock()
    direct._send_mouse_events = mock.MagicMock()
    with mock.patch.object(claimer.ctypes, "windll"), mock.patch.object(
        claimer, "focus_roblox", return_value=True
    ), mock.patch.object(claimer, "_client_geometry", return_value=direct.geometry), mock.patch.object(
        claimer.time, "sleep"
    ):
        direct.click(500, 400, make_guard(direct))
    return direct


def test_click_aims_again_when_a_pause_happened_right_before_the_press():
    def make_guard(direct):
        calls = []

        def guard():
            calls.append(None)
            # The pre-press check (third call) is where the user tabbed out and came back.
            if len(calls) == 3:
                direct.pauses += 1
        return guard

    direct = click_input(make_guard)

    assert direct._aim.call_count == 2
    assert direct._send_mouse_events.call_count == 1  # one press, after the second aim


def test_click_without_a_pause_aims_once():
    direct = click_input(lambda direct: lambda: None)
    assert direct._aim.call_count == 1
    assert direct._send_mouse_events.call_count == 1


def test_manual_and_scheduled_runs_both_stop_when_you_tab_out(tmp_path):
    from dataclasses import replace
    from xynclaim.settings import default_settings

    for scheduled in (True, False):
        api = XynClaimAPI(tmp_path / f"settings-{scheduled}.json")
        api._scheduled = scheduled
        runner = mock.MagicMock()
        runner.run.return_value = claimer.RunReport("test", "start", "finish", 1.0, "owner", "", 1, [], [])
        with mock.patch("xynclaim.bridge.load_settings", return_value=replace(default_settings())), mock.patch.object(
            claimer, "load_claim_state", return_value={}
        ), mock.patch.object(claimer, "DBOGRunner", return_value=runner) as runner_type:
            api.start_run({"slot_count": 1, "slots": [1], "close_when_done": False, "public_fallback": False})
            api._worker.join(2)
        assert runner_type.call_args.kwargs.get("stop_on_focus_loss", True) is True
    import inspect
    assert inspect.signature(claimer.DBOGRunner).parameters["stop_on_focus_loss"].default is True
