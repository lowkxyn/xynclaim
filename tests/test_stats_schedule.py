from dataclasses import replace
from datetime import date, datetime, timezone
import csv
import json
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

import pytest

from xynclaim import engine
from xynclaim import app as xynclaim_app
from xynclaim import schedule
from xynclaim.settings import default_settings, load_settings, save_settings
from xynclaim.stats import summarize
from xynclaim.bridge import XynClaimAPI


def run_record(stamp, slots, seconds=60):
    return {"started_utc": stamp, "total_seconds": seconds, "slots": slots}


def slot(number, reward, status="claimed_verified", seconds=10):
    return {"slot": number, "status": status, "day": 3, "seconds": seconds,
            "received_reward": reward}


def test_stats_empty_history():
    summary = summarize([], {}, 2, date(2026, 9, 28))
    assert summary["totals"]["runs"] == 0
    assert summary["totals"]["avg_run_seconds"] is None
    assert len(summary["days"]) == 14
    assert all(day["run_seconds"] is None for day in summary["days"])
    assert summary["slots"][0]["streak_day"] is None


def test_stats_rewards_and_missed_statuses():
    records = [run_record("2026-09-28T12:00:00+00:00", [
        slot(1, "10x Holy Water; 62,500 Zeni; 2X EXP for 30 min; +10 Mastery Points (global reward locked)"),
        slot(2, "4-Star Dragon Ball; 2X Zeni for 60 min; 2X Drops for 1 min 30 s; +100 Mastery Points (all Dragon Balls held)"),
        slot(1, None, "claimed_unverified"),
        slot(2, None, "failed"),
        slot(2, None, "held"),
    ])]
    summary = summarize(records, {"1": {"claimed_day": 3}}, 2, date(2026, 9, 28))
    assert summary["totals"]["claims"] == 3
    assert summary["totals"]["verified"] == 2
    assert summary["totals"]["unread"] == 1
    assert summary["collected"]["zeni"] == 62500
    assert summary["collected"]["mastery_points"] == 110
    assert summary["collected"]["xp_boost_minutes"] == 30
    assert summary["collected"]["zeni_boost_minutes"] == 60
    assert summary["collected"]["drop_boost_minutes"] == 1.5
    assert summary["collected"]["dragon_balls_unique"] == 1
    assert summary["collected"]["consumables"] == 10
    assert summary["slots"][1]["missed"] == 1


def test_stats_ignores_unknown_rewards_and_sorts_top_items():
    rewards = "; ".join(["1x Zebra", "3x Apple", "3x Banana", "unknown item", "4x Apple"])
    summary = summarize([run_record("2026-09-28T12:00:00+00:00", [slot(1, rewards)])], {}, 1)
    assert summary["items"] == [{"name": "Apple", "count": 7},
                                {"name": "Banana", "count": 3}, {"name": "Zebra", "count": 1}]


def test_stats_consecutive_days_and_read_global_lock():
    records = [run_record(f"2026-09-{day:02d}T12:00:00+00:00", [
        slot(1, "+10 Mastery Points (global reward locked)", "claimed_global_locked")
    ]) for day in (25, 26, 28)]
    summary = summarize(records, {}, 1, date(2026, 9, 28))
    assert summary["totals"]["longest_streak_days"] == 2
    assert summary["totals"]["longest_streak_slots"] == [1]
    assert summary["totals"]["verified"] == 3
    assert summary["collected"]["mastery_points"] == 30


def test_stats_uses_local_day_boundary(monkeypatch):
    class LocalDateTime(datetime):
        def astimezone(self, tz=None):
            return super().astimezone(timezone.utc).replace(tzinfo=timezone.utc) + __import__("datetime").timedelta(hours=2)
    monkeypatch.setattr("xynclaim.stats.datetime", LocalDateTime)
    summary = summarize([run_record("2026-09-27T23:30:00+00:00", [slot(1, "1,000 Zeni")])], {}, 1, date(2026, 9, 28))
    assert summary["days"][-1]["claims"] == 1


def test_run_prefs_round_trip_and_old_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 2}), encoding="utf-8")
    assert load_settings(path).slots == tuple(range(1, 13))
    api = XynClaimAPI(path)
    saved = api.save_run_prefs({"slot_count": 3, "slots": [1, 3], "access_mode": "shared",
                                "close_when_done": True, "public_fallback": False, "server_code": "NEVER-SAVE"})
    assert saved["slots"] == [1, 3]
    assert api.get_state()["settings"]["access_mode"] == "shared"
    assert "NEVER-SAVE" not in path.read_text(encoding="utf-8")


def test_scheduled_day1_uses_existing_hold_mode(tmp_path):
    api = XynClaimAPI(tmp_path / "settings.json")
    report = engine.RunReport("test", "start", "finish", 1.0, "owner", "private", 2, [], [])
    runner = mock.MagicMock()
    runner.run.return_value = report
    with mock.patch.object(engine, "load_claim_state", return_value={
        "1": {"next_day": 1}, "2": {"next_day": 2}
    }), mock.patch.object(engine, "DBOGRunner", return_value=runner) as runner_type:
        started = api.start_run({"slot_count": 2, "slots": [1, 2], "access_mode": "owner",
                                 "close_when_done": False, "public_fallback": False,
                                 "scheduled_day1": "hold"})
        api._worker.join(2)
    assert started["slots"] == [2]
    assert started["held_slots"] == [1]
    assert runner_type.call_args.kwargs["slot_mismatch_policies"] == {2: "align"}


def test_receipt_rejects_traversal(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (tmp_path / "secret.png").write_bytes(b"secret")
    (report_dir / "receipt.png").write_bytes(b"png")
    monkeypatch.setattr("xynclaim.bridge.engine.REPORT_DIR", report_dir)
    api = XynClaimAPI(tmp_path / "settings.json")
    monkeypatch.setattr(api, "_all_history", lambda: [run_record("run", [
        dict(slot(1, "item"), receipt_image="../secret.png"),
        dict(slot(2, "item"), receipt_image="receipt.png"),
    ])])
    assert api.get_receipt("run", 1) == {"data_uri": None}
    assert api.get_run("run")["slots"][0]["has_receipt"] is False
    assert api.get_receipt("run", 2)["data_uri"].startswith("data:image/png;base64,")


def test_schedule_xml_action_and_interactive_settings():
    settings = replace(default_settings(), schedule_enabled=True, schedule_catch_up=True)
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    root = ET.fromstring(schedule.task_xml(settings, Path("C:/App/xynclaim/__main__.py"),
                                         executable="C:/Python/python.exe", now_utc=now))
    values = {element.tag.rsplit("}", 1)[-1]: element.text for element in root.iter()}
    assert values["Command"].endswith("pythonw.exe")
    assert values["Arguments"] == '"C:\\App\\xynclaim\\__main__.py" --scheduled' or values["Arguments"] == '"C:/App/xynclaim/__main__.py" --scheduled'
    assert values["LogonType"] == "InteractiveToken"
    assert values["StartWhenAvailable"] == "true"
    frozen = ET.fromstring(schedule.task_xml(settings, Path("C:/App/XynClaim.exe"), frozen=True,
                                           executable="C:/App/XynClaim.exe", now_utc=now))
    frozen_values = {element.tag.rsplit("}", 1)[-1]: element.text for element in frozen.iter()}
    assert frozen_values["Command"] == "C:/App/XynClaim.exe"
    assert frozen_values["Arguments"] == "--scheduled"


def test_offset_can_run_after_the_reset_on_the_same_day():
    reset, scheduled = schedule.next_schedule_time(180, datetime(2026, 9, 28, 2, tzinfo=timezone.utc))
    assert reset.astimezone(timezone.utc).hour == 0
    assert scheduled.astimezone(timezone.utc).hour == 3


def test_disabled_schedule_deletes_task_with_mocked_subprocess():
    with mock.patch.object(schedule, "_run_task_command") as command:
        command.return_value.returncode = 0
        schedule.sync_task(default_settings(), Path("xynclaim/__main__.py"))
    assert command.call_args_list[0].args[0][:2] == ["schtasks", "/Query"]
    assert command.call_args_list[1].args[0][:2] == ["schtasks", "/Delete"]


def test_enabled_schedule_registers_xml_with_mocked_subprocess():
    settings = replace(default_settings(), schedule_enabled=True)
    with mock.patch.object(schedule, "_run_task_command") as command:
        command.return_value.returncode = 0
        schedule.sync_task(settings, Path("C:/App/xynclaim/__main__.py"))
    args = command.call_args.args[0]
    assert args[:2] == ["schtasks", "/Create"]
    assert args[args.index("/TN") + 1] == schedule.TASK_NAME


def test_task_registration_failure_still_opens_window():
    settings = replace(default_settings(), schedule_enabled=True)
    with mock.patch.object(xynclaim_app.engine, "migrate_legacy_data"), mock.patch.object(
        xynclaim_app, "load_settings", return_value=settings
    ), mock.patch.object(xynclaim_app.schedule, "sync_task", side_effect=RuntimeError("denied")), mock.patch.object(
        xynclaim_app, "launch_webview"
    ) as launch:
        xynclaim_app.main([])
    assert launch.call_args.kwargs["scheduled"] is False
    api = mock.MagicMock()
    launch.call_args.kwargs["on_ready"](api)
    assert "denied" in api._safe_log.call_args.args[0]


@pytest.mark.parametrize("slots", [None, 7, "old format", {"1": {"status": "claimed_verified"}}])
def test_stats_ignores_invalid_slot_containers(slots):
    summary = summarize([run_record("2026-09-28T12:00:00+00:00", slots)], {}, 1)
    assert summary["totals"]["claims"] == 0


def test_stats_ignores_unhashable_status_and_invalid_durations():
    records = [run_record("2026-09-28T12:00:00+00:00", [
        slot(1, None, [], float("inf")), slot(1, None, {}, float("nan")),
        slot(1, "10 Zeni", seconds=-10),
    ], float("nan"))]
    summary = summarize(records, {}, 1)
    assert summary["totals"]["claims"] == 1
    assert summary["totals"]["avg_run_seconds"] is None
    assert summary["totals"]["avg_slot_seconds"] is None
    assert summary["slots"][0]["missed"] == 2
    json.dumps(summary, allow_nan=False)


@pytest.mark.parametrize("slots", [None, 7, "old format", [None, {}, 3]])
def test_history_endpoints_ignore_invalid_slots(tmp_path, monkeypatch, slots):
    history = tmp_path / "history.jsonl"
    history.write_text(json.dumps(run_record("2026-09-28T12:00:00+00:00", slots)) + '\n{"partial":', encoding="utf-8")
    monkeypatch.setattr(engine, "HISTORY_PATH", history)
    monkeypatch.setattr(engine, "HISTORY_LOCK_PATH", tmp_path / ".history.lock")
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    api = XynClaimAPI(tmp_path / "settings.json")
    state = api.get_state()
    assert all(isinstance(result, dict) for result in state["history"][0]["slots"])
    assert isinstance(api.get_run("2026-09-28T12:00:00+00:00")["slots"], list)
    assert isinstance(api.get_streak()["slots"], list)
    assert isinstance(api.get_stats()["totals"], dict)
    assert Path(api.export_stats_csv()["path"]).is_file()


def test_streak_validates_days_and_shows_unread_claim(tmp_path, monkeypatch):
    api = XynClaimAPI(tmp_path / "settings.json")
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(api, "_all_history", lambda: [run_record(now, [slot(1, None, "claimed_unverified")])])
    monkeypatch.setattr(engine, "load_claim_state", lambda: {"1": {"claimed_day": True, "next_day": {"old": 3}}})
    streak = api.get_streak()["slots"][0]
    assert streak["streak_day"] is None
    assert streak["next_day"] is None
    assert streak["next_reward"] is None
    assert streak["claimed_today"] == "Claimed (popup not read)"


def test_csv_preserves_commas_quotes_and_newlines(tmp_path, monkeypatch):
    api = XynClaimAPI(tmp_path / "settings.json")
    reward = '2x Water, "Holy"\nSpecial'
    monkeypatch.setattr(api, "_all_history", lambda: [run_record("2026-09-28T12:00:00+00:00", [slot(1, reward)])])
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = Path(api.export_stats_csv()["path"])
    with path.open(encoding="utf-8-sig", newline="") as exported:
        rows = list(csv.reader(exported))
    assert len(rows) == 2
    assert rows[1][4] == reward


def test_schedule_xml_uses_local_wall_clock_boundary():
    settings = replace(default_settings(), schedule_enabled=True)
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    root = ET.fromstring(schedule.task_xml(settings, Path("xynclaim/__main__.py"), now_utc=now))
    stamp = root.find(f".//{{{schedule.NS}}}StartBoundary").text
    boundary = datetime.fromisoformat(stamp)
    _, scheduled = schedule.next_schedule_time(settings.schedule_offset_minutes, now)
    assert boundary.tzinfo is None
    assert boundary == scheduled.replace(tzinfo=None)


def test_task_registration_writes_actual_utf16_xml():
    settings = replace(default_settings(), schedule_enabled=True)

    def register(args):
        if args[1] == "/Query":
            return mock.Mock(returncode=1, stdout="")
        path = Path(args[args.index("/XML") + 1])
        content = path.read_bytes()
        assert content.startswith((b"\xff\xfe", b"\xfe\xff"))
        root = ET.fromstring(content)
        assert root.tag == f"{{{schedule.NS}}}Task"
        return mock.Mock(returncode=0)

    with mock.patch.object(schedule, "_run_task_command", side_effect=register):
        schedule.sync_task(settings, Path("xynclaim/__main__.py"))


def test_schedule_info_distinguishes_next_reset_from_pending_run(monkeypatch):
    now = datetime(2026, 10, 25, 0, 10, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(schedule, "datetime", FixedDateTime)
    monkeypatch.setattr(schedule, "task_present", lambda: True)
    info = schedule.schedule_info(replace(default_settings(), schedule_enabled=True))
    assert datetime.fromisoformat(info["next_run_local"]).astimezone(timezone.utc) == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    assert datetime.fromisoformat(info["next_reset_utc"]) == datetime(2026, 10, 26, tzinfo=timezone.utc)


def test_invalid_local_timestamp_does_not_crash_read_endpoints(tmp_path, monkeypatch):
    records = [run_record("0001-01-01T00:00:00+00:00", [slot(1, "10 Zeni")])]
    api = XynClaimAPI(tmp_path / "settings.json")
    monkeypatch.setattr(api, "_all_history", lambda: records)
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert summarize(records, {}, 1)["totals"]["claims"] == 1
    assert len(api.get_streak()["slots"]) == 12
    assert Path(api.export_stats_csv()["path"]).is_file()


def test_receipt_ignores_invalid_filename_and_deleted_file(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "receipt.png").write_bytes(b"png")
    monkeypatch.setattr(engine, "REPORT_DIR", report_dir)
    api = XynClaimAPI(tmp_path / "settings.json")
    monkeypatch.setattr(api, "_all_history", lambda: [run_record("run", [
        dict(slot(1, "item"), receipt_image="bad\0.png"),
        dict(slot(2, "item"), receipt_image="receipt.png"),
    ])])
    assert api.get_receipt("run", 1) == {"data_uri": None}
    with mock.patch.object(Path, "read_bytes", side_effect=FileNotFoundError("rotated receipt")):
        assert api.get_receipt("run", 2) == {"data_uri": None}


def test_failed_schedule_registration_preserves_saved_preferences(tmp_path):
    path = tmp_path / "settings.json"
    original = replace(default_settings(), schedule_offset_minutes=60)
    save_settings(path, original)
    api = XynClaimAPI(path)
    with mock.patch.object(schedule, "sync_task", side_effect=RuntimeError("registration denied")):
        with pytest.raises(RuntimeError, match="registration denied"):
            api.save_schedule({"enabled": True, "offset_minutes": 30, "catch_up": True,
                               "day1": "claim", "roblox_busy": "wait"})
    assert load_settings(path) == original


def test_successful_schedule_save_preserves_other_preferences(tmp_path):
    path = tmp_path / "settings.json"
    original = replace(default_settings(), slot_count=2, slots=(2,), mute_roblox=True)
    save_settings(path, original)
    api = XynClaimAPI(path)
    with mock.patch.object(schedule, "sync_task") as sync, mock.patch.object(
        schedule, "schedule_info", return_value={"enabled": True}
    ):
        assert api.save_schedule({"enabled": True, "offset_minutes": 60, "catch_up": False,
                                  "day1": "hold", "roblox_busy": "skip"}) == {"enabled": True}
    saved = load_settings(path)
    assert saved.slots == (2,)
    assert saved.mute_roblox is True
    assert saved.schedule_offset_minutes == 60
    assert sync.call_args.args[0] == saved


def test_unchanged_task_is_not_rewritten():
    settings = replace(default_settings(), schedule_enabled=True)
    app = Path("C:/App/XynClaim.exe")
    current = schedule.task_xml(settings, app, frozen=True, executable=str(app))
    # schtasks returns its own formatting, a declaration and an older start date.
    queried = '<?xml version="1.0" encoding="UTF-16"?>' + current.replace(current[current.index("<StartBoundary>") + 15:][:10], "2026-01-01")
    with mock.patch.object(schedule, "_run_task_command") as command:
        command.return_value.returncode = 0
        command.return_value.stdout = queried
        schedule.sync_task(settings, app, frozen=True, executable=str(app))
    assert [call.args[0][:2] for call in command.call_args_list] == [["schtasks", "/Query"]]


def test_task_is_rewritten_when_the_program_moved():
    settings = replace(default_settings(), schedule_enabled=True)
    old = schedule.task_xml(settings, Path("C:/Old/XynClaim.exe"), frozen=True, executable="C:/Old/XynClaim.exe")
    with mock.patch.object(schedule, "_run_task_command") as command:
        command.return_value.returncode = 0
        command.return_value.stdout = old
        schedule.sync_task(settings, Path("C:/New/XynClaim.exe"), frozen=True, executable="C:/New/XynClaim.exe")
    assert command.call_args.args[0][:2] == ["schtasks", "/Create"]


def test_task_signature_ignores_the_start_date_but_not_the_time():
    settings = replace(default_settings(), schedule_enabled=True)
    xml = schedule.task_xml(settings, Path("C:/App/XynClaim.exe"), frozen=True, executable="C:/App/XynClaim.exe")
    start = xml.index("<StartBoundary>") + 15
    other_day = xml[:start] + "2030-05-05" + xml[start + 10:]
    other_time = xml[:start + 11] + "23:59:00" + xml[start + 19:]
    assert schedule.task_signature(other_day) == schedule.task_signature(xml)
    assert schedule.task_signature(other_time) != schedule.task_signature(xml)
    assert schedule.task_signature("not xml") is None


@pytest.mark.parametrize("field,value", [
    ("UserId", "OTHERDOMAIN\\otheruser"),
    ("CalendarTrigger/Enabled", "false"),
    ("LogonType", "Password"),
    ("RunLevel", "HighestAvailable"),
    ("ExecutionTimeLimit", "PT1H"),
    ("MultipleInstancesPolicy", "Parallel"),
    ("Settings/Enabled", "false"),
])
def test_task_is_rewritten_when_principal_or_fixed_settings_change(field, value):
    settings = replace(default_settings(), schedule_enabled=True)
    app = Path("C:/App/XynClaim.exe")
    xml = schedule.task_xml(settings, app, frozen=True, executable=str(app))
    root = ET.fromstring(xml)
    path = "/".join(f"{{{schedule.NS}}}{part}" for part in field.split("/"))
    node = root.find(f".//{path}")
    if node is None:
        node = ET.SubElement(root.find(f"{{{schedule.NS}}}Settings"), f"{{{schedule.NS}}}Enabled")
    node.text = value
    changed = ET.tostring(root, encoding="unicode")
    assert schedule.task_signature(changed) != schedule.task_signature(xml)
    with mock.patch.object(schedule, "_run_task_command") as command:
        command.return_value.returncode = 0
        command.return_value.stdout = changed
        schedule.sync_task(settings, app, frozen=True, executable=str(app))
    assert [call.args[0][:2] for call in command.call_args_list] == [
        ["schtasks", "/Query"], ["schtasks", "/Create"]]


def test_task_signature_normalizes_user_case_and_default_enabled():
    xml = schedule.task_xml(default_settings(), Path("C:/App/XynClaim.exe"), frozen=True)
    root = ET.fromstring(xml)
    user = root.find(f".//{{{schedule.NS}}}UserId")
    user.text = user.text.swapcase()
    trigger = root.find(f".//{{{schedule.NS}}}CalendarTrigger")
    trigger.remove(trigger.find(f"{{{schedule.NS}}}Enabled"))
    assert schedule.task_signature(ET.tostring(root, encoding="unicode")) == schedule.task_signature(xml)


def test_task_signature_normalizes_stored_sid_and_omitted_run_level():
    xml = schedule.task_xml(default_settings(), Path("C:/App/XynClaim.exe"), frozen=True)
    root = ET.fromstring(xml)
    principal = root.find(f".//{{{schedule.NS}}}Principal")
    principal.remove(principal.find(f"{{{schedule.NS}}}RunLevel"))
    user = principal.find(f"{{{schedule.NS}}}UserId")
    sid = schedule._task_user_id(user.text)
    assert sid.startswith("s-1-")
    user.text = sid.upper()
    assert schedule.task_signature(ET.tostring(root, encoding="unicode")) == schedule.task_signature(xml)
