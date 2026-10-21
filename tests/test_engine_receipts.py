from pathlib import Path
from unittest import mock

import pytest

from xynclaim import engine as claimer
from test_core import bare_runner


def accepted_claim_runner(make_runner, monkeypatch):
    runner = make_runner()
    runner.process_slot = claimer.DBOGRunner.process_slot.__get__(runner)
    runner.open_private_slot = mock.Mock()
    runner.wait_for_daily_or_world = mock.Mock(return_value=("daily", "daily"))
    runner.tap = mock.Mock()
    runner.detect_claim_day = mock.Mock(return_value=5)
    runner.click_claim_button = mock.Mock(return_value="receipt")
    runner.screenshot.return_value = "diagnostic"
    runner.read_receipt_rewards = mock.Mock(return_value=(["31,250 Zeni"], "receipt"))
    runner.save_receipt_region = mock.Mock(return_value=Path("receipt.png"))
    monkeypatch.setattr(claimer, "load_claim_state", mock.Mock(return_value={}))
    monkeypatch.setattr(claimer, "update_claim_state", mock.Mock())
    return runner


@pytest.mark.parametrize("failure", ["stop", "focus", "image", "state", "verified_state", "interrupt"])
def test_accepted_claim_survives_receipt_and_state_failures(run_environment, monkeypatch, fake_clock, failure):
    runner = accepted_claim_runner(run_environment, monkeypatch)
    if failure == "stop":
        runner.read_receipt_rewards.side_effect = claimer.RunStopped()
        runner.screenshot.side_effect = claimer.RunStopped()
    elif failure == "focus":
        runner.read_receipt_rewards.side_effect = claimer.FocusLost("focus denied")
        runner.screenshot.side_effect = claimer.FocusLost("focus denied")
    elif failure == "image":
        runner.save_receipt_region.side_effect = OSError("disk full")
    elif failure == "state":
        claimer.update_claim_state.side_effect = OSError("state disk full")
    elif failure == "verified_state":
        claimer.update_claim_state.side_effect = [None, OSError("state disk full")]
    else:
        runner.read_receipt_rewards.side_effect = KeyboardInterrupt()
        runner.screenshot.side_effect = KeyboardInterrupt()

    try:
        report = runner.run()
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C escaped after the claim was accepted")

    assert report.slots[0].status.startswith("claimed")
    assert report.slots[0].day == 5
    assert report.slots[1].status == "not_attempted"
    assert report.errors
    runner.click_claim_button.assert_called_once_with(1, 5, mock.ANY)
    # run() may try a failure diagnostic after the result was safely recorded.
    runner.screenshot.assert_called_once_with()
    if failure == "state":
        runner.read_receipt_rewards.assert_not_called()
    else:
        runner.read_receipt_rewards.assert_called_once_with("receipt")
    if failure in {"stop", "focus", "interrupt"}:
        runner.save_diagnostic.assert_not_called()
    else:
        runner.save_diagnostic.assert_called_once_with("diagnostic", "slot_1_post_result_navigation")
    claimer.save_report.assert_called_once()


def test_held_navigation_failure_stops_later_slots(run_environment):
    runner = run_environment()
    runner.process_slot.side_effect = claimer.SlotHeld("pending intent")
    runner.return_to_main_menu.side_effect = claimer.FocusLost("focus denied")

    report = runner.run()

    assert [result.status for result in report.slots] == ["held", "not_attempted"]
    assert runner.process_slot.call_count == 1
    assert any("held" in error.lower() and "focus denied" in error for error in report.errors)


def test_claim_day_is_rechecked_on_the_actual_preclick_frame(run_environment, monkeypatch):
    runner = accepted_claim_runner(run_environment, monkeypatch)
    runner.click_claim_button = claimer.DBOGRunner.click_claim_button.__get__(runner)
    runner.detector.matches.side_effect = lambda _frame, name: (name == "daily_rewards", 0.9)
    runner.read_claim_button = mock.Mock(return_value=6)
    runner.click = mock.Mock()
    runner.wait_until_gone = mock.Mock()

    with pytest.raises(claimer.UnsafeGameState, match="changed"):
        runner.process_slot(1)

    runner.click.assert_not_called()
    runner.mark_pending_claim.assert_not_called()
    claimer.update_claim_state.assert_not_called()


def test_confirm_claim_day_falls_back_to_streak_when_hover_hides_button_text():
    # Live 17 Oct: the cursor rested on "Claim Day 3", its hover state was unreadable,
    # and the old check refused a valid claim as a changed streak.
    runner = bare_runner()
    runner.read_claim_button = mock.Mock(return_value=None)
    runner.read_streak = mock.Mock(return_value=2)

    runner.confirm_claim_day("hovered", 3)


@pytest.mark.parametrize(
    ("button_day", "streak", "message"),
    [(4, 2, "changed"), (None, 3, "changed"), (None, None, "unreadable")],
)
def test_confirm_claim_day_refuses_a_different_or_unreadable_day(button_day, streak, message):
    runner = bare_runner()
    runner.read_claim_button = mock.Mock(return_value=button_day)
    runner.read_streak = mock.Mock(return_value=streak)

    with pytest.raises(claimer.UnsafeGameState, match=message):
        runner.confirm_claim_day("frame", 3)


def test_click_claim_button_returns_the_first_receipt_frame_when_streak_advances(fake_clock):
    runner = bare_runner()
    runner.screenshot = mock.Mock(side_effect=["before", "same", "receipt"])
    runner.detector.matches.return_value = (True, 0.9)
    runner.read_streak = mock.Mock(side_effect=[4, 4, 5])
    runner.read_claim_button = mock.Mock()
    runner.click = mock.Mock()
    validate_source = mock.Mock()

    assert runner.click_claim_button(1, 5, validate_source) == "receipt"

    validate_source.assert_called_once_with("before")
    runner.mark_pending_claim.assert_called_once_with(1, 5)
    runner.click.assert_called_once_with(*claimer.CLAIM_BUTTON, before_button_send=mock.ANY)
    runner.read_claim_button.assert_not_called()
    assert runner.screenshot.call_count == 3


@pytest.mark.parametrize("before_streak, after_streak", [(13, 14), (13, 0), (14, 1), (None, 5)])
def test_click_claim_button_accepts_streak_wrap_and_unreadable_initial_streak(fake_clock, before_streak, after_streak):
    runner = bare_runner()
    runner.screenshot = mock.Mock(side_effect=["before", "receipt"])
    runner.detector.matches.return_value = (True, 0.9)
    runner.read_streak = mock.Mock(side_effect=[before_streak, after_streak])
    runner.click = mock.Mock()
    day = 5 if before_streak is None else before_streak % 14 + 1

    assert runner.click_claim_button(1, day) == "receipt"
    runner.click.assert_called_once_with(*claimer.CLAIM_BUTTON, before_button_send=mock.ANY)
