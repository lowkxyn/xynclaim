from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest import mock

import pytest

from xynclaim import bridge, engine
from xynclaim.settings import (
    SettingsError, default_settings, load_settings, save_settings,
    order_sequence_slots, plan_sequence_claims, recorded_day_today,
)
from test_core import bare_runner


NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def observed(day, stamp=NOW, **extra):
    return {"claimed_day": day, "next_day": day % 14 + 1,
            "claimed_utc": stamp.isoformat(), **extra}


def panel_runner(day=6, **attrs):
    runner = bare_runner(slot_mismatch_policies={2: "align"}, sequence_align_targets={2: 1}, **attrs)
    runner.open_private_slot = mock.Mock()
    runner.wait_for_daily_or_world = mock.Mock(return_value=("daily", "screen"))
    runner.detect_claim_day = mock.Mock(return_value=day)
    runner.click_claim_button = mock.Mock(return_value="receipt")
    runner.read_receipt_rewards = mock.Mock(return_value=(None, "receipt"))
    runner.save_receipt_region = mock.Mock(return_value=engine.Path("receipt.png"))
    runner.return_to_main_menu = mock.Mock()
    return runner


def test_new_defaults_and_existing_modes_are_distinct(tmp_path):
    path = tmp_path / "settings.json"
    assert load_settings(path).sequence_modes == {1: "always", **dict.fromkeys(range(2, 13), "align")}
    for old in ({}, {"sequence_target_days": "ignored obsolete data"},
                {"sequence_modes": {"1": "hold", "2": "always", "3": "align"},
                 "sequence_target_days": {"3": 14}}):
        path.write_text(json.dumps(old))
        loaded = load_settings(path)
        assert loaded.sequence_modes[1] == ("hold" if "sequence_modes" in old else "always")
        assert loaded.sequence_modes[2] == "always"
        assert loaded.sequence_align_targets[3] == 1


def test_old_file_with_slot_one_on_align_still_loads(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"sequence_modes": {"1": "align", "2": "align"},
                                "sequence_target_days": {"1": 1, "2": 2}}))
    loaded = load_settings(path)
    assert loaded.sequence_modes[1] == "always"
    assert loaded.sequence_modes[2] == "align"
    assert loaded.sequence_align_targets[2] == 1


@pytest.mark.parametrize("targets,modes,count", [
    ({2: True}, {}, 2), ({2: "1"}, {}, 2), ({2: []}, {}, 2),
    ({2: 0}, {}, 2), ({2: 13}, {}, 2), ({2: 2}, {}, 2),
    ({2: 3}, {}, 2), ({}, {1: "align"}, 2),
    ({1: 2, 2: 1}, {1: "align", 2: "align"}, 2),
    ({2: 3, 3: 2}, {2: "align", 3: "align"}, 3),
])
def test_invalid_align_settings_fail_before_save_and_on_load(tmp_path, targets, modes, count):
    path = tmp_path / "settings.json"
    settings = replace(default_settings(count), sequence_align_targets=targets, sequence_modes=modes)
    with pytest.raises(SettingsError):
        save_settings(path, settings)
    assert not path.exists()
    path.write_text(json.dumps({"run_prefs": {"slot_count": count},
                                "sequence_align_targets": targets, "sequence_modes": modes}))
    with pytest.raises(SettingsError):
        load_settings(path)


def test_slot_one_can_follow_another_claim_slot_and_round_trip(tmp_path):
    settings = replace(default_settings(2), sequence_modes={1: "align", 2: "always"},
                       sequence_align_targets={1: 2, 2: 1})
    path = tmp_path / "settings.json"
    save_settings(path, settings)
    assert load_settings(path, 2) == settings
    assert "sequence_target_days" not in json.loads(path.read_text())


def test_dependency_order_chains_and_unselected_targets():
    modes = {2: "align", 3: "align"}
    targets = {2: 1, 3: 2}
    assert order_sequence_slots([4, 3, 5, 2, 1, 4], modes, targets) == [4, 1, 2, 3, 5]
    assert order_sequence_slots([4, 3, 5], modes, targets) == [4, 3, 5]
    with pytest.raises(SettingsError, match="cycle"):
        order_sequence_slots([2, 3], modes, {2: 3, 3: 2})


@pytest.mark.parametrize("stamp,expected", [
    ("2026-10-20T00:00:00+00:00", (14, True)),
    ("2026-10-19T23:59:59+00:00", (1, False)),
    ("2026-10-18T23:59:59+00:00", (None, False)),
    ("2026-10-20T13:00:00+00:00", (None, False)),
    ("2026-10-20T01:00:00", (None, False)),
    ("bad", (None, False)), (None, (None, False)),
])
def test_recorded_day_uses_reset_boundary_and_wraparound(stamp, expected):
    assert recorded_day_today({"claimed_day": 14, "next_day": 1, "claimed_utc": stamp}, NOW) == expected


@pytest.mark.parametrize("record", [None, [], {}, observed(True), observed(15),
                                     observed(3, pending_claim={"day": 4})])
def test_invalid_or_pending_target_is_unknown(record):
    assert recorded_day_today(record, NOW) == (None, False)


@pytest.mark.parametrize("now,stamp", [
    ("2026-11-02T02:00:00+00:00", "2026-11-01T00:30:00+00:00"),
    ("2026-03-09T02:00:00+00:00", "2026-03-08T02:00:00+00:00"),
])
def test_previous_reset_cycle_across_dst(now, stamp):
    assert recorded_day_today(observed(14, datetime.fromisoformat(stamp)), datetime.fromisoformat(now)) == (1, False)


def test_live_target_claim_wins_over_saved_history(monkeypatch):
    runner = panel_runner(day=6, claimed_days_today={1: (6, datetime.now(timezone.utc))})
    monkeypatch.setattr(engine, "load_claim_state", lambda: {"1": observed(10), "2": {"next_day": 1}})
    monkeypatch.setattr(engine, "update_claim_state", mock.Mock())
    assert runner.process_slot(2).day == 6
    runner.click_claim_button.assert_called_once()
    assert runner.claimed_days_today[2][0] == 6


@pytest.mark.parametrize("target_day,slot_day", [(10, 6), (14, 1), (1, 14)])
def test_live_mismatch_holds_without_claim_or_restore(monkeypatch, target_day, slot_day):
    runner = panel_runner(day=slot_day, claimed_days_today={1: (target_day, datetime.now(timezone.utc))})
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    runner.detector.matches.return_value = (True, 1.0)  # Restore offered.
    with pytest.raises(engine.SlotHeld, match=f"slot 2 is on Day {slot_day}, slot 1 on Day {target_day}"):
        runner.process_slot(2)
    runner.click_claim_button.assert_not_called()
    runner.mark_pending_claim.assert_not_called()


@pytest.mark.parametrize("age,day,expected_claim", [(0, 6, True), (0, 10, False),
                                                  (1, 5, True), (1, 6, False), (4, 10, True)])
def test_unselected_target_today_previous_and_stale(monkeypatch, age, day, expected_claim):
    stamp = datetime.now(timezone.utc) - timedelta(days=age)
    runner = panel_runner(day=6)
    monkeypatch.setattr(engine, "load_claim_state", lambda: {"1": observed(day, stamp)})
    monkeypatch.setattr(engine, "update_claim_state", mock.Mock())
    if expected_claim:
        assert runner.process_slot(2).day == 6
    else:
        with pytest.raises(engine.SlotHeld):
            runner.process_slot(2)
        runner.click_claim_button.assert_not_called()


def test_unknown_target_claims_and_explains_why(monkeypatch):
    logged = []
    runner = panel_runner(day=1, log=logged.append)
    monkeypatch.setattr(engine, "load_claim_state", lambda: {"2": {"next_day": 6}})
    monkeypatch.setattr(engine, "update_claim_state", mock.Mock())
    assert runner.process_slot(2).day == 1
    assert any("unknown; claiming the live Day 1" in line for line in logged)


def test_chain_waits_if_target_was_held(monkeypatch):
    runner = panel_runner(results=[engine.SlotResult(1, "held", 6, 0)])
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    with pytest.raises(engine.SlotHeld, match="target skipped today"):
        runner.process_slot(2)
    runner.click_claim_button.assert_not_called()


def test_runner_reorders_dependencies_and_rejects_cycles():
    with mock.patch.object(engine, "VisualDetector"), mock.patch.object(engine, "DirectInput"):
        runner = engine.DBOGRunner([3, 2, 1], lambda _: None,
                                   slot_mismatch_policies={2: "align", 3: "align"},
                                   sequence_align_targets={2: 1, 3: 2})
        assert runner.slots == [1, 2, 3]
        with pytest.raises(SettingsError, match="cycle"):
            engine.DBOGRunner([2, 3], lambda _: None, slot_mismatch_policies={2: "align", 3: "align"},
                              sequence_align_targets={2: 3, 3: 2})


def test_bridge_start_and_api_shapes(tmp_path, monkeypatch):
    api = bridge.XynClaimAPI(tmp_path / "settings.json")
    settings = replace(default_settings(3), sequence_align_targets={2: 1, 3: 2})
    monkeypatch.setattr(api, "_settings", lambda: settings)
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    monkeypatch.setattr(api, "_all_history", lambda: [])
    runner = mock.Mock(results=[])
    runner.run.return_value = engine.RunReport("test", "start", "end", 0, "owner", "private", 3, [], [])
    with mock.patch.object(engine, "DBOGRunner", return_value=runner) as constructor:
        started = api.start_run({"slot_count": 3, "slots": [3, 2, 1]})
        api._worker.join(2)
    assert started["slots"] == constructor.call_args.args[0] == [1, 2, 3]
    assert started["held_slots"] == []
    assert constructor.call_args.kwargs["sequence_align_targets"] == {2: 1, 3: 2}
    assert api.get_state()["settings"]["sequence_align_targets"] == {"2": 1, "3": 2}
    assert [slot["align_target"] for slot in api.get_streak()["slots"]] == [None, 1, 2]


@pytest.mark.parametrize("payload", [
    {"sequence_align_targets": {"2": True}}, {"sequence_align_targets": {"2": 2}},
    {"sequence_align_targets": {"2": 13}}, {"sequence_modes": {"1": "align"}},
    {"sequence_modes": {"2": []}},
    {"sequence_align_targets": {"2": 3, "3": 2}},
])
def test_bridge_rejects_invalid_plan_without_writing(tmp_path, payload):
    api = bridge.XynClaimAPI(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        api.save_plan(payload)
    assert not api.settings_path.exists()


def test_in_run_target_without_a_claim_does_not_use_yesterdays_prediction(monkeypatch):
    logged = []
    runner = panel_runner(day=1, slots=[1, 2], log=logged.append,
                          results=[engine.SlotResult(1, "no_daily", None, 0)])
    previous = datetime.now(timezone.utc) - timedelta(days=1)
    monkeypatch.setattr(engine, "load_claim_state", lambda: {"1": observed(9, previous)})
    monkeypatch.setattr(engine, "update_claim_state", mock.Mock())
    assert runner.process_slot(2).day == 1
    assert any("unknown" in line for line in logged)


def test_unreadable_held_target_does_not_permanently_block_followers(monkeypatch):
    runner = panel_runner(day=1, slots=[1, 2], results=[engine.SlotResult(1, "held", None, 0)])
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    monkeypatch.setattr(engine, "update_claim_state", mock.Mock())
    assert runner.process_slot(2).day == 1


def test_chain_claims_live_days_in_order_even_when_saved_days_differ(monkeypatch):
    runner = panel_runner(day=1, slots=[1, 2, 3])
    runner.slot_mismatch_policies = {2: "align", 3: "align"}
    runner.sequence_align_targets = {2: 1, 3: 2}
    monkeypatch.setattr(engine, "load_claim_state", lambda: {"2": {"next_day": 6}, "3": {"next_day": 10}})
    monkeypatch.setattr(engine, "update_claim_state", mock.Mock())
    for slot in (1, 2, 3):
        runner.results.append(runner.process_slot(slot))
    assert [result.day for result in runner.results] == [1, 1, 1]
    assert runner.click_claim_button.call_count == 3


def test_held_live_day_is_preserved_for_dependent_slots(run_environment):
    runner = run_environment(slots=[2, 3])
    runner.process_slot = mock.Mock(side_effect=engine.SlotHeld("waiting to line up", 6))
    report = runner.run()
    assert [(result.slot, result.status, result.day) for result in report.slots] == [(2, "held", 6), (3, "held", 6)]


@pytest.mark.parametrize("targets", [{"2": None}, {"13": 1}, {"wat": 1}, {"02": 1}])
def test_load_rejects_invalid_target_entries(tmp_path, targets):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"sequence_align_targets": targets}))
    with pytest.raises(SettingsError):
        load_settings(path)


@pytest.mark.parametrize("key", [True, 1.5, "01", "wat", 13])
def test_save_plan_rejects_non_slot_keys(tmp_path, key):
    api = bridge.XynClaimAPI(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        api.save_plan({"sequence_align_targets": {key: 2}})
    assert not api.settings_path.exists()


def test_bridge_keeps_user_order_without_dependencies(tmp_path, monkeypatch):
    api = bridge.XynClaimAPI(tmp_path / "settings.json")
    settings = replace(default_settings(4), sequence_modes=dict.fromkeys(range(1, 5), "always"))
    monkeypatch.setattr(api, "_settings", lambda: settings)
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    runner = mock.Mock(results=[])
    runner.run.return_value = engine.RunReport("test", "start", "end", 0, "owner", "private", 4, [], [])
    with mock.patch.object(engine, "DBOGRunner", return_value=runner) as constructor:
        started = api.start_run({"slot_count": 4, "slots": [4, 2, 1, 4]})
        api._worker.join(2)
    assert started["slots"] == constructor.call_args.args[0] == [4, 2, 1]
