import json
from types import SimpleNamespace
from unittest import mock

import pytest

from xynclaim import engine as claimer
from test_core import bare_runner

def settings_with_level(level):
    return (
        '<roblox><int name="GraphicsQualityLevel">21</int>'
        f'<token name="SavedQualityLevel">{level}</token></roblox>'
    )


@pytest.mark.parametrize(
    ("xml", "expected"),
    [(settings_with_level(10), 10), (settings_with_level(0), 0), (settings_with_level(14), None),
     (settings_with_level("x"), None), ("<roblox/>", None), ("<broken", None)],
)
def test_read_saved_quality_level(tmp_path, xml, expected):
    path = tmp_path / "GlobalBasicSettings_13.xml"
    path.write_text(xml, encoding="utf-8")

    assert claimer.read_saved_quality_level(path) == expected


def test_missing_settings_file_reads_as_unknown(tmp_path):
    assert claimer.read_saved_quality_level(tmp_path / "missing.xml") is None


def graphics_run(run_environment, level, **attrs):
    claimer.ROBLOX_SETTINGS_PATH.write_text(settings_with_level(level), encoding="utf-8")
    runner = run_environment(**{"low_graphics": True, **attrs})
    runner.tap = mock.Mock()
    runner.require_focus = mock.Mock()
    runner.process_running = mock.Mock(return_value=True)
    return runner


def presses(runner):
    return [(c.args, c.kwargs) for c in runner.tap.call_args_list]


def test_low_graphics_presses_down_to_1_then_back_to_the_players_level(run_environment):
    runner = graphics_run(run_environment, 7)

    report = runner.run()

    assert not report.errors
    assert presses(runner) == [(("f10",), {"modifier": "shift"})] * 9 + [(("f10",), {})] * 6
    assert not claimer.GRAPHICS_RESTORE_PATH.exists()


def test_automatic_graphics_is_left_alone(run_environment):
    runner = graphics_run(run_environment, 0)

    runner.run()

    runner.tap.assert_not_called()


def test_a_failed_restore_leaves_a_note_and_the_next_run_raises_it_back(run_environment):
    runner = graphics_run(run_environment, 6)
    runner.require_focus.side_effect = claimer.FocusLost("focus denied")

    report = runner.run()

    assert any("Graphics restore failed" in error for error in report.errors)
    assert claimer.load_graphics_note() == 6

    # Roblox saved bar 1 at shutdown; the note still knows the real level, even with low graphics off.
    retry = graphics_run(run_environment, 1, low_graphics=False)
    assert not retry.run().errors
    assert presses(retry)[-5:] == [(("f10",), {})] * 5
    assert len(presses(retry)) == 9 + 5
    assert not claimer.GRAPHICS_RESTORE_PATH.exists()


def test_graphics_are_raised_before_roblox_closes(run_environment):
    runner = graphics_run(run_environment, 3, close_when_done=True)
    order = []
    runner.restore_graphics_in_game = mock.Mock(side_effect=lambda: order.append("restore"))
    runner.close_roblox = mock.Mock(side_effect=lambda: order.append("close"))

    runner.run()

    assert order == ["restore", "close"]


def test_stuck_roblox_pids_skips_windowed_and_still_loading_processes(monkeypatch):
    tasklist = (
        '"RobloxPlayerBeta.exe","101","Console","1","500,000 K"\n'
        '"RobloxPlayerBeta.exe","202","Console","1","400,000 K"\n'
        '"RobloxPlayerBeta.exe","303","Console","1","300,000 K"\n'
    )
    monkeypatch.setattr(claimer.subprocess, "run", mock.Mock(return_value=SimpleNamespace(stdout=tasklist)))
    monkeypatch.setattr(claimer, "_roblox_pids_with_visible_windows", lambda: {101})
    monkeypatch.setattr(claimer, "_process_age_seconds", {202: 90.0, 303: 4.0}.get)

    assert claimer.stuck_roblox_pids() == [202]


def test_ghost_recovery_closes_only_stuck_processes(run_environment, monkeypatch, fake_clock):
    runner = run_environment()
    runner.ensure_roblox_window = claimer.DBOGRunner.ensure_roblox_window.__get__(runner)
    runner.roblox_running = mock.Mock(return_value=True)
    monkeypatch.delenv("XYNCLAIM_ROBLOX_PID", raising=False)
    launch = mock.Mock()
    # No window until the fresh launch, like a ghost process that never drew one.
    monkeypatch.setattr(claimer, "find_roblox_window", lambda: runner.geometry if launch.called else None)
    monkeypatch.setattr(claimer, "stuck_roblox_pids", lambda: [202])
    monkeypatch.setattr(claimer, "launch_roblox_on_default_desktop", launch)
    kill = mock.Mock()
    monkeypatch.setattr(claimer.subprocess, "run", kill)

    assert runner.ensure_roblox_window() is False

    assert [call.args[0] for call in kill.call_args_list] == [["taskkill", "/F", "/PID", "202"]]


@pytest.mark.parametrize("already_running", [False, True])
def test_main_menu_timeout_mentions_sign_in_only_for_a_fresh_launch(already_running):
    runner = bare_runner()
    runner.wait_for_main_menu = mock.Mock(side_effect=TimeoutError("main_menu"))
    runner.screenshot = mock.Mock(return_value="screen")
    runner.prepare_menu = mock.Mock(side_effect=TimeoutError("game_menu"))

    with pytest.raises((RuntimeError, TimeoutError)) as raised:
        runner.finish_launch(already_running)

    assert ("sign in" in str(raised.value)) is not already_running


def _already_running_runner(visible_state):
    runner = bare_runner()
    runner.screenshot = mock.Mock(return_value="screen")
    runner.detector.matches.side_effect = lambda _screen, name: (name == visible_state, 0.9)
    runner.prepare_menu = mock.Mock()
    runner.click_transition = mock.Mock()
    runner.click_until_gone = mock.Mock()
    runner.wait_for_main_menu = mock.Mock()
    runner.click = mock.Mock()
    return runner


@pytest.mark.parametrize("visible_state", ["world", "daily_rewards", "game_menu", "travel_menu", None])
def test_already_open_off_main_menu_goes_there_through_travel(visible_state):
    runner = _already_running_runner(visible_state)

    runner.finish_launch(already_running=True)

    runner.prepare_menu.assert_called_once()
    assert runner.click_transition.call_args.args[:2] == claimer.MENU_TRAVEL_TAB
    assert runner.click_until_gone.call_args.args[:2] == claimer.TRAVEL_MAIN_MENU
    runner.wait_for_main_menu.assert_called_once()


def test_already_open_on_main_menu_skips_the_menu_route():
    runner = _already_running_runner("main_menu")

    runner.finish_launch(already_running=True)

    runner.prepare_menu.assert_not_called()
    runner.click_transition.assert_not_called()
    runner.wait_for_main_menu.assert_called_once()


def test_already_open_on_a_loading_screen_falls_back_to_waiting():
    runner = _already_running_runner(None)
    runner.prepare_menu.side_effect = TimeoutError("game_menu")

    runner.finish_launch(already_running=True)

    runner.click_transition.assert_not_called()
    runner.click_until_gone.assert_not_called()
    runner.wait_for_main_menu.assert_called_once()


def test_fresh_launch_never_presses_m():
    runner = _already_running_runner(None)
    runner.check_stopped = mock.Mock()

    with mock.patch.object(claimer.time, "sleep"), mock.patch.object(
        claimer.time, "monotonic", side_effect=[0.0, 30.0]
    ):
        runner.finish_launch(already_running=False)

    runner.prepare_menu.assert_not_called()
