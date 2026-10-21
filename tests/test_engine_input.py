import threading
from unittest import mock

import pytest

from xynclaim import engine as claimer
from test_core import bare_runner


@pytest.mark.parametrize("method, args", [("click", claimer.CLAIM_BUTTON), ("tap", ("m",))])
def test_stopped_normal_input_is_never_dispatched(method, args):
    runner = bare_runner(stop_event=threading.Event(), geometry=None, input=mock.Mock())
    runner.stop_event.set()
    runner.require_focus = mock.Mock()

    with pytest.raises(claimer.RunStopped):
        getattr(runner, method)(*args)

    getattr(runner.input, method).assert_not_called()


@pytest.mark.parametrize("ambiguous", [False, True])
def test_pending_claim_is_cleared_only_when_button_input_is_provably_unsent(engine_files, monkeypatch, fake_clock, ambiguous):
    claimer.update_claim_state(1, 4)
    runner = bare_runner(stop_event=threading.Event(), geometry=None, input=mock.Mock())
    runner.mark_pending_claim = claimer.mark_pending_claim
    runner.open_private_slot = mock.Mock()
    runner.wait_for_daily_or_world = mock.Mock(return_value=("daily", "daily"))
    runner.tap = mock.Mock()
    runner.detect_claim_day = mock.Mock(return_value=5)
    runner.read_claim_button = mock.Mock(return_value=5)
    runner.read_streak = mock.Mock(return_value=4)
    runner.screenshot = mock.Mock(return_value="daily")
    runner.detector.matches.side_effect = lambda _frame, name: (name == "daily_rewards", 0.9)
    runner.require_focus = mock.Mock()

    def click(_x, _y, _guard, _clicks, before_button_send=None):
        if ambiguous and before_button_send is not None:
            before_button_send()
        raise claimer.FocusLost("input interrupted")

    runner.input.click.side_effect = click
    with pytest.raises(claimer.FocusLost):
        runner.process_slot(1)

    saved = claimer.load_claim_state()["1"]
    assert saved["claimed_day"] == 4 and saved["next_day"] == 5
    assert ("pending_claim" in saved) is ambiguous


def direct_input(geometry):
    controls = claimer.DirectInput.__new__(claimer.DirectInput)
    controls.geometry = geometry
    controls.user32 = mock.Mock()
    controls.user32.SetCursorPos.return_value = True
    controls.user32.GetSystemMetrics.side_effect = {76: 0, 77: 0, 78: 1920, 79: 1080}.__getitem__
    return controls


def test_stop_during_cursor_setup_prevents_the_button_packet(run_environment, monkeypatch, fake_clock):
    runner = run_environment()
    controls = direct_input(runner.geometry)
    runner.input = controls
    runner.require_focus = mock.Mock()
    monkeypatch.setattr(claimer, "_client_geometry", mock.Mock(return_value=runner.geometry))
    monkeypatch.setattr(claimer, "process_name_at_screen_point", mock.Mock(return_value="RobloxPlayerBeta.exe"))
    flags = []

    def send(count, events, _size):
        flags.extend(events[index].mi.dwFlags for index in range(count))
        if len(flags) == 4:
            runner.stop_event.set()
        return count

    controls.user32.SendInput.side_effect = send
    with pytest.raises(claimer.RunStopped):
        runner.click(*claimer.CLAIM_BUTTON)

    assert not any(flag & controls.MOUSEEVENTF_LEFTDOWN for flag in flags)


def test_cleanup_click_remains_available_after_stop(run_environment, monkeypatch):
    runner = run_environment()
    runner.stop_event.set()
    runner.require_focus = mock.Mock()
    monkeypatch.setattr(claimer, "process_name_at_screen_point", mock.Mock(return_value="RobloxPlayerBeta.exe"))

    runner.click(*claimer.PRIVATE_CODE_FIELD, cleanup=True)

    runner.input.click.assert_called_once()
    runner.input.click.call_args.args[2]()


def test_partial_button_packet_releases_the_button_without_pressing_again(run_environment, monkeypatch, fake_clock):
    runner = run_environment()
    controls = direct_input(runner.geometry)
    monkeypatch.setattr(claimer, "_client_geometry", mock.Mock(return_value=runner.geometry))
    flags = []

    def send(count, events, _size):
        packet = [events[index].mi.dwFlags for index in range(count)]
        flags.append(packet)
        return 1 if count == 2 else count

    controls.user32.SendInput.side_effect = send
    with pytest.raises(RuntimeError, match="1/2"):
        controls.click(*claimer.CLAIM_BUTTON, mock.Mock())

    assert flags[-1] == [controls.MOUSEEVENTF_LEFTUP]
    assert sum(bool(flag & controls.MOUSEEVENTF_LEFTDOWN) for packet in flags for flag in packet) == 1


@pytest.mark.parametrize("failed_packet", [1, 5])
def test_real_input_boundary_controls_pending_intent_recovery(run_environment, monkeypatch, fake_clock, failed_packet):
    claimer.update_claim_state(1, 4)
    runner = run_environment()
    runner.process_slot = claimer.DBOGRunner.process_slot.__get__(runner)
    runner.mark_pending_claim = claimer.mark_pending_claim
    runner.open_private_slot = mock.Mock()
    runner.wait_for_daily_or_world = mock.Mock(return_value=("daily", "daily"))
    runner.tap = mock.Mock()
    runner.detect_claim_day = mock.Mock(return_value=5)
    runner.read_claim_button = mock.Mock(return_value=5)
    runner.read_streak = mock.Mock(return_value=4)
    runner.detector.matches.side_effect = lambda _frame, name: (name == "daily_rewards", 0.9)
    runner.require_focus = mock.Mock()
    controls = direct_input(runner.geometry)
    runner.input = controls
    monkeypatch.setattr(claimer, "_client_geometry", mock.Mock(return_value=runner.geometry))
    monkeypatch.setattr(claimer, "process_name_at_screen_point", mock.Mock(return_value="RobloxPlayerBeta.exe"))

    def send(count, _events, _size):
        return count - 1 if controls.user32.SendInput.call_count == failed_packet else count

    controls.user32.SendInput.side_effect = send
    with pytest.raises(RuntimeError, match="Windows accepted only"):
        runner.process_slot(1)

    saved = claimer.load_claim_state()["1"]
    assert saved["claimed_day"] == 4
    assert ("pending_claim" in saved) is (failed_packet == 5)


@pytest.mark.parametrize("retry_day", [None, 6])
def test_claim_timeout_preserves_intent_without_a_confirmation_click(engine_files, fake_clock, retry_day):
    claimer.update_claim_state(1, 4)
    runner = bare_runner()
    runner.mark_pending_claim = claimer.mark_pending_claim
    runner.open_private_slot = mock.Mock()
    runner.wait_for_daily_or_world = mock.Mock(return_value=("daily", "daily"))
    runner.tap = mock.Mock()
    runner.detect_claim_day = mock.Mock(return_value=5)
    runner.read_claim_button = mock.Mock(side_effect=[5, retry_day])
    runner.read_streak = mock.Mock(return_value=4)
    runner.screenshot = mock.Mock(return_value="daily")
    runner.detector.matches.side_effect = lambda _frame, name: (name == "daily_rewards", 0.9)
    runner.click = mock.Mock(side_effect=lambda *_args, **kwargs: kwargs["before_button_send"]())
    with pytest.raises(claimer.UnsafeGameState, match="never advanced"):
        runner.process_slot(1)

    assert runner.click.call_count == 1
    saved = claimer.load_claim_state()["1"]
    assert saved["claimed_day"] == 4 and claimer.pending_claim_day(saved) == 5


def test_click_claim_button_does_not_reclick_when_button_reads_claimed(engine_files, fake_clock):
    claimer.update_claim_state(1, 4)
    runner = bare_runner()
    runner.mark_pending_claim = claimer.mark_pending_claim
    runner.screenshot = mock.Mock(return_value="before")
    runner.detector.matches.return_value = (True, 0.9)
    # Simulate an accepted button with unreadable confirmation OCR.
    runner.read_streak = mock.Mock(side_effect=lambda frame: 4 if frame == "before" else None)
    runner.read_claim_button = mock.Mock(return_value=None)

    def send_claim(*_args, before_button_send):
        before_button_send()
        runner.screenshot.return_value = "claimed panel"

    runner.click = mock.Mock(side_effect=send_claim)

    with pytest.raises(claimer.UnsafeGameState, match="never advanced"):
        runner.click_claim_button(1, 5)

    runner.click.assert_called_once_with(*claimer.CLAIM_BUTTON, before_button_send=mock.ANY)
    runner.read_claim_button.assert_called_once_with("claimed panel")
    saved = claimer.load_claim_state()["1"]
    assert saved["claimed_day"] == 4 and claimer.pending_claim_day(saved) == 5


def test_click_claim_button_retries_once_only_while_same_day_is_offered(engine_files, fake_clock):
    runner = bare_runner()
    runner.mark_pending_claim = claimer.mark_pending_claim
    runner.screenshot = mock.Mock(return_value="daily")
    runner.detector.matches.return_value = (True, 0.9)
    runner.read_streak = mock.Mock(return_value=4)
    runner.read_claim_button = mock.Mock(return_value=5)

    def send(*_args, before_button_send=None):
        if before_button_send is not None:
            before_button_send()

    runner.click = mock.Mock(side_effect=send)

    with pytest.raises(claimer.UnsafeGameState, match="never advanced"):
        runner.click_claim_button(1, 5)

    assert runner.click.call_args_list == [
        mock.call(*claimer.CLAIM_BUTTON, before_button_send=mock.ANY),
        mock.call(*claimer.CLAIM_BUTTON),
    ]
    runner.read_claim_button.assert_called_once_with("daily")
    assert claimer.pending_claim_day(claimer.load_claim_state()["1"]) == 5


def test_navigation_still_retries_a_recognized_source():
    runner = bare_runner()
    runner.screenshot = mock.Mock(return_value="travel menu")
    runner.detector.matches.return_value = (True, 0.9)
    runner.click = mock.Mock()
    runner.wait_until_gone = mock.Mock(side_effect=[TimeoutError("menu stayed visible"), None])

    assert runner.click_until_gone(*claimer.TRAVEL_MAIN_MENU, "travel_menu", 1) is True
    assert runner.click.call_args_list == [mock.call(*claimer.TRAVEL_MAIN_MENU, 1)] * 2
