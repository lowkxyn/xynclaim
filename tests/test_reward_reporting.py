"""Reward popup parsing, the verified claim status, and the per-slot run summary."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
import pytest

from xynclaim import engine as claimer
from xynclaim import summary
from xynclaim import ocr as reward_reader
from test_core import bare_runner


@pytest.mark.parametrize("lines, expected", [
    (["Claim Day 1"], 1),
    (["Claim Day 14"], 14),
    (["Claim Day I2"], 12),
    (["Claimed"], None),
    ([], None),
])
def test_claim_button_text_is_parsed(lines, expected):
    assert reward_reader.parse_claim_day(lines) == expected


@pytest.mark.parametrize("lines, expected", [
    (["Streak O / 14"], 0),
    (["Streak 1 / 11+"], 1),
    (["Streak 13 / 14"], 13),
    (["New Daily: 02:11:55"], None),
])
def test_streak_footer_is_parsed_despite_ocr_noise(lines, expected):
    assert reward_reader.parse_streak(lines) == expected


def test_new_global_lock_toast_is_read_as_locked():
    lines = ["Daily reward", "This boost was already claimed on this", "account. +10 Mastery Points."]
    assert reward_reader.parse_rewards(lines) == ["+10 Mastery Points (global reward locked)"]
    assert reward_reader.is_global_locked(lines)


def test_new_reward_toast_reads_the_sentence_under_its_title():
    lines = ["Daily reward", "You received", "31,250 Zeni."]
    assert reward_reader.parse_rewards(lines) == ["31,250 Zeni"]
    assert not reward_reader.is_global_locked(lines)


def test_new_reward_toast_without_a_sentence_stays_unverified():
    assert reward_reader.parse_rewards(["Daily reward"]) == []


# --- popup parsing -------------------------------------------------------------

@pytest.mark.parametrize("lines, expected", [
    (["Received Zeni", "31250"], ["31,250 Zeni"]),
    (["Received Zeni", "31,250"], ["31,250 Zeni"]),
    (["Received Dragon Ball", "5-Star Namekian Dragon Ball"], ["5-Star Namekian Dragon Ball"]),
    (["Received 2X EXP for", "600"], ["2X EXP for 10 min"]),
    (["Received 2X EXP for", "1800"], ["2X EXP for 30 min"]),
    (["Received 2X Drops for", "90"], ["2X Drops for 1 min 30 s"]),
    (["Received +10 of Item", "Holy Water"], ["10x Holy Water"]),
    (["Received 10 of Item", "Fruit of the Tree of Might"], ["10x Fruit of the Tree of Might"]),
    (["Received +10 of Item"], []),
    (["Global reward locked", "+10 Mastery Points"], ["+10 Mastery Points (global reward locked)"]),
    (["Dragon Balls Complete", "+100 Mastery Points"], ["+100 Mastery Points (all Dragon Balls held)"]),
    (["Day 14 Bonus", "1 Hour 2x Zeni + 2x Drops + 2x EXP"], ["1 Hour 2x Zeni + 2x Drops + 2x EXP"]),
    (["Day 14 Bonus"], []),
])
def test_known_popups_are_read(lines, expected):
    assert reward_reader.parse_rewards(lines) == expected


def test_ocr_misreading_one_star_as_capital_i_is_corrected():
    assert reward_reader.parse_rewards(["Received Dragon Ball", "I-Star Negative Energy Dragon Ball"]) == [
        "1-Star Negative Energy Dragon Ball"
    ]


def test_unrelated_popups_in_the_stack_are_ignored():
    lines = ["Elder Kai's Online Training", "Your potential has been pushed!", "Received Zeni", "31250"]
    assert reward_reader.parse_rewards(lines) == ["31,250 Zeni"]


def test_a_clipped_title_still_reads_a_self_describing_dragon_ball():
    assert reward_reader.parse_rewards(["7-Star Dragon Ball"]) == ["7-Star Dragon Ball"]


def test_a_bare_number_without_its_title_is_not_guessed():
    # "31250" alone could be anything; claiming it as Zeni would be a guess.
    assert reward_reader.parse_rewards(["31250"]) == []


def test_empty_and_title_only_stacks():
    assert reward_reader.parse_rewards([]) == []
    assert reward_reader.parse_rewards(["Received Zeni"]) == []
    assert reward_reader.parse_rewards(["Global reward locked"]) == ["+10 Mastery Points (global reward locked)"]


def test_two_reward_popups_are_both_kept():
    lines = ["Received Dragon Ball", "4-Star Dragon Ball", "Dragon Balls Complete", "+100 Mastery Points"]
    assert reward_reader.parse_rewards(lines) == [
        "4-Star Dragon Ball",
        "+100 Mastery Points (all Dragon Balls held)",
    ]


def test_ocr_reads_the_multiplier_on_a_white_on_navy_popup():
    pytest.importorskip("winrt.windows.media.ocr")
    popup = np.full((60, 520, 3), (90, 50, 10), np.uint8)
    cv2.putText(popup, "Received 2X EXP for", (10, 40), cv2.FONT_HERSHEY_DUPLEX, 1.0, (255, 255, 255), 2)
    lines = reward_reader.ocr_lines(popup)
    if lines is None:
        pytest.skip("no Windows OCR language installed")
    assert lines == ["Received 2X EXP for"]


def test_ocr_returns_none_when_windows_ocr_is_missing():
    with mock.patch.dict("sys.modules", {"winrt.windows.media.ocr": None}):
        assert reward_reader.ocr_lines(np.zeros((10, 10, 3), np.uint8)) is None


# --- claimer status --------------------------------------------------------------

def claim_runner(read_result):
    runner = bare_runner()
    runner.open_private_slot = mock.MagicMock()
    runner.wait_for_daily_or_world = mock.MagicMock(return_value=("daily", "screen"))
    runner.tap = mock.MagicMock()
    runner.detect_claim_day = mock.MagicMock(return_value=3)
    runner.click_claim_button = mock.MagicMock(return_value="screen-after-claim")
    runner.screenshot = mock.MagicMock(return_value="screen-after-claim")
    runner.save_receipt_region = mock.MagicMock(return_value=Path("receipt_slot_01.png"))
    runner.return_to_main_menu = mock.MagicMock()
    runner.detector.matches.return_value = (False, 0.0)
    runner.read_receipt_rewards = mock.MagicMock(return_value=(read_result, "screen-after-claim"))
    return runner


def run_claim(runner):
    with mock.patch.object(claimer, "load_claim_state", return_value={}), mock.patch.object(
        claimer, "update_claim_state"
    ) as update_state, mock.patch("time.sleep"):
        return runner.process_slot(1), update_state


def test_read_reward_marks_the_claim_verified():
    runner = claim_runner(["5-Star Namekian Dragon Ball"])
    result, update_state = run_claim(runner)
    assert result.status == "claimed_verified"
    assert result.verification == "ocr"
    assert result.received_reward == "5-Star Namekian Dragon Ball"
    assert result.day == 3 and result.next_day == 4
    update_state.assert_called_with(1, 3, verification="ocr", received_reward="5-Star Namekian Dragon Ball")
    runner.click_claim_button.assert_called_once_with(1, 3, mock.ANY)
    runner.read_receipt_rewards.assert_called_once_with("screen-after-claim")
    runner.save_receipt_region.assert_called_once_with("screen-after-claim", 1)
    runner.screenshot.assert_not_called()


def test_global_lock_read_by_ocr_keeps_the_global_locked_status():
    result, _ = run_claim(claim_runner(["+10 Mastery Points (global reward locked)"]))
    assert result.status == "claimed_global_locked"


@pytest.mark.parametrize("read_result", [None, []])
def test_unreadable_popup_stays_unverified(read_result):
    result, _ = run_claim(claim_runner(read_result))
    assert result.status == "claimed_unverified"
    assert result.received_reward is None


@pytest.mark.parametrize("first_lines", [[], ["Received Zeni"]])
def test_reader_looks_again_when_the_popup_lags(first_lines):
    runner = bare_runner()
    runner.screenshot = mock.MagicMock(return_value=np.zeros((1080, 1920, 3), np.uint8))
    with mock.patch.object(reward_reader, "ocr_lines", side_effect=[first_lines, ["Received Zeni", "31250"]]), \
            mock.patch("time.sleep"):
        rewards, _ = runner.read_receipt_rewards(np.zeros((1080, 1920, 3), np.uint8))
    assert rewards == ["31,250 Zeni"]
    assert runner.screenshot.call_count == 1


def test_reader_fault_does_not_fail_the_slot():
    runner = bare_runner()
    with mock.patch.object(reward_reader, "ocr_lines", side_effect=OSError("ocr broke")):
        rewards, _ = runner.read_receipt_rewards(np.zeros((1080, 1920, 3), np.uint8))
    assert rewards is None


def test_text_report_shows_the_received_reward():
    slot = claimer.SlotResult(slot=2, status="claimed_verified", day=2, seconds=1.0,
                              reward="Zeni (Level Ã— 25)", next_day=3, next_reward="x",
                              received_reward="31,250 Zeni")
    lines = claimer.render_text_report(mock.Mock(
        app_version="1", server_profile="p", access_mode="owner", configured_slot_count=12,
        started_utc="s", finished_utc="f", total_seconds=1.0, slots=[slot], errors=[],
    ))
    assert "CLAIMED Day 2: received 31,250 Zeni" in lines


# --- per-slot summary -------------------------------------------------------------

def slot(number, status, day, **extra):
    return {"slot": number, "status": status, "day": day, **extra}


def test_summary_lines_for_every_status():
    slots = [
        slot(1, "claimed_verified", 3, received_reward="5-Star Dragon Ball", next_day=4, next_reward="30 min 2Ã— XP"),
        slot(2, "claimed_global_locked", 1, received_reward="+10 Mastery Points", next_day=2),
        slot(3, "claimed_unverified", 3, reward="Random Dragon Ball", next_day=4),
        slot(4, "inspected", 5),
        slot(5, "no_daily", None),
        slot(6, "held", 1),
    ]
    body, claimed, held = summary.summarize(slots)
    assert body.splitlines() == [
        "` 1` Day 3: 5-Star Dragon Ball",
        "` 2` Day 1: +10 Mastery Points (global reward locked)",
        "` 3` Day 3: claimed, reward not read (expected Random Dragon Ball)",
        "` 4` on Day 5, not claimed",
        "` 5` no daily panel, skipped",
        "` 6` held on Day 1",
    ]
    assert claimed == ["1", "2", "3"]
    assert held == ["5", "6"]


def test_later_report_replaces_the_scan_entry_for_the_same_slot():
    scan = {"slots": [slot(1, "inspected", 3), slot(2, "inspected", 1)]}
    claim = {"slots": [slot(1, "claimed_verified", 3, received_reward="Zeni")]}
    merged = summary.latest_slots([scan, claim])
    assert [(entry["slot"], entry["status"]) for entry in merged] == [(1, "claimed_verified"), (2, "inspected")]


def test_next_day_shows_the_reward_when_aligned_and_the_spread_when_not():
    aligned = [slot(n, "claimed_verified", 3, next_day=4, next_reward="30 min 2Ã— XP") for n in (1, 2)]
    assert summary.next_days(aligned) == "Day 4 (30 min 2Ã— XP)"
    spread = aligned + [slot(3, "claimed_verified", 5, next_day=6, next_reward="10 random consumables")]
    assert summary.next_days(spread) == "Day 4, Day 6"
    assert summary.next_days([slot(1, "inspected", 2)]) == "none"


def test_fields_add_up_errors_and_time_across_reports():
    reports = [{"errors": ["a"], "total_seconds": 60}, {"errors": [], "total_seconds": 120}]
    fields = summary.embed_fields([slot(1, "claimed_verified", 2, next_day=3)], reports)
    assert fields["Errors"] == "1"
    assert fields["Duration"] == "3.0 min"


def test_skipped_slots_are_listed_with_held_ones_in_slot_order():
    slots = [slot(n, "claimed_verified", 5, next_day=6) for n in (2, 3)] + [slot(11, "held", 1)]
    assert summary.embed_fields(slots, [], skipped=[1, 12])["Held / skipped"] == "1, 11, 12"
    assert summary.embed_fields(slots, [], skipped=["11"])["Held / skipped"] == "11"
    assert summary.embed_fields(slots[:2], [])["Held / skipped"] == "none"


def test_reports_since_skips_older_and_unreadable_files(tmp_path):
    started = datetime(2026, 9, 25, 1, tzinfo=timezone.utc)
    old = tmp_path / "run_old.json"
    new = tmp_path / "run_new.json"
    broken = tmp_path / "run_broken.json"
    old.write_text(json.dumps({"slots": [slot(1, "held", 1)]}))
    new.write_text(json.dumps({"slots": [slot(1, "claimed_verified", 3)]}))
    broken.write_text("{not json")
    before, after = (started - timedelta(hours=1)).timestamp(), (started + timedelta(minutes=5)).timestamp()
    os.utime(old, (before, before))
    os.utime(new, (after, after))
    os.utime(broken, (after, after))
    reports = summary.reports_since(tmp_path, started)
    assert reports == [{"slots": [slot(1, "claimed_verified", 3)]}]


@pytest.mark.parametrize("lines, expected", [
    (["Daily reward", "Received +32500 Zeni."], ["32,500 Zeni"]),
    (["Daily rew�rd", "Received I-Star Black Star Dragon Ball."], ["1-Star Black Star Dragon Ball"]),
    (["Daily reward", "Received I-Star Negative Energy Dragon"], ["1-Star Negative Energy Dragon Ball"]),
    (["Daily reward", "Received 2X EXP for 30 minutes."], ["2X EXP for 30 minutes"]),
])
def test_october_toast_sentences_are_normalised(lines, expected):
    assert reward_reader.parse_rewards(lines) == expected


def test_click_transition_waits_out_a_blank_frame_after_a_landed_click():
    from test_core import bare_runner as _bare
    runner = _bare()
    runner.screenshot = mock.MagicMock(side_effect=["slots", "blank"])
    runner.detector.matches.side_effect = lambda screen, name: (screen == "slots" and name == "slot_select", 0.9)
    runner.click = mock.MagicMock()
    runner.wait_for = mock.MagicMock(side_effect=TimeoutError("still loading"))
    runner.wait_for_either = mock.MagicMock(return_value=("server_menu", "destinations", 0.9))
    runner.log = mock.MagicMock()
    assert runner.click_transition(715, 330, "server_menu", 0.8, "slot_select") == ("destinations", 0.9)
    runner.click.assert_called_once()


def test_click_transition_still_refuses_input_when_the_target_never_appears():
    from test_core import bare_runner as _bare
    runner = _bare()
    runner.screenshot = mock.MagicMock(side_effect=["slots", "blank"])
    runner.detector.matches.side_effect = lambda screen, name: (screen == "slots" and name == "slot_select", 0.9)
    runner.click = mock.MagicMock()
    runner.wait_for = mock.MagicMock(side_effect=TimeoutError("nothing"))
    runner.wait_for_either = mock.MagicMock(return_value=(None, None, 0.0))
    runner.log = mock.MagicMock()
    with pytest.raises(claimer.UnsafeGameState):
        runner.click_transition(715, 330, "server_menu", 0.8, "slot_select")
    runner.click.assert_called_once()
