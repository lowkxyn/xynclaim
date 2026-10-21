import sys
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from xynclaim import engine as claimer


REAL_GEOMETRY_WAIT = claimer.wait_for_stable_geometry


def test_log_sink_failure_cannot_skip_cleanup(run_environment):
    runner = run_environment(_original_priority=0x20, _audio_restore=[("volume", False)])
    runner.server_code = "secret-code"
    runner._log = mock.Mock(side_effect=OSError("cannot log secret-code"))
    runner.log = claimer.DBOGRunner.log.__get__(runner)
    runner.close_then_restore_audio = mock.Mock()

    report = runner.run()

    runner.close_then_restore_audio.assert_called_once()
    runner.capture.close.assert_called_once()
    claimer.release_display_awake.assert_called_once()
    assert runner.roblox_handle is None and runner.run_mutex is None
    claimer.save_report.assert_called_once()
    assert any("Log output failed" in error for error in report.errors)
    assert all("secret-code" not in error for error in report.errors)


def test_audio_com_is_owned_on_each_run_thread_through_restoration(run_environment, monkeypatch):
    local = threading.local()
    imported = [False]
    events = []
    reports = []

    def initialize():
        local.initialized = True
        events.append("initialize")

    def uninitialize():
        assert local.initialized
        local.initialized = False
        events.append("uninitialize")

    def mute(_pid):
        # Model comtypes' first-import side effect, followed by a cached import.
        if not imported[0]:
            imported[0] = True
            local.initialized = True
        if not getattr(local, "initialized", False):
            raise OSError("CO_E_NOTINITIALIZED")
        events.append("mute")
        return [(mock.Mock(), False)]

    def restore(_states):
        assert local.initialized
        events.append("restore")

    monkeypatch.setitem(sys.modules, "comtypes", SimpleNamespace(CoInitialize=initialize, CoUninitialize=uninitialize))
    monkeypatch.setattr(claimer, "mute_process_audio", mute)
    monkeypatch.setattr(claimer, "restore_process_audio", restore)
    for _ in range(2):
        runner = run_environment(mute_roblox=True, slots=[1])
        worker = threading.Thread(target=lambda: reports.append(runner.run()))
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert len(reports) == 2 and all(not report.errors for report in reports)
    assert events == ["initialize", "mute", "restore", "uninitialize"] * 2


def test_visible_focus_denial_never_enters_ghost_recovery(run_environment, monkeypatch, fake_clock):
    runner = run_environment()
    runner.ensure_roblox_window = claimer.DBOGRunner.ensure_roblox_window.__get__(runner)
    monkeypatch.setattr(claimer, "find_roblox_window", mock.Mock(return_value=runner.geometry))
    monkeypatch.setattr(claimer, "focus_roblox", mock.Mock(return_value=False))
    runner.roblox_running = mock.Mock(return_value=True)
    kill = mock.Mock()
    launch = mock.Mock()
    monkeypatch.setattr(claimer.subprocess, "run", kill)
    monkeypatch.setattr(claimer, "launch_roblox_on_default_desktop", launch)

    with pytest.raises((claimer.FocusLost, RuntimeError)):
        runner.ensure_roblox_window()

    kill.assert_not_called()
    launch.assert_not_called()


def test_stopped_attachment_sends_no_recovery_actions(run_environment, monkeypatch, fake_clock):
    runner = run_environment()
    runner.ensure_roblox_window = claimer.DBOGRunner.ensure_roblox_window.__get__(runner)
    runner.stop_event.set()
    runner.roblox_running = mock.Mock(return_value=True)
    monkeypatch.setattr(claimer, "find_roblox_window", mock.Mock(return_value=runner.geometry))
    monkeypatch.setattr(claimer, "focus_roblox", mock.Mock(return_value=False))
    kill = mock.Mock()
    launch = mock.Mock()
    monkeypatch.setattr(claimer.subprocess, "run", kill)
    monkeypatch.setattr(claimer, "launch_roblox_on_default_desktop", launch)

    with pytest.raises(claimer.RunStopped):
        runner.ensure_roblox_window()

    kill.assert_not_called()
    launch.assert_not_called()


def test_stopped_geometry_poll_exits_before_window_changes(run_environment, monkeypatch, fake_clock):
    runner = run_environment()
    runner.stop_event.set()
    geometry_reader = mock.Mock(return_value=runner.geometry)
    monkeypatch.setattr(claimer, "_client_geometry", geometry_reader)

    with pytest.raises(claimer.RunStopped):
        REAL_GEOMETRY_WAIT(runner.geometry.hwnd, cancel_check=runner.check_stopped)

    geometry_reader.assert_not_called()


def test_cli_corrupt_settings_fail_before_runner_construction(engine_files, monkeypatch, capsys):
    monkeypatch.setattr(claimer, "migrate_legacy_data", mock.Mock())
    monkeypatch.setattr("xynclaim.settings.load_settings", mock.Mock(side_effect=ValueError("corrupt settings")))
    runner = mock.Mock()
    runner.return_value.run.return_value = claimer.RunReport("test", "start", "end", 0, "owner", "test", 2, [], [])
    monkeypatch.setattr(claimer, "DBOGRunner", runner)
    monkeypatch.setattr(sys, "argv", ["claimer", "--run", "--align", "--slots", "1,2"])

    with pytest.raises(SystemExit) as error:
        claimer.main()

    assert error.value.code == 2
    assert "corrupt settings" in capsys.readouterr().err
    runner.assert_not_called()


def test_ctrl_c_during_close_still_restores_audio_and_finishes_cleanup(run_environment, monkeypatch):
    states = [(mock.Mock(), False)]
    runner = run_environment(close_when_done=True, _audio_restore=states)
    runner.close_roblox = mock.Mock(side_effect=KeyboardInterrupt())
    restore = mock.Mock()
    monkeypatch.setattr(claimer, "restore_process_audio", restore)

    try:
        report = runner.run()
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C during close skipped audio and the remaining cleanup")

    restore.assert_called_once_with(states)
    claimer.release_display_awake.assert_called_once()
    assert runner.roblox_handle is None and runner.run_mutex is None
    assert any("KeyboardInterrupt" in error for error in report.errors)
    claimer.save_report.assert_called_once()


def test_interrupted_audio_restore_still_attempts_the_other_sessions():
    first, second = mock.Mock(), mock.Mock()
    first.SetMute.side_effect = KeyboardInterrupt()

    try:
        with pytest.raises(RuntimeError, match="KeyboardInterrupt"):
            claimer.restore_process_audio([(first, False), (second, True)])
    except KeyboardInterrupt:
        pytest.fail("The interrupted restore abandoned later audio sessions")

    second.SetMute.assert_called_once_with(1, None)


def test_interrupted_mute_rolls_back_all_touched_sessions(monkeypatch):
    first, second = mock.Mock(), mock.Mock()
    first.GetMute.return_value = False
    second.GetMute.return_value = True
    second.SetMute.side_effect = [KeyboardInterrupt(), None]
    sessions = [SimpleNamespace(ProcessId=42, SimpleAudioVolume=volume) for volume in (first, second)]
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", SimpleNamespace(
        AudioUtilities=SimpleNamespace(GetAllSessions=lambda: sessions),
    ))

    with pytest.raises(KeyboardInterrupt):
        claimer.mute_process_audio(42)

    assert first.SetMute.call_args_list == [mock.call(1, None), mock.call(0, None)]
    assert second.SetMute.call_args_list == [mock.call(1, None), mock.call(1, None)]
