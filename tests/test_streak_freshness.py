import tempfile
from datetime import timedelta
from pathlib import Path

from xynclaim import engine
from xynclaim import bridge
from xynclaim.settings import next_reported_reset


def streak_for(monkeypatch, claimed_utc):
    state = {"1": {"claimed_day": 6, "next_day": 7, "claimed_utc": claimed_utc}}
    monkeypatch.setattr(engine, "load_claim_state", lambda: state)
    api = bridge.XynClaimAPI(Path(tempfile.mkdtemp()) / "settings.json")
    monkeypatch.setattr(api, "_all_history", lambda: [])
    return api.get_streak()["slots"][0]


def test_claim_in_previous_cycle_keeps_next_day(monkeypatch):
    yesterday = next_reported_reset() - timedelta(hours=30)
    slot = streak_for(monkeypatch, yesterday.isoformat())
    assert slot["next_day"] == 7 and slot["stale"] is False


def test_month_old_claim_hides_next_day(monkeypatch):
    old = next_reported_reset() - timedelta(days=30)
    slot = streak_for(monkeypatch, old.isoformat())
    assert slot["next_day"] is None and slot["stale"] is True
    assert slot["streak_day"] == 6  # still shown as the last observed day


def test_missing_or_naive_timestamp_counts_as_stale(monkeypatch):
    assert streak_for(monkeypatch, None)["stale"] is True
    assert streak_for(monkeypatch, "2026-09-29T12:00:00")["stale"] is True
