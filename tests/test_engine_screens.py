from unittest import mock
from types import SimpleNamespace

import pytest

from xynclaim import engine as claimer
from test_core import bare_runner


REAL_WINDOW_PREPARE = claimer.prepare_roblox_window


def test_diagnostic_redacts_the_new_private_code_field_without_mutating_the_capture(engine_files, monkeypatch):
    geometry = claimer.RobloxGeometry(123, 0, 0, 1920, 1080)
    runner = bare_runner(geometry=geometry)
    runner.require_focus = mock.Mock()
    monkeypatch.setattr(claimer, "_client_geometry", mock.Mock(return_value=geometry))
    screen = claimer.np.full((1080, 1920, 3), 173, dtype=claimer.np.uint8)

    path = runner.save_diagnostic(screen, "join_rejected")
    redacted = claimer.cv2.imread(str(path))

    assert redacted is not None
    x, y = claimer.PRIVATE_CODE_FIELD
    assert not redacted[y - 25:y + 25, x - 250:x + 250].any()
    assert not redacted[405:605, 1375:1665].any()
    assert not redacted[0:115, 0:650].any()
    assert (redacted[900:1000, 900:1000] == 173).all()
    assert (screen == 173).all()
    assert path.parent == engine_files


def test_slot_points_follow_the_two_column_six_row_grid():
    assert [claimer.DBOGRunner.slot_point(slot) for slot in range(1, 13)] == [
        (715, 330), (1200, 330), (715, 452), (1200, 452),
        (715, 574), (1200, 574), (715, 696), (1200, 696),
        (715, 818), (1200, 818), (715, 940), (1200, 940),
    ]


def test_owner_private_join_retries_join_once_without_generating_or_typing_a_code(fake_clock):
    runner = bare_runner()
    runner.open_slot_select = mock.Mock()
    runner.click_transition = mock.Mock()
    runner.click = mock.Mock()
    runner.require_focus = mock.Mock()
    runner.input = mock.Mock()
    runner.tap = mock.Mock()
    runner.wait_until_gone = mock.Mock(side_effect=[TimeoutError("Enter dropped"), None])

    runner.open_private_slot(2)

    assert runner.click_transition.call_args_list == [
        mock.call(*runner.slot_point(2), "server_menu", 0.8, "slot_select"),
        mock.call(*claimer.DESTINATION_NORTH_EARTH, "private_panel", 8.0, "server_menu"),
    ]
    field, join = mock.call(*claimer.PRIVATE_CODE_FIELD), mock.call(*claimer.PRIVATE_JOIN_BUTTON)
    assert runner.click.call_args_list == [field, join, field, join]
    runner.tap.assert_not_called()
    assert runner.wait_until_gone.call_args_list == [mock.call("private_panel", 15.0)] * 2
    assert fake_clock[0] == pytest.approx(0.5 + 0.4 + 0.4)
    runner.input.replace_text.assert_not_called()


def test_slow_destination_list_after_the_last_slot_click_is_waited_for_without_clicking():
    runner = bare_runner()
    runner.open_slot_select = mock.Mock()
    runner.click_transition = mock.Mock(side_effect=[TimeoutError("blank while loading"), None])
    runner.wait_for = mock.Mock(return_value=("destinations", 0.9))
    runner.click = mock.Mock(side_effect=claimer.RunStopped("stop after destination"))

    with pytest.raises(claimer.RunStopped):
        runner.open_private_slot(9)

    runner.wait_for.assert_called_once_with("server_menu", claimer.TRANSITION_GRACE_SECONDS)
    assert runner.click_transition.call_args_list[1] == mock.call(
        *claimer.DESTINATION_NORTH_EARTH, "private_panel", 8.0, "server_menu"
    )


@pytest.mark.parametrize(
    ("on_slot_select", "expected"),
    [(True, claimer.SlotUnavailable), (False, claimer.UnsafeGameState)],
)
def test_slot_pick_still_fails_when_the_destination_list_never_loads(on_slot_select, expected):
    runner = bare_runner()
    runner.open_slot_select = mock.Mock()
    runner.click_transition = mock.Mock(side_effect=TimeoutError("no destinations"))
    runner.wait_for = mock.Mock(side_effect=TimeoutError("still nothing"))
    runner.screenshot = mock.Mock(return_value="frame")
    runner.detector.matches.return_value = (on_slot_select, 0.9)

    with pytest.raises(expected):
        runner.open_private_slot(9)

    assert runner.click_transition.call_count == 1


def test_first_click_waits_for_a_source_menu_that_is_still_fading_in():
    runner = bare_runner()
    runner.screenshot = mock.Mock(return_value="fading slot list")
    runner.detector.matches.return_value = (False, 0.4)
    runner.wait_for_either = mock.Mock(return_value=("slot_select", "slot list", 0.9))
    runner.wait_for = mock.Mock(return_value=("destinations", 0.9))
    runner.click = mock.Mock()

    assert runner.click_transition(715, 330, "server_menu", 0.8, "slot_select") == ("destinations", 0.9)

    runner.wait_for_either.assert_called_once_with("server_menu", "slot_select", claimer.SOURCE_SETTLE_SECONDS)
    runner.click.assert_called_once_with(715, 330, 1)


def test_flash_after_the_preview_click_waits_for_the_slot_list_and_clicks_again(fake_clock):
    runner = bare_runner()
    frames = iter(["slot list", "flash", "flash", "slot list", "destinations"])
    runner.screenshot = mock.Mock(side_effect=lambda: next(frames))
    runner.detector.matches.side_effect = lambda frame, name: (
        (frame, name) in {("slot list", "slot_select"), ("destinations", "server_menu")}, 0.9
    )
    runner.wait_for = mock.Mock(side_effect=[TimeoutError("preview only"), ("destinations", 0.9)])
    runner.click = mock.Mock()
    runner.log = mock.Mock()

    assert runner.click_transition(715, 330, "server_menu", 0.8, "slot_select") == ("destinations", 0.9)

    assert runner.click.call_count == 2


def test_wait_for_either_returns_whichever_screen_appears_first(fake_clock):
    runner = bare_runner()
    frames = iter(["blank", "destinations"])
    runner.screenshot = mock.Mock(side_effect=lambda: next(frames))
    runner.detector.matches.side_effect = lambda frame, name: (frame == "destinations" and name == "server_menu", 0.9)

    assert runner.wait_for_either("slot_select", "server_menu", 2.0) == ("server_menu", "destinations", 0.9)


def test_wait_for_either_gives_up_without_a_frame(fake_clock):
    runner = bare_runner()
    runner.screenshot = mock.Mock(return_value="blank")
    runner.detector.matches.return_value = (False, 0.1)

    assert runner.wait_for_either("slot_select", "server_menu", 2.0) == (None, None, 0.0)


def test_first_click_is_still_refused_when_the_source_never_appears():
    runner = bare_runner()
    runner.screenshot = mock.Mock(return_value="unknown")
    runner.detector.matches.return_value = (False, 0.1)
    runner.wait_for_either = mock.Mock(return_value=(None, None, 0.0))
    runner.click = mock.Mock()

    with pytest.raises(claimer.UnsafeGameState, match="before click 1"):
        runner.click_transition(715, 330, "server_menu", 0.8, "slot_select")

    runner.click.assert_not_called()


def test_daily_settling_rejects_a_vanished_panel(fake_clock):
    runner = bare_runner()
    runner.detect_claim_day = mock.Mock(side_effect=lambda frame: 5 if frame == "daily" else 1)
    runner.screenshot = mock.Mock(return_value="blank")
    runner.detector.matches.side_effect = lambda frame, _anchor: (frame == "daily", 0.9)

    with pytest.raises(claimer.UnsafeGameState, match="disappeared"):
        runner.settled_daily_screen("daily")


def test_daily_settling_never_accepts_an_unstable_timeout(fake_clock):
    runner = bare_runner()
    runner.detect_claim_day = mock.Mock(side_effect=lambda _frame: 1 + runner.detect_claim_day.call_count % 2)
    runner.screenshot = mock.Mock(return_value="daily")
    runner.detector.matches.return_value = (True, 0.9)

    with pytest.raises(TimeoutError, match="settle"):
        runner.settled_daily_screen("daily")


def test_load_game_retry_polls_unknown_frames_without_clicking(fake_clock):
    runner = bare_runner()
    runner.screenshot = mock.Mock(side_effect=["main", "unknown", "unknown", "slots"])
    runner.click = mock.Mock()
    runner.detector.matches.side_effect = lambda frame, anchor: (
        (frame, anchor) in {("main", "main_menu"), ("slots", "slot_select")}, 0.9,
    )

    def wait(*_args):
        fake_clock[0] += 6
        raise TimeoutError("transition pending")

    runner.wait_for = mock.Mock(side_effect=wait)
    runner.open_slot_select()

    runner.click.assert_called_once_with(*claimer.PLAY_BUTTON)


def test_settled_daily_screen_skips_unreadable_fade_in_frames(fake_clock):
    runner = bare_runner()
    runner.detector.matches.return_value = (True, 0.9)
    runner.detect_claim_day = mock.Mock(side_effect=[
        claimer.UnsafeGameState("fading"), claimer.UnsafeGameState("fading"), 2, 2, 2, 2,
    ])
    runner.screenshot = mock.Mock(side_effect=["fade2", "readable1", "readable2", "readable3", "settled"])
    runner.click = mock.Mock()

    assert runner.settled_daily_screen("fade1") == "settled"

    assert runner.detect_claim_day.call_args_list == [
        mock.call(frame) for frame in ("fade1", "fade2", "readable1", "readable2", "readable3", "settled")
    ]
    assert fake_clock[0] >= 1.0
    runner.click.assert_not_called()


def test_daily_settling_times_out_when_every_frame_is_unreadable(fake_clock):
    runner = bare_runner()
    runner.detector.matches.return_value = (True, 0.9)
    runner.detect_claim_day = mock.Mock(side_effect=claimer.UnsafeGameState("unreadable"))
    runner.screenshot = mock.Mock(return_value="fade")
    runner.click = mock.Mock()

    with pytest.raises(TimeoutError, match="settle"):
        runner.settled_daily_screen("fade")

    runner.click.assert_not_called()


def test_return_to_main_menu_closes_daily_before_opening_travel():
    runner = bare_runner()
    runner.screenshot = mock.Mock(side_effect=["daily", "world"])
    runner.detector.matches.side_effect = lambda frame, name: ((frame, name) == ("daily", "daily_rewards"), 0.9)
    calls = mock.Mock()
    for method in ("click_until_gone", "tap", "wait_for", "click_transition", "wait_for_main_menu"):
        setattr(runner, method, getattr(calls, method))

    runner.return_to_main_menu()

    assert calls.mock_calls == [
        mock.call.click_until_gone(*claimer.DAILY_BACK_BUTTON, "daily_rewards", 5.0),
        mock.call.tap("m"),
        mock.call.wait_for("game_menu", 5.0),
        mock.call.click_transition(*claimer.MENU_TRAVEL_TAB, "travel_menu", 5.0, "game_menu"),
        mock.call.click_until_gone(*claimer.TRAVEL_MAIN_MENU, "travel_menu", 8.0),
        mock.call.wait_for_main_menu(claimer.MAIN_MENU_TIMEOUT),
    ]


def test_prepare_menu_does_not_close_an_already_open_menu():
    runner = bare_runner()
    runner.screenshot = mock.Mock(return_value="menu")
    runner.detector.matches.side_effect = lambda _frame, name: (name == "game_menu", 0.9)
    runner.click_until_gone = mock.Mock()
    runner.tap = mock.Mock()
    runner.wait_for = mock.Mock()

    runner.prepare_menu()

    runner.click_until_gone.assert_not_called()
    runner.tap.assert_not_called()
    runner.wait_for.assert_called_once_with("game_menu", 5.0)


def test_originally_windowed_client_exits_fullscreen_before_restoring_bounds(run_environment, monkeypatch, fake_clock):
    runner = run_environment()
    fullscreen = runner.geometry
    windowed = claimer.RobloxGeometry(100, 108, 130, 1600, 900)
    current = [fullscreen]
    monitor = SimpleNamespace(rcMonitor=SimpleNamespace(left=0, top=0, right=1920, bottom=1080))
    monkeypatch.setattr(claimer, "_monitor_info_for_window", mock.Mock(return_value=monitor))
    monkeypatch.setattr(claimer, "_client_geometry", lambda _hwnd: current[0])
    controls = mock.Mock()
    controls.tap.side_effect = lambda *_args: current.__setitem__(0, windowed)
    user32 = claimer.ctypes.windll.user32
    user32.SetWindowPos.return_value = True

    def bounds(_hwnd, pointer):
        rect = pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = 100, 100, 1716, 1038
        return True

    user32.GetWindowRect.side_effect = bounds
    restore = claimer.WindowRestore(False, (100, 100, 1616, 938))
    claimer.restore_roblox_window(restore, fullscreen, controls, runner.require_focus)

    controls.tap.assert_called_once_with("f11", runner.require_focus)
    user32.SetWindowPos.assert_called_once_with(100, None, 100, 100, 1616, 938, 0x0004 | 0x0010)


def test_already_restored_fullscreen_client_needs_no_toggle(run_environment, monkeypatch):
    runner = run_environment()
    monitor = SimpleNamespace(rcMonitor=SimpleNamespace(left=0, top=0, right=1920, bottom=1080))
    monkeypatch.setattr(claimer, "_monitor_info_for_window", mock.Mock(return_value=monitor))
    monkeypatch.setattr(claimer, "_client_geometry", mock.Mock(return_value=runner.geometry))
    controls = mock.Mock()

    claimer.restore_roblox_window(
        claimer.WindowRestore(True, (0, 0, 1920, 1080)), runner.geometry, controls, runner.require_focus,
    )

    controls.tap.assert_not_called()
    claimer.ctypes.windll.user32.SetWindowPos.assert_not_called()


def test_fullscreen_restore_moves_back_to_the_original_monitor(run_environment, monkeypatch, fake_clock):
    runner = run_environment()
    current = [claimer.RobloxGeometry(100, 2560, 0, 1920, 1080)]
    controls = mock.Mock()

    def monitor_info(_hwnd):
        left, width, height = (2560, 1920, 1080) if current[0].left >= 2560 else (0, 2560, 1440)
        return SimpleNamespace(rcMonitor=SimpleNamespace(left=left, top=0, right=left + width, bottom=height))

    def toggle(*_args):
        monitor = monitor_info(100).rcMonitor
        if current[0].width == monitor.right - monitor.left:
            current[0] = claimer.RobloxGeometry(100, monitor.left + 8, 30, 1600, 900)
        else:
            current[0] = claimer.RobloxGeometry(100, monitor.left, 0, monitor.right - monitor.left, monitor.bottom)

    def set_bounds(_hwnd, _after, x, y, width, height, _flags):
        current[0] = claimer.RobloxGeometry(100, x + 8, y + 30, width - 16, height - 38)
        return True

    monkeypatch.setattr(claimer, "_monitor_info_for_window", monitor_info)
    monkeypatch.setattr(claimer, "_client_geometry", lambda _hwnd: current[0])
    claimer.ctypes.windll.user32.SetWindowPos.side_effect = set_bounds
    controls.tap.side_effect = toggle

    claimer.restore_roblox_window(
        claimer.WindowRestore(True, (0, 0, 2560, 1440)), current[0], controls, runner.require_focus,
    )

    assert current[0] == claimer.RobloxGeometry(100, 0, 0, 2560, 1440)
    assert controls.tap.call_count == 2


def test_cancelled_window_setup_uses_a_focus_only_rollback_guard(run_environment, monkeypatch):
    runner = run_environment()
    restore = claimer.WindowRestore(False, (100, 100, 1616, 938))
    monkeypatch.setattr(claimer, "_window_restore_snapshot", mock.Mock(return_value=restore))
    rollback = mock.Mock()
    monkeypatch.setattr(claimer, "restore_roblox_window", rollback)
    normal_guard = mock.Mock(side_effect=claimer.RunStopped())
    cleanup_guard = mock.Mock()

    with pytest.raises(claimer.RunStopped):
        REAL_WINDOW_PREPARE(runner.geometry, runner.input, normal_guard, rollback_guard=cleanup_guard)

    rollback.assert_called_once_with(restore, runner.geometry, runner.input, cleanup_guard)


def test_ctrl_c_during_window_setup_still_rolls_back(run_environment, monkeypatch):
    runner = run_environment()
    restore = claimer.WindowRestore(False, (100, 100, 1616, 938))
    monkeypatch.setattr(claimer, "_window_restore_snapshot", mock.Mock(return_value=restore))
    monkeypatch.setattr(claimer, "_prepare_roblox_window_unchecked", mock.Mock(side_effect=KeyboardInterrupt()))
    rollback = mock.Mock()
    monkeypatch.setattr(claimer, "restore_roblox_window", rollback)
    cleanup_guard = mock.Mock()

    with pytest.raises(KeyboardInterrupt):
        REAL_WINDOW_PREPARE(runner.geometry, runner.input, mock.Mock(), rollback_guard=cleanup_guard)

    rollback.assert_called_once_with(restore, runner.geometry, runner.input, cleanup_guard)
