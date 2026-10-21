import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from xynclaim import engine as claimer
from test_core import bare_runner


@pytest.fixture
def fake_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(claimer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(claimer.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return clock


@pytest.fixture
def engine_files(tmp_path, monkeypatch):
    monkeypatch.setattr(claimer, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(claimer, "HISTORY_PATH", tmp_path / "history.jsonl")
    monkeypatch.setattr(claimer, "HISTORY_LOCK_PATH", tmp_path / ".history.lock")
    monkeypatch.setattr(claimer, "CLAIM_STATE_PATH", tmp_path / "claim_state.json")
    monkeypatch.setattr(claimer, "STATE_LOCK_PATH", tmp_path / ".claim_state.lock")
    monkeypatch.setattr(claimer, "LIVE_LOG_PATH", tmp_path / "live_run.log")
    return tmp_path


@pytest.fixture
def run_environment(monkeypatch, engine_files):
    geometry = claimer.RobloxGeometry(100, 0, 0, 1920, 1080)
    winapi = SimpleNamespace(user32=mock.Mock(), kernel32=mock.Mock())
    monkeypatch.setattr(claimer.ctypes, "windll", winapi)
    monkeypatch.setattr(claimer, "ensure_default_desktop", mock.Mock())
    monkeypatch.setattr(claimer, "keep_display_awake", mock.Mock())
    monkeypatch.setattr(claimer, "release_display_awake", mock.Mock())
    monkeypatch.setattr(claimer, "GRAPHICS_RESTORE_PATH", engine_files / "graphics_restore.json")
    monkeypatch.setattr(claimer, "ROBLOX_SETTINGS_PATH", engine_files / "GlobalBasicSettings_13.xml")
    monkeypatch.setattr(claimer, "wait_for_stable_geometry", mock.Mock(return_value=geometry))
    monkeypatch.setattr(claimer, "focus_roblox", mock.Mock(return_value=True))
    monkeypatch.setattr(claimer, "prepare_roblox_window", mock.Mock(return_value=(geometry, None)))
    monkeypatch.setattr(claimer, "ScreenCapture", mock.Mock(side_effect=lambda _geometry: mock.Mock()))
    monkeypatch.setattr(claimer, "save_report", mock.Mock())

    def make_runner(**attrs):
        runner = bare_runner(
            slots=[1, 2], geometry=geometry, server_profile="My private server",
            close_when_done=False, roblox_pid=42, roblox_handle=200, run_mutex=300,
            capture=None, _window_restore=None, _audio_restore=[], _original_priority=None,
            stop_event=threading.Event(), input=mock.Mock(),
        )
        runner.acquire_run_mutex = mock.Mock()
        runner.ensure_roblox_window = mock.Mock(return_value=True)
        runner.attach_roblox_process = mock.Mock()
        runner.finish_launch = mock.Mock()
        runner.process_running = mock.Mock(return_value=False)
        runner.process_slot = mock.Mock(side_effect=lambda slot: claimer.SlotResult(slot, "no_daily", None, 0.1))
        runner.return_to_main_menu = mock.Mock()
        runner.screenshot = mock.Mock(return_value="screen")
        runner.save_diagnostic = mock.Mock(return_value=engine_files / "diagnostic.png")
        for name, value in attrs.items():
            setattr(runner, name, value)
        return runner

    return make_runner


@pytest.fixture(autouse=True)
def isolated_live_log(tmp_path, monkeypatch):
    # The real live log is what a running app writes and watches; tests must never append to it.
    monkeypatch.setattr(claimer, "LIVE_LOG_PATH", tmp_path / "live_run.log")
    monkeypatch.setattr(claimer, "WINDOW_STATE_PATH", tmp_path / "window.json")
