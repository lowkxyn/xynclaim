from dataclasses import replace
import json
from unittest import mock

import pytest

from xynclaim import bridge, engine
from xynclaim.engine import save_report as save_run_report
from xynclaim.settings import SettingsError, default_settings, load_settings, save_settings, plan_sequence_claims, validate_sequence
from test_align_slots import panel_runner


def sync_runner(day=1, slots=None, modes=None, **attrs):
    runner = panel_runner(day=day, slots=slots or [2, 3], **attrs)
    runner.slot_mismatch_policies = modes or {2: "sync", 3: "sync"}
    return runner


@pytest.mark.parametrize("day", [2, 7, 14])
def test_sync_claims_non_day_one_using_visible_day(monkeypatch, day):
    logged = []
    runner = sync_runner(day, log=logged.append)
    monkeypatch.setattr(engine, "load_claim_state", lambda: {"2": {"next_day": 1}})
    monkeypatch.setattr(engine, "update_claim_state", mock.Mock())
    assert runner.process_slot(2).day == day
    runner.click_claim_button.assert_called_once()
    assert any("using the visible day" in line for line in logged)
    assert getattr(runner, "sync_day_one_seen", set()) == set()


@pytest.mark.parametrize("restore", [False, True])
def test_sync_day_one_holds_until_other_sync_slots_are_seen(monkeypatch, restore):
    runner = sync_runner(slots=[2, 3, 5])
    runner.slot_mismatch_policies[5] = "sync"
    runner.detector.matches.return_value = (restore, 1.0)
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    with pytest.raises(engine.SlotHeld, match="waiting for slots 3, 5") as held:
        runner.process_slot(2)
    assert held.value.day == 1
    assert runner.sync_day_one_seen == {2}
    runner.click_claim_button.assert_not_called()
    runner.mark_pending_claim.assert_not_called()


@pytest.mark.parametrize("restore", [False, True])
def test_last_sync_slot_claims_day_one_directly(monkeypatch, restore):
    runner = sync_runner(sync_day_one_seen={2})
    runner.detector.matches.return_value = (restore, 1.0)
    monkeypatch.setattr(engine, "load_claim_state", lambda: {"3": {"next_day": 8}})
    monkeypatch.setattr(engine, "update_claim_state", mock.Mock())
    assert runner.process_slot(3).day == 1
    assert runner.sync_day_one_seen == {2, 3}
    runner.click_claim_button.assert_called_once()


def test_only_selected_sync_slots_count(monkeypatch):
    runner = sync_runner(slots=[1, 2, 3, 4], modes={1: "always", 2: "sync", 3: "hold", 4: "align", 5: "sync"})
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    monkeypatch.setattr(engine, "update_claim_state", mock.Mock())
    assert runner.process_slot(2).day == 1
    runner.click_claim_button.assert_called_once()


def run_with_panels(run_environment, monkeypatch, days, modes=None):
    runner = run_environment(slots=list(days), slot_mismatch_policies=modes or dict.fromkeys(days, "sync"))
    panel = panel_runner()
    for name in ("open_private_slot", "wait_for_daily_or_world", "detect_claim_day",
                 "click_claim_button", "read_receipt_rewards", "save_receipt_region"):
        setattr(runner, name, getattr(panel, name))
    runner.detect_claim_day.side_effect = lambda screen: days[runner.open_private_slot.call_args.args[0]]
    runner.process_slot = mock.Mock(wraps=engine.DBOGRunner.process_slot.__get__(runner))
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    monkeypatch.setattr(engine, "update_claim_state", mock.Mock())
    return runner


def test_second_pass_replaces_holds_in_reports_and_history(run_environment, monkeypatch, engine_files):
    runner = run_with_panels(run_environment, monkeypatch, {2: 1, 3: 1, 5: 1})
    monkeypatch.setattr(engine, "save_report", save_run_report)
    partials = []
    monkeypatch.setattr(engine, "save_partial_report", lambda report: partials.append(
        [(result.slot, result.status) for result in report.slots]))
    report = runner.run()
    assert runner.process_slot.call_args_list == [mock.call(slot) for slot in [2, 3, 5, 2, 3]]
    assert [(result.slot, result.day, result.status) for result in report.slots] == [
        (slot, 1, "claimed_unverified") for slot in [2, 3, 5]]
    assert all(len({slot for slot, status in partial}) == len(partial) for partial in partials)
    history = [json.loads(line) for line in (engine_files / "history.jsonl").read_text().splitlines()]
    assert len(history) == 1
    assert len(history[0]["slots"]) == 3
    assert runner.click_claim_button.call_count == 3


@pytest.mark.parametrize("outcome", ["later_day", "missing", "failed", "no_daily", "not_visited"])
def test_no_second_pass_without_every_sync_slot_ready(run_environment, monkeypatch, outcome):
    runner = run_with_panels(run_environment, monkeypatch, {2: 1, 3: 7 if outcome == "later_day" else 1})
    process = runner.process_slot
    def visit(slot):
        if slot == 3:
            if outcome == "missing":
                raise engine.SlotUnavailable("not available")
            if outcome == "failed":
                # Even a failure after a live observation must block the second pass.
                runner.sync_day_one_seen.add(3)
                raise RuntimeError("failed after reading")
            if outcome == "no_daily":
                return engine.SlotResult(3, "no_daily", None, 0)
        result = process(slot)
        return result
    runner.process_slot = mock.Mock(side_effect=visit)
    if outcome == "not_visited":
        runner.return_to_main_menu.side_effect = RuntimeError("navigation failed")
    report = runner.run()
    assert report.slots[0].status == "held"
    assert report.slots[0].day == 1
    assert runner.process_slot.call_count == (1 if outcome == "not_visited" else 2)


@pytest.mark.parametrize("stop", ["cancel", "keyboard", "focus"])
def test_second_pass_respects_stop_and_focus_loss(run_environment, monkeypatch, stop):
    runner = run_with_panels(run_environment, monkeypatch, {2: 1, 3: 1, 4: 1})
    process = runner.process_slot
    visits = []
    def visit(slot):
        visits.append(slot)
        if len(visits) == 4:
            if stop == "cancel":
                raise engine.RunStopped()
            if stop == "keyboard":
                raise KeyboardInterrupt()
            raise engine.FocusLost("focus lost")
        return process(slot)
    runner.process_slot = mock.Mock(side_effect=visit)
    report = runner.run()
    assert visits == [2, 3, 4, 2]
    assert len(report.slots) == 3
    assert report.slots[1].status == "not_attempted"
    assert report.slots[1].detail == (
        "Run failed before the Day 1 revisit" if stop == "focus"
        else "Run stopped before the Day 1 revisit"
    )
    assert report.slots[0].status == ("failed" if stop == "focus" else "not_attempted")
    assert runner.click_claim_button.call_count == 1


def test_run_clears_observations_from_an_earlier_run(run_environment, monkeypatch):
    runner = run_with_panels(run_environment, monkeypatch, {2: 1, 3: 7})
    runner.sync_day_one_seen = {2, 3}
    report = runner.run()
    assert [result.status for result in report.slots] == ["held", "claimed_unverified"]
    assert runner.sync_day_one_seen == {2}


def test_sync_settings_and_bridge_round_trip_and_planner(tmp_path):
    path = tmp_path / "settings.json"
    settings = replace(default_settings(3), sequence_modes={1: "sync", 2: "sync", 3: "hold"})
    save_settings(path, settings)
    assert load_settings(path, 3) == settings
    api = bridge.XynClaimAPI(path)
    api.save_plan({"sequence_modes": {"1": "sync", "2": "sync", "3": "hold"}})
    assert load_settings(path, 3).sequence_modes == settings.sequence_modes
    decision = plan_sequence_claims(settings, [3, 2, 1], {"1": {"next_day": 1}, "2": {"next_day": 7}})
    assert decision.due_slots == (2, 1)
    assert decision.held_slots == (3,)
    assert decision.reasons[1] == "Day 1 line-up: claims until Day 1, then waits for the other Day 1 slots"


def test_runner_accepts_sync_mode():
    with mock.patch.object(engine, "VisualDetector"), mock.patch.object(engine, "DirectInput"):
        runner = engine.DBOGRunner([3, 2], lambda _: None, slot_mismatch_policies={2: "sync", 3: "sync"})
    assert runner.slots == [3, 2]


def test_mixed_run_only_revisits_sync_holds(run_environment, monkeypatch):
    runner = run_with_panels(run_environment, monkeypatch, {1: 1, 2: 8, 3: 1, 4: 1, 5: 1},
                             modes={1: "sync", 2: "always", 3: "hold", 4: "align", 5: "sync"})
    runner.sequence_align_targets = {4: 2}
    report = runner.run()
    assert [result.status for result in report.slots] == [
        "claimed_unverified", "claimed_unverified", "held", "held", "claimed_unverified"]
    assert runner.process_slot.call_args_list == [mock.call(slot) for slot in [1, 2, 3, 4, 5, 1]]
    assert runner.sync_day_one_seen == {1, 5}


@pytest.mark.parametrize("failure", [RuntimeError("unsafe state"), engine.JoinCodeRejected("rejected")])
def test_failure_in_second_pass_replaces_pending_holds(run_environment, monkeypatch, failure):
    runner = run_with_panels(run_environment, monkeypatch, {2: 1, 3: 1, 4: 1})
    process = runner.process_slot
    visits = []
    def visit(slot):
        visits.append(slot)
        if len(visits) == 4:
            raise failure
        return process(slot)
    runner.process_slot = mock.Mock(side_effect=visit)
    report = runner.run()
    assert visits == [2, 3, 4, 2]
    assert [result.status for result in report.slots] == [
        "server_code_invalid" if isinstance(failure, engine.JoinCodeRejected) else "failed",
        "not_attempted", "claimed_unverified",
    ]
    assert report.slots[1].detail == (
        "The selected server access was rejected" if isinstance(failure, engine.JoinCodeRejected)
        else "Run failed before the Day 1 revisit"
    )
    assert len({result.slot for result in report.slots}) == 3


def test_stop_before_first_revisit_replaces_queued_holds(run_environment, monkeypatch):
    runner = run_with_panels(run_environment, monkeypatch, {2: 1, 3: 1, 4: 1})
    process = runner.process_slot
    def visit(slot):
        result = process(slot)
        if slot == 4:
            runner.stop_event.set()
        return result
    runner.process_slot = mock.Mock(side_effect=visit)
    report = runner.run()
    assert [result.status for result in report.slots] == [
        "not_attempted", "not_attempted", "claimed_unverified"]
    assert all(result.detail == "Run stopped before the Day 1 revisit" for result in report.slots[:2])
    assert runner.process_slot.call_count == 3


@pytest.mark.parametrize("targets", [{4: 1}, {}])
def test_align_cannot_target_day_one_slot(targets):
    with pytest.raises(SettingsError, match="Slot 4 can't Align with slot 1 while slot 1 uses Day 1; set slot 4 to Day 1 too or pick another target"):
        validate_sequence({1: "sync", 4: "align"}, targets, 4)


def test_bridge_surfaces_align_day_one_error_without_saving(tmp_path):
    path = tmp_path / "settings.json"
    settings = default_settings(4)
    save_settings(path, settings)
    before = path.read_bytes()
    api = bridge.XynClaimAPI(path)
    with pytest.raises(SettingsError, match="uses Day 1"):
        api.save_plan({"sequence_modes": {"1": "sync"}})
    assert path.read_bytes() == before


@pytest.mark.parametrize("preset", ["sync", "align"])
def test_presets_save_valid_settings(tmp_path, preset):
    path = tmp_path / "settings.json"
    save_settings(path, default_settings(4))
    api = bridge.XynClaimAPI(path)
    modes = {str(slot): ("sync" if preset == "sync" else "always" if slot == 1 else "align")
             for slot in range(1, 5)}
    assert api.save_plan({"sequence_modes": modes,
                          "sequence_align_targets": {str(slot): 1 for slot in range(2, 5)}}) == {"saved": True}
    settings = load_settings(path)
    assert {slot: settings.sequence_modes[slot] for slot in range(1, 5)} == {int(slot): mode for slot, mode in modes.items()}


def test_bridge_invalid_mode_uses_day_one_label(tmp_path):
    api = bridge.XynClaimAPI(tmp_path / "settings.json")
    with pytest.raises(ValueError, match="Day 1") as error:
        api.save_plan({"sequence_modes": {"1": "invalid"}})
    assert "sync" not in str(error.value)


@pytest.mark.parametrize("error", [engine.RunStopped, KeyboardInterrupt])
def test_stop_after_recorded_revisit_preserves_claim(run_environment, monkeypatch, error):
    runner = run_with_panels(run_environment, monkeypatch, {2: 1, 3: 1, 4: 1})
    def save_partial(report):
        if runner.process_slot.call_count == 4:
            raise error()
    monkeypatch.setattr(engine, "save_partial_report", save_partial)
    report = runner.run()
    assert [result.status for result in report.slots] == [
        "claimed_unverified", "not_attempted", "claimed_unverified"]
    assert report.slots[1].detail == "Run stopped before the Day 1 revisit"
    assert runner.click_claim_button.call_count == 2
