from pathlib import Path
from unittest import mock

import pytest

from xynclaim import engine as claimer


def captured_panel(name='daily_panel_2026_claim_day1.png'):
    frame = claimer.cv2.imread(str(Path(__file__).parent / 'fixtures' / name))
    assert frame is not None
    return frame


def test_real_broken_streak_panel_reads_day_one_and_offers_paid_restore():
    frame = captured_panel()
    assert claimer.DBOGRunner.read_claim_button(frame) == 1
    assert claimer.DBOGRunner.detect_claim_day(frame) == 1
    assert claimer.DBOGRunner.read_streak(frame) == 0
    assert claimer.VisualDetector().matches(frame, 'restore_streak')[0]


def test_absent_restore_button_does_not_reset_normal_day():
    frame = captured_panel()
    left, top, right, bottom = claimer.ANCHORS['restore_streak'].search_rect
    frame[top:bottom, left:right] = 0
    assert not claimer.VisualDetector().matches(frame, 'restore_streak')[0]
    assert claimer.DBOGRunner.detect_claim_day(frame) == 1


def test_claimed_button_has_no_claim_day_and_panel_stays_open():
    frame = captured_panel('daily_panel_2026_claimed.png')
    assert claimer.DBOGRunner.read_claim_button(frame) is None
    assert claimer.DBOGRunner.read_streak(frame) == 1
    detector = claimer.VisualDetector()
    assert detector.matches(frame, 'daily_rewards')[0]
    assert detector.matches(frame, 'global_reward_locked')[0]


def test_unreadable_panel_refuses_to_guess_a_day():
    frame = captured_panel()
    for top, bottom, left, right in (claimer.CLAIM_BUTTON_TEXT_REGION, claimer.STREAK_TEXT_REGION):
        frame[top:bottom, left:right] = 0
    with pytest.raises(claimer.UnsafeGameState, match="Couldn't read"):
        claimer.DBOGRunner.detect_claim_day(frame)


def configured_runner(inspect_only):
    frame = captured_panel()
    runner = object.__new__(claimer.DBOGRunner)
    runner.log = mock.Mock()
    runner.open_private_slot = mock.Mock()
    runner.wait_for_daily_or_world = mock.Mock(return_value=('daily', frame))
    runner.tap = mock.Mock()
    runner.click = mock.Mock()
    runner.detector = claimer.VisualDetector()
    runner.min_claim_day = 2
    runner.inspect_only = inspect_only
    runner.return_after_recorded_result = lambda result: result
    runner.click_claim_button = mock.Mock()
    runner.mark_pending_claim = mock.Mock()
    return runner


def test_broken_streak_is_inspected_as_day1_without_clicking():
    runner = configured_runner(True)
    with mock.patch.object(claimer, 'load_claim_state', return_value={}):
        result = runner.process_slot(3)
    assert result.status == 'inspected' and result.day == 1
    runner.click_claim_button.assert_not_called()
    runner.click.assert_not_called()
    runner.mark_pending_claim.assert_not_called()


def test_broken_streak_day_one_is_held_and_restore_is_never_clicked():
    runner = configured_runner(False)
    with mock.patch.object(claimer, 'load_claim_state', return_value={}), mock.patch.object(claimer, 'update_claim_state') as update:
        result = runner.process_slot(3)
    assert result.status == 'held' and result.day == 1
    runner.click_claim_button.assert_not_called()
    runner.click.assert_not_called()
    runner.mark_pending_claim.assert_not_called()
    update.assert_not_called()


def test_restore_offer_does_not_override_a_readable_later_claim_day():
    runner = configured_runner(True)
    runner.detect_claim_day = mock.Mock(return_value=4)
    assert runner.detector.matches(captured_panel(), 'restore_streak')[0]

    result = runner.process_slot(3)

    assert result.status == 'inspected' and result.day == 4
    runner.click_claim_button.assert_not_called()
    runner.click.assert_not_called()
    runner.mark_pending_claim.assert_not_called()


def test_day_one_release_holds_every_later_day_without_clicks():
    for day in range(2, 15):
        runner = configured_runner(False)
        runner.min_claim_day = 1
        runner.max_claim_day = 1
        runner.detector = mock.Mock()
        runner.detector.matches.return_value = (False, 0.0)
        runner.detect_claim_day = mock.Mock(return_value=day)
        with mock.patch.object(claimer, 'load_claim_state', return_value={}):
            result = runner.process_slot(2)
        assert result.status == 'held' and result.day == day
        runner.click_claim_button.assert_not_called()
        runner.click.assert_not_called()
        runner.mark_pending_claim.assert_not_called()


def test_invalid_claim_day_range_is_rejected():
    for minimum, maximum in [(1, 0), (1, 15), (3, 1)]:
        with pytest.raises(ValueError, match='max_claim_day'):
            claimer.DBOGRunner([1], lambda message: None,
                              min_claim_day=minimum, max_claim_day=maximum)
