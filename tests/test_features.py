from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from xynclaim.settings import (
    PlannerSettings,
    SettingsError,
    default_settings,
    load_settings,
    next_reported_reset,
    projected_day14,
    plan_sequence_claims,
    save_settings,
    slots_due_today,
)
from xynclaim.history import history_days, read_history
from xynclaim.dpapi import protect_secret, unprotect_secret
from xynclaim.webhook import validate_webhook_url, webhook_payload


def test_settings_round_trip(tmp_path: Path) -> None:
    original = PlannerSettings(
        follow_schedule=True,
        missed_day_rule="pauses",
        claim_weekdays={1: (0, 2, 4), 2: (6,)},
        day14_targets={1: 4, 2: 6},
        day14_week_parity={1: 0, 2: 1},
        webhook_enabled=True,
        webhook_blob="encrypted",
        sequence_modes={1: "align", 2: "hold"},
        sequence_align_targets={1: 2, 2: 1},
        slot_count=2,
        slots=(1, 2),
        run_monitor="secondary",
        mute_roblox=True,
        low_graphics=True,
        low_cpu_priority=True,
    )
    path = tmp_path / "settings.json"
    save_settings(path, original)
    assert load_settings(path, slot_count=2) == original


def test_invalid_existing_settings_block_instead_of_claiming_all(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SettingsError, match="unreadable"):
        load_settings(path, slot_count=2)
    assert path.read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("payload", [
    {"missed_day_rule": []},
    {"run_prefs": {"access_mode": {}}},
    {"schedule": {"day1": []}},
    {"schedule": {"roblox_busy": {}}},
    {"runtime": {"run_monitor": []}},
    {"sequence_modes": {"1": {}}},
    {"day14_week_parity": {"1": []}},
])
def test_malformed_enum_settings_raise_settings_error(tmp_path: Path, payload: dict) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SettingsError):
        load_settings(path)


def test_partial_utf8_history_line_does_not_discard_complete_records(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    record = {"started_utc": "2026-09-29T12:00:00Z", "slots": []}
    path.write_bytes(json.dumps(record).encode("utf-8") + b'\n{"detail": "\xe2\x80')
    assert read_history(path) == [record]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_history_fields_are_unknown_not_invalid_bridge_json(tmp_path: Path, constant: str) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text('{"slots": [{"seconds": ' + constant + '}], "future": ' + constant + '}\n', encoding="utf-8")
    records = read_history(path)
    json.dumps(records, allow_nan=False)
    assert records[0]["slots"][0]["seconds"] is None
    assert records[0]["future"] is None


@pytest.mark.parametrize(
    "field,payload",
    [
        ("claim_weekdays", {"1": [True]}),
        ("day14_targets", {"1": True}),
        ("day14_week_parity", {"1": True}),
        ("sequence_align_targets", {"1": True}),
        ("sequence_modes", {"1": "not-a-mode"}),
    ],
)
def test_settings_reject_booleans_disguised_as_integers(
    tmp_path: Path, field: str, payload: dict[str, object]
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({field: payload}), encoding="utf-8")
    with pytest.raises(SettingsError):
        load_settings(path)


def test_legacy_weekday_plan_never_suppresses_a_slot() -> None:
    settings = default_settings(3)
    custom = PlannerSettings(
        **{
            **settings.__dict__,
            "follow_schedule": True,
            "claim_weekdays": {1: (0,), 2: (1,), 3: (0, 1)},
        }
    )
    assert slots_due_today(custom, [1, 2, 3], date(2026, 7, 27)) == [1, 2, 3]
    assert slots_due_today(settings, [1, 2, 3], date(2026, 7, 27)) == [1, 2, 3]


def test_legacy_settings_load_with_sequence_defaults_and_ignore_weekdays(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "follow_schedule": True,
                "missed_day_rule": "resets",
                "claim_weekdays": {"1": []},
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(path, slot_count=2)
    assert settings.sequence_modes == {1: "always", 2: "always"}
    assert settings.sequence_align_targets == {2: 1}
    assert slots_due_today(settings, [1, 2], date(2026, 7, 27)) == [1, 2]


def test_sequence_aligned_slots_claim_together() -> None:
    decision = plan_sequence_claims(default_settings(2), [2, 1], {})
    assert decision.due_slots == (1, 2)
    assert decision.held_slots == ()


def test_sequence_does_not_hold_on_expected_mismatch() -> None:
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    state = {"1": {"claimed_day": 9, "next_day": 10, "claimed_utc": "2026-10-19T12:00:00+00:00"},
             "2": {"claimed_day": 5, "next_day": 6, "claimed_utc": "2026-10-19T12:00:00+00:00"}}
    decision = plan_sequence_claims(default_settings(2), [1, 2], state, now)
    assert decision.due_slots == (1, 2)
    assert decision.held_slots == ()
    assert "live panel against Day 10" in decision.reasons[2]


def test_sequence_holds_only_a_confirmed_today_mismatch() -> None:
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    state = {"1": {"claimed_day": 9, "claimed_utc": now.isoformat()},
             "2": {"claimed_day": 5, "claimed_utc": now.isoformat()}}
    decision = plan_sequence_claims(default_settings(2), [1, 2], state, now)
    assert decision.due_slots == (1,)
    assert decision.held_slots == (2,)
    assert "slot 2 is on Day 5, slot 1 on Day 9" in decision.reasons[2]


def test_sequence_mixed_modes_and_missing_history_are_explicit() -> None:
    settings = replace(default_settings(4), sequence_modes={1: "always", 2: "hold", 3: "align", 4: "align"})
    decision = plan_sequence_claims(settings, [1, 2, 3, 4], {})
    assert decision.due_slots == (1, 3, 4)
    assert decision.held_slots == (2,)
    assert decision.reasons[1] == "Claim anyway"
    assert decision.reasons[2] == "Held by slot mode"
    assert "unknown" in decision.reasons[4]


def test_sequence_uses_integer_or_string_slot_keys_and_deduplicates_selection() -> None:
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    state = {1: {"claimed_day": 14, "claimed_utc": now.isoformat()},
             "2": {"claimed_day": 14, "claimed_utc": now.isoformat()}}
    decision = plan_sequence_claims(default_settings(2), [2, 2], state, now)
    assert decision.due_slots == (2,)
    assert decision.held_slots == ()


def test_sequence_rejects_invalid_manual_settings() -> None:
    settings = PlannerSettings(
        **{**default_settings(1).__dict__, "sequence_modes": {1: "surprise"}}
    )
    with pytest.raises(ValueError, match="unsupported"):
        plan_sequence_claims(settings, [1], {})


def test_save_settings_keeps_existing_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"known": "good"}', encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr("xynclaim.settings.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        save_settings(path, default_settings(2))
    assert path.read_text(encoding="utf-8") == '{"known": "good"}'
    assert not list(tmp_path.glob(".settings.json.*.tmp"))


def test_projection_refuses_unknown_skip_semantics() -> None:
    projected, note = projected_day14(12, "2026-07-27T14:00:00+00:00", (4, 5, 6), "unknown", date(2026, 7, 27))
    assert projected is None
    assert "unverified" in note


def test_projection_for_continuous_claims() -> None:
    projected, note = projected_day14(12, "2026-07-27T14:00:00+00:00", tuple(range(7)), "unknown", date(2026, 7, 27))
    assert projected == date(2026, 7, 29)
    assert "continues" in note


def test_reported_reset_tracks_eastern_daylight_time() -> None:
    summer = next_reported_reset(datetime(2026, 7, 27, 14, tzinfo=timezone.utc))
    winter = next_reported_reset(datetime(2026, 12, 27, 14, tzinfo=timezone.utc))
    assert summer.hour == 0
    assert winter.hour == 1


def test_history_aggregates_verified_lock_separately() -> None:
    records = [{
        "started_utc": "2026-07-27T14:00:00+00:00",
        "slots": [
            {"status": "claimed_unverified"},
            {"status": "claimed_global_locked"},
            {"status": "no_daily"},
            {"status": "failed"},
        ],
    }]
    summary = next(iter(history_days(records).values()))
    assert summary == {"claimed": 2, "locked": 1, "failed": 1, "no_daily": 1}


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/123/token",
        "https://example.com/api/webhooks/123/token",
        "https://discord.com/not-webhooks/123/token",
        "https://discord.com/api/webhooks/not-a-number/token",
    ],
)
def test_webhook_validation_rejects_ssrf_and_bad_paths(url: str) -> None:
    with pytest.raises(ValueError):
        validate_webhook_url(url)


def test_webhook_validation_accepts_discord_url() -> None:
    value = "https://discord.com/api/webhooks/123456/secret-token"
    assert validate_webhook_url(value) == value


def test_dpapi_round_trip_does_not_store_plaintext() -> None:
    secret = "https://discord.com/api/webhooks/123456/secret-token"
    encrypted = protect_secret(secret)
    assert secret not in encrypted
    assert unprotect_secret(encrypted) == secret


@dataclass
class _Slot:
    status: str
    server_route: str = "private"
    received_reward: str | None = None


@dataclass
class _Report:
    slots: list[_Slot]
    errors: list[str]
    total_seconds: float = 120.0
    server_profile: str = "Friend's server"


def test_webhook_payload_is_compact_and_contains_no_codes_or_receipts() -> None:
    payload = webhook_payload(
        _Report(
            [
                _Slot("claimed_unverified"),
                _Slot("claimed_global_locked", "public_fallback", "+10 Mastery Points"),
                _Slot("not_attempted"),
            ],
            [],
        )
    )
    encoded = json.dumps(payload)
    assert "secret-token" not in encoded
    assert "Friend's server" not in encoded
    assert "receipt" not in encoded.lower()
    fields = payload["embeds"][0]["fields"]
    counts = {field["name"]: field["value"] for field in fields}
    assert counts["Global locked"] == "1"
    assert counts["Public fallback"] == "1"
    assert counts["Not attempted"] == "1"
    assert "CLAIMED GLOBAL LOCKED · Day" not in counts["Slot outcomes"]
    assert "CLAIMED GLOBAL LOCKED · RECEIVED +10 Mastery Points · PUBLIC FALLBACK" in counts["Slot outcomes"]


def test_webhook_cuts_long_ocr_rewards() -> None:
    payload = webhook_payload(_Report([_Slot("claimed_global_locked", "private", "x" * 500)], []))
    outcomes = {field["name"]: field["value"] for field in payload["embeds"][0]["fields"]}["Slot outcomes"]
    assert "x" * 77 + "..." in outcomes
    assert "x" * 78 not in outcomes


def test_webhook_sender_refuses_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_handlers: list[object] = []

    class _Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith("https://discord.com/api/webhooks/")
            return _Response()

    def build_opener(*handlers):
        captured_handlers.extend(handlers)
        return _Opener()

    monkeypatch.setattr("xynclaim.webhook.urllib.request.build_opener", build_opener)
    from xynclaim.webhook import _NoWebhookRedirects, send_webhook

    send_webhook(
        "https://discord.com/api/webhooks/123456/secret-token",
        {"allowed_mentions": {"parse": []}},
    )
    assert any(isinstance(handler, _NoWebhookRedirects) for handler in captured_handlers)
