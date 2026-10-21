from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import csv
import json
from pathlib import Path

import pytest

from xynclaim import engine, stats
from xynclaim.bridge import XynClaimAPI
from xynclaim.settings import default_settings, save_settings


def result(slot=1, reward=None, status="claimed_verified", seconds=10, day=3):
    return {"slot": slot, "received_reward": reward, "status": status, "seconds": seconds, "day": day}


def record(stamp, results):
    return {"started_utc": stamp, "total_seconds": 60, "slots": results}


def test_empty_slot_summary_exact_shape():
    assert stats.summarize_slot([], {}, 12) == {
        "slot": 12, "claims": 0, "verified": 0, "verified_pct": 0, "current_day": None,
        "longest_streak": 0, "last_claimed_utc": None, "avg_seconds": None,
        "collected": {"zeni": 0, "mastery_points": 0, "xp_boost_minutes": 0,
                      "zeni_boost_minutes": 0, "drop_boost_minutes": 0, "dragon_balls": 0,
                      "dragon_balls_unique": 0, "consumables": 0, "addon_packs": 0},
        "items": [], "recent": [], "dragon_balls_detail": [],
    }


@pytest.mark.parametrize("slot", [True, False, 0, 13, -1, 1.0, "1", None, [], {}])
def test_slot_validation_for_summary_and_api(tmp_path, monkeypatch, slot):
    api = XynClaimAPI(tmp_path / "settings.json")
    monkeypatch.setattr(api, "_all_history", lambda: pytest.fail("Invalid slot must fail before reading history"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with pytest.raises(ValueError, match="integer between 1 and 12"):
        stats.summarize_slot([], {}, slot)
    with pytest.raises(ValueError, match="integer between 1 and 12"):
        api.get_slot_stats(slot)
    if slot is not None:  # None deliberately selects every slot for CSV export.
        with pytest.raises(ValueError, match="integer between 1 and 12"):
            api.export_stats_csv(slot)
    assert not (tmp_path / "Downloads").exists()


def test_slot_rewards_match_account_parser_and_ignore_other_slots():
    reward = ("10x Holy Water; 62,500 Zeni; 2X EXP for 30 min; +10 Mastery Points (global reward locked); "
              "4-Star Dragon Ball; 2X Zeni for 60 min; 2X Drops for 1 min 30 s; XP Add-on Pack")
    own_results = [result(reward=reward), result(reward="4-Star Dragon Ball; 3x Apple; 3x Banana"),
                   result(reward="3-Star Earth Dragon Ball; 1x Apple; 2X XP for 10 min"),
                   result(reward=None, status="claimed_unverified"),
                   result(reward="+100 Mastery Points", status="claimed_global_locked"),
                   result(reward="  ", status="claimed_global_locked"),
                   result(reward="999,999 Zeni; 999x Apple", status="failed"),
                   result(status="held"), result(status="skipped")]
    stamp = "2026-09-28T12:00:00+00:00"
    summary = stats.summarize_slot([record(stamp, [*own_results, result(5, "999,999 Zeni")])],
                                   {"1": {"claimed_day": 3, "claimed_utc": "2026-09-28T12:03:00+00:00"}}, 1)
    account = stats.summarize([record(stamp, own_results)], {}, 12)
    assert summary["collected"] == account["collected"] == {
        "zeni": 62500, "mastery_points": 110, "xp_boost_minutes": 40, "zeni_boost_minutes": 60,
        "drop_boost_minutes": 1.5, "dragon_balls": 3, "dragon_balls_unique": 2,
        "consumables": 17, "addon_packs": 1,
    }
    assert summary["items"] == [["Holy Water", 10], ["Apple", 4], ["Banana", 3], ["XP Add-on Pack", 1]]
    assert summary["dragon_balls_detail"] == [["3-Star", 1], ["4-Star", 2]]
    assert summary["claims"] == 6
    assert summary["verified"] == 4
    assert summary["verified_pct"] == 67
    assert summary["current_day"] == 3
    assert summary["last_claimed_utc"] == "2026-09-28T12:03:00+00:00"
    assert summary["avg_seconds"] == account["totals"]["avg_slot_seconds"] == 10
    assert len(summary["recent"]) == len(own_results)


def test_slot_lists_all_items_with_stable_count_and_name_order():
    reward = "; ".join([f"1x Item{number}" for number in range(10)] + ["2x Zebra", "2x Apple"])
    records = [record("2026-09-28T12:00:00+00:00", [result(reward=reward)])]
    summary = stats.summarize_slot(records, {}, 1)
    assert len(summary["items"]) == 12
    assert summary["items"][:2] == [["Apple", 2], ["Zebra", 2]]
    assert len(stats.summarize(records, {}, 1)["items"]) == 8


def test_slot_streak_uses_unique_consecutive_local_dates_and_ignores_failed_attempts():
    records = [record(f"2026-09-{day:02d}T12:00:00+00:00", [result()]) for day in (22, 24, 25, 25, 26, 28)]
    records.append(record("2026-09-27T12:00:00+00:00", [result(status="failed")]))
    records.append(record("2026-09-29T12:00:00+00:00", [result(2)]))
    summary = stats.summarize_slot(list(reversed(records)), {}, 1)
    assert summary["longest_streak"] == 3
    assert summary["last_claimed_utc"] == "2026-09-28T12:00:00+00:00"


def test_recent_is_last_14_attempts_newest_first_and_matches_csv_dates():
    start = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    records = [record((start + timedelta(days=offset)).isoformat(),
                      [result(day=offset % 14 + 1, status="failed" if offset == 18 else "claimed_verified")])
               for offset in range(20)]
    summary = stats.summarize_slot(list(reversed(records)), {}, 1)
    assert len(summary["recent"]) == 14
    assert summary["recent"][0] == {"date": "2026-09-20", "day": 6, "status": "claimed_verified",
                                       "received_reward": None, "seconds": 10.0}
    assert summary["recent"][1]["status"] == "failed"
    assert summary["recent"][-1]["date"] == "2026-09-07"


def test_recent_orders_actual_instants_across_timezones():
    records = [record("2026-09-28T14:00:00+03:00", [result(day=1)]),
               record("2026-09-28T10:30:00-01:00", [result(day=2)])]
    summary = stats.summarize_slot(records, {}, 1)
    assert [attempt["day"] for attempt in summary["recent"]] == [2, 1]
    assert summary["last_claimed_utc"] == "2026-09-28T11:30:00+00:00"


def test_same_timestamp_attempts_show_later_result_first():
    summary = stats.summarize_slot([record("2026-09-28T12:00:00+00:00", [
        result(status="failed"), result(status="claimed_verified")])], {}, 1)
    assert [attempt["status"] for attempt in summary["recent"]] == ["claimed_verified", "failed"]


@pytest.mark.parametrize("state", [None, [], {"claimed_day": True}, {"claimed_day": 0},
                                   {"claimed_day": 15}, {"claimed_day": "3"}, {"claimed_utc": {}}])
def test_invalid_claim_state_has_no_current_day_or_timestamp(state):
    summary = stats.summarize_slot([], {"1": state}, 1)
    assert summary["current_day"] is None
    assert summary["last_claimed_utc"] is None


def test_state_only_and_integer_state_keys():
    summary = stats.summarize_slot([], {12: {"claimed_day": 14, "claimed_utc": "2026-09-28T14:00:00+02:00"}}, 12)
    assert summary["current_day"] == 14
    assert summary["last_claimed_utc"] == "2026-09-28T12:00:00+00:00"
    assert summary["claims"] == 0


def test_older_or_invalid_state_stamp_does_not_hide_latest_history():
    records = [record("2026-09-28T12:00:00+00:00", [result()])]
    for stamp in ("invalid", "2026-09-01T12:00:00+00:00", None):
        summary = stats.summarize_slot(records, {"1": {"claimed_utc": stamp}}, 1)
        assert summary["last_claimed_utc"] == "2026-09-28T12:00:00+00:00"


def test_invalid_history_values_are_ignored_or_sanitized():
    records = [record("invalid", [result(status=[], seconds=float("inf"), day=True, reward={}),
                                  result(seconds=-1), result(seconds=True), result(seconds=float("nan")),
                                  result(True, "999 Zeni"), result("1", "999 Zeni"), None, 1]),
               record(None, None), record([], {"slot": 1})]
    summary = stats.summarize_slot(records, {}, 1)
    assert summary["claims"] == 3
    assert summary["avg_seconds"] is None
    assert summary["longest_streak"] == 0
    assert summary["collected"]["zeni"] == 0
    assert summary["last_claimed_utc"] is None
    assert summary["recent"][-1] == {"date": "", "day": None, "status": None,
                                        "received_reward": None, "seconds": None}
    json.dumps(summary, allow_nan=False)


def test_recent_and_streak_use_same_local_days_as_account(monkeypatch):
    class LocalDateTime(datetime):
        def astimezone(self, tz=None):
            return super().astimezone(tz or timezone(timedelta(hours=2)))

    monkeypatch.setattr(stats, "datetime", LocalDateTime)
    records = [record("2026-09-27T23:30:00+00:00", [result()])]
    summary = stats.summarize_slot(records, {}, 1)
    account = stats.summarize(records, {}, 1, date(2026, 9, 28))
    assert summary["recent"][0]["date"] == "2026-09-28"
    assert account["days"][-1]["claims"] == summary["claims"]


def test_slot_api_includes_historical_slots_outside_current_slot_count(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    save_settings(path, replace(default_settings(), slot_count=1, slots=(1,)))
    api = XynClaimAPI(path)
    monkeypatch.setattr(api, "_all_history", lambda: [record("2026-09-28T12:00:00+00:00", [result(12, "500 Zeni")])])
    monkeypatch.setattr(engine, "load_claim_state", lambda: {"12": {"claimed_day": 3}})
    summary = api.get_slot_stats(12)
    assert summary["slot"] == 12
    assert summary["claims"] == 1
    assert summary["current_day"] == 3
    assert summary["collected"]["zeni"] == 500


def test_csv_slot_filter_and_default_export(tmp_path, monkeypatch):
    api = XynClaimAPI(tmp_path / "settings.json")
    records = [record("2026-09-28T12:00:00+00:00", [result(1), result(3, '2x Water, "Holy"\nSpecial'),
                                                           result(True), result("3")]),
               record("invalid", [result(3, status="failed")])]
    monkeypatch.setattr(api, "_all_history", lambda: records)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = Path(api.export_stats_csv(3)["path"])
    assert path.name == f"XynClaim-slot3-history-{datetime.now().strftime('%Y%m%d')}.csv"
    with path.open(encoding="utf-8-sig", newline="") as exported:
        rows = list(csv.reader(exported))
    assert rows[0] == ["date", "slot", "day", "status", "received_reward", "seconds"]
    assert len(rows) == 3
    assert all(row[1] == "3" for row in rows[1:])
    assert rows[1][4] == '2x Water, "Holy"\nSpecial'
    assert rows[2][0] == ""
    path = Path(api.export_stats_csv()["path"])
    assert path.name.startswith("XynClaim-history-")
    with path.open(encoding="utf-8-sig", newline="") as exported:
        assert len(list(csv.reader(exported))) == 6
    assert api.export_stats_csv(None) == {"path": str(path)}


def test_csv_keeps_rewards_from_running_as_spreadsheet_formulas(tmp_path, monkeypatch):
    api = XynClaimAPI(tmp_path / "settings.json")
    rewards = ["+10 Mastery Points", "=HYPERLINK(1)", "-1", "@x", "2x Water", None]
    monkeypatch.setattr(api, "_all_history", lambda: [record("2026-09-28T12:00:00+00:00",
                                                             [result(1, reward) for reward in rewards])])
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with Path(api.export_stats_csv(1)["path"]).open(encoding="utf-8-sig", newline="") as exported:
        cells = [row[4] for row in list(csv.reader(exported))[1:]]
    assert cells == ["'+10 Mastery Points", "'=HYPERLINK(1)", "'-1", "'@x", "2x Water", ""]


def test_csv_empty_slot_has_header_only(tmp_path, monkeypatch):
    api = XynClaimAPI(tmp_path / "settings.json")
    monkeypatch.setattr(api, "_all_history", lambda: [record("stamp", [result(1)])])
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = Path(api.export_stats_csv(12)["path"])
    with path.open(encoding="utf-8-sig", newline="") as exported:
        assert len(list(csv.reader(exported))) == 1


@pytest.mark.parametrize("installed", [False, True])
def test_roblox_setup_message(tmp_path, monkeypatch, installed):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(engine, "list_displays", lambda: [])
    if installed:
        player = tmp_path / "Roblox" / "Versions" / "version-test" / "RobloxPlayerBeta.exe"
        player.parent.mkdir(parents=True)
        player.touch()
    check = XynClaimAPI(tmp_path / "settings.json").setup_checks()[0]
    assert check["title"] == "Roblox installed"
    assert check["state"] == ("ok" if installed else "bad")
    assert check["detail"] == ("Roblox player found" if installed else
                               "XynClaim couldn't find Roblox for this Windows user. Install Roblox from roblox.com. "
                               "XynClaim never installs anything itself.")


@pytest.mark.parametrize("reward,expected", [
    ("+1 3-Star Dragon Ball", [["3-Star", 1]]),
    ("+1 3-Star Dragon Ball.", [["3-Star", 1]]),
    ("Received 3-Star Namekian Dragon Ball.", [["3-Star", 1]]),
    ("Received +2 7-Star Black Star Dragon Ball", [["7-Star", 2]]),
    ("I-Star Dragon Ball; Received I-Star Dragon Ball", []),
    ("7-Star Dragon Ball; +2 3-Star Dragon Ball.;1-Star Dragon Ball", [["1-Star", 1], ["3-Star", 2], ["7-Star", 1]]),
    (" +1 3-star dragon ball. ; 3-Star Earth Dragon Ball ", [["3-Star", 2]]),
    ("+0 3-Star Dragon Ball", []),
    ("+1 8-Star Dragon Ball; +1 0-Star Dragon Ball", []),
    ("-1 3-Star Dragon Ball; +x 3-Star Dragon Ball; +1 3-Star Dragon Ball extra", []),
    ("+1 3-Star Dragon Balls; +1 3-Star Dragon Ball..", []),
    (None, []), (42, []), ({}, []), ("", []),
])
def test_dragon_ball_receipt_formats(reward, expected):
    summary = stats.summarize_slot([record("2026-10-19T12:00:00Z", [result(reward=reward)])], {}, 1)
    assert summary["dragon_balls_detail"] == expected
    assert summary["collected"]["dragon_balls"] == sum(count for _, count in expected)
    assert summary["collected"]["dragon_balls_unique"] == len(expected)


def test_dragon_balls_are_received_only_and_slot_specific(tmp_path, monkeypatch):
    records = [record("2026-10-19T12:00:00Z", [
        result(reward="+1 3-Star Dragon Ball."), result(reward="+1 3-Star Dragon Ball"),
        result(2, "+9 7-Star Dragon Ball"), result(reward="+7 1-Star Dragon Ball", status="held"),
        {**result(reward=None), "reward": "5-Star Dragon Ball"},
    ])]
    api = XynClaimAPI(tmp_path / "settings.json")
    monkeypatch.setattr(api, "_all_history", lambda: records)
    monkeypatch.setattr(engine, "load_claim_state", lambda: {})
    assert api.get_slot_stats(1)["dragon_balls_detail"] == [["3-Star", 2]]
    assert api.get_slot_stats(2)["dragon_balls_detail"] == [["7-Star", 9]]
