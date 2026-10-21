import json
from pathlib import Path
from unittest import mock

import pytest

from xynclaim import engine as claimer
from test_core import bare_runner
from xynclaim.history import read_history


def pending_runner(day):
    runner = bare_runner()
    runner.open_private_slot = mock.Mock()
    runner.wait_for_daily_or_world = mock.Mock(return_value=("daily", "daily"))
    runner.tap = mock.Mock()
    runner.detect_claim_day = mock.Mock(return_value=day)
    runner.click_claim_button = mock.Mock()
    runner.return_to_main_menu = mock.Mock()
    return runner


def test_no_panel_is_not_evidence_of_pending_claim_acceptance(engine_files):
    claimer.mark_pending_claim(1, 5)
    runner = pending_runner(5)
    runner.wait_for_daily_or_world.return_value = ("world", "world")
    runner.wait_for_daily_grace = mock.Mock(return_value=(False, None))

    with pytest.raises(claimer.SlotHeld, match="pending"):
        runner.process_slot(1)

    assert claimer.pending_claim_day(claimer.load_claim_state()["1"]) == 5
    runner.click_claim_button.assert_not_called()


def test_broken_day_one_is_not_evidence_of_pending_day_fourteen_acceptance(engine_files):
    claimer.update_claim_state(1, 13)
    claimer.mark_pending_claim(1, 14)
    runner = pending_runner(1)
    runner.min_claim_day = 2
    runner.detector.matches.side_effect = lambda _frame, name: (name == "restore_streak", 0.9)

    with pytest.raises(claimer.SlotHeld, match="pending"):
        runner.process_slot(1)

    saved = claimer.load_claim_state()["1"]
    assert saved["claimed_day"] == 13 and claimer.pending_claim_day(saved) == 14
    runner.click_claim_button.assert_not_called()


def test_healthy_advanced_panel_preserves_the_pending_attempt_time(engine_files):
    attempted = "2026-09-29T01:20:00+00:00"
    claimer.CLAIM_STATE_PATH.write_text(json.dumps({"1": {
        "next_day": 5, "pending_claim": {"day": 5, "started_utc": attempted},
    }}), encoding="utf-8")
    runner = pending_runner(6)
    runner.min_claim_day = 7

    result = runner.process_slot(1)

    saved = claimer.load_claim_state()["1"]
    assert result.status == "held" and saved["claimed_day"] == 5
    assert saved["claimed_utc"] == attempted
    assert saved["verification"] == "reconciled"
    runner.click_claim_button.assert_not_called()


@pytest.mark.parametrize("tail", [b'{"started_utc":', b'{"message":"\xe2\x82'])
def test_truncated_history_tail_cannot_swallow_the_next_report(engine_files, tail):
    previous = {"started_utc": "previous", "slots": []}
    claimer.HISTORY_PATH.write_bytes(json.dumps(previous).encode() + b"\n" + tail)
    report = claimer.RunReport("test", "new", "end", 1, "owner", "test", 1, [], [])

    claimer.save_report(report)

    assert [record["started_utc"] for record in read_history(claimer.HISTORY_PATH)] == ["previous", "new"]


def test_ctrl_c_saves_already_completed_slots(run_environment):
    runner = run_environment()
    runner.process_slot.side_effect = [claimer.SlotResult(1, "claimed_unverified", 5, 1), KeyboardInterrupt()]

    try:
        report = runner.run()
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C skipped the completed-slot report")

    assert [slot.status for slot in report.slots] == ["claimed_unverified", "not_attempted"]
    claimer.save_report.assert_called_once_with(report)


def test_completed_slot_is_durable_before_the_next_slot_can_crash(run_environment):
    runner = run_environment()
    observed = []

    def process(slot):
        if slot == 1:
            return claimer.SlotResult(1, "claimed_unverified", 5, 1)
        observed.extend(read_history(claimer.HISTORY_PATH))
        raise SystemExit("simulated process exit")

    runner.process_slot.side_effect = process
    with pytest.raises(SystemExit):
        runner.run()

    assert len(observed) == 1
    assert observed[0]["slots"][0]["status"] == "claimed_unverified"


def test_boolean_pending_day_is_not_a_valid_day():
    assert claimer.pending_claim_day({"pending_claim": {"day": True}}) is None


@pytest.mark.parametrize("pending, visible_day", [({"day": True}, 2), ({"day": 5}, 6), ("invalid", 6)])
def test_invalid_pending_records_cannot_authorize_claims(engine_files, pending, visible_day):
    claimer.CLAIM_STATE_PATH.write_text(json.dumps({"1": {"pending_claim": pending}}), encoding="utf-8")
    runner = pending_runner(visible_day)

    with pytest.raises(claimer.SlotHeld):
        runner.process_slot(1)

    runner.click_claim_button.assert_not_called()


def test_maximum_day_hold_preserves_public_fallback_route(monkeypatch):
    runner = pending_runner(5)
    runner.allow_public_fallback = True
    runner.max_claim_day = 1
    runner.open_private_slot.side_effect = claimer.JoinCodeRejected()
    runner.open_public_fallback = mock.Mock()
    monkeypatch.setattr(claimer, "load_claim_state", mock.Mock(return_value={}))

    result = runner.process_slot(1)

    assert result.status == "held" and result.server_route == "public_fallback"
    runner.click_claim_button.assert_not_called()


def test_partial_checkpoint_updates_then_promotes_without_counting_twice(engine_files):
    report = claimer.RunReport(
        "test", "2026-09-30T01:20:00+00:00", "2026-09-30T01:21:00+00:00", 60,
        "owner", "test", 2, [claimer.SlotResult(1, "claimed_unverified", 5, 1)], [],
    )
    claimer.save_partial_report(report)
    report.slots.append(claimer.SlotResult(2, "claimed_verified", 5, 1))
    claimer.save_partial_report(report)
    records = read_history(claimer.HISTORY_PATH)
    assert len(records) == 1 and len(records[0]["slots"]) == 2
    assert len(list(engine_files.glob("partial_run_*.json"))) == 1

    claimer.save_report(report)

    records = read_history(claimer.HISTORY_PATH)
    assert len(records) == 1 and len(records[0]["slots"]) == 2
    assert not list(engine_files.glob("partial_run_*.json"))
    assert "in_progress" not in records[0]
    # A failed checkpoint deletion after finalization must not double totals.
    claimer.save_partial_report(report)
    assert len(read_history(claimer.HISTORY_PATH)) == 1


def test_checkpoint_write_failure_stops_before_another_slot(run_environment, monkeypatch):
    runner = run_environment()
    runner.process_slot.side_effect = lambda slot: claimer.SlotResult(slot, "claimed_unverified", 5, 1)
    monkeypatch.setattr(claimer, "save_partial_report", mock.Mock(side_effect=OSError("checkpoint disk full")))

    report = runner.run()

    assert runner.process_slot.call_count == 1
    assert [slot.status for slot in report.slots] == ["claimed_unverified", "not_attempted"]
    assert any("checkpoint disk full" in error for error in report.errors)


def test_partial_checkpoint_redacts_private_code_before_persisting(run_environment):
    runner = run_environment(server_code="private-code", slots=[1])
    runner.process_slot.side_effect = lambda slot: claimer.SlotResult(
        slot, "held", 5, 1, detail="cannot use private-code",
    )
    runner.errors.append("private-code was rejected")

    report = runner.run()

    payload = claimer.partial_report_path(report).read_text(encoding="utf-8")
    assert "private-code" not in payload and "[REDACTED]" in payload


def test_failed_atomic_checkpoint_replace_keeps_the_previous_complete_record(engine_files, monkeypatch):
    report = claimer.RunReport(
        "test", "2026-09-30T01:20:00+00:00", "2026-09-30T01:21:00+00:00", 60,
        "owner", "test", 2, [claimer.SlotResult(1, "claimed_unverified", 5, 1)], [],
    )
    claimer.save_partial_report(report)
    before = claimer.partial_report_path(report).read_bytes()
    report.slots.append(claimer.SlotResult(2, "claimed_unverified", 5, 1))
    monkeypatch.setattr(claimer.os, "replace", mock.Mock(side_effect=OSError("replace denied")))

    with pytest.raises(OSError, match="replace denied"):
        claimer.save_partial_report(report)

    assert claimer.partial_report_path(report).read_bytes() == before
    assert not list(engine_files.glob("*.tmp"))


def test_readable_history_survives_unavailable_checkpoint_directory(engine_files, monkeypatch):
    previous = {"started_utc": "previous", "slots": []}
    claimer.HISTORY_PATH.write_text(json.dumps(previous) + "\n", encoding="utf-8")
    monkeypatch.setattr(Path, "glob", mock.Mock(side_effect=OSError("directory unavailable")))

    assert read_history(claimer.HISTORY_PATH) == [previous]
