from dataclasses import replace
from unittest import mock

import pytest

from xynclaim import engine
from xynclaim import app
from xynclaim.settings import default_settings, save_settings
from xynclaim.bridge import XynClaimAPI


@pytest.fixture
def scheduled_api(tmp_path):
    api = XynClaimAPI(tmp_path / "settings.json")
    save_settings(api.settings_path, replace(default_settings(), schedule_enabled=True))
    api._bind_window(mock.MagicMock())
    return api


def test_scheduled_wait_is_cancelled_when_window_closes(scheduled_api):
    api = scheduled_api

    def close_during_wait(_seconds):
        api.window_action("close")
        return True

    with mock.patch.object(engine.DBOGRunner, "roblox_running", side_effect=[True, False]), mock.patch.object(
        api._stop_event, "wait", side_effect=close_during_wait
    ), mock.patch.object(
        api, "start_run"
    ) as start, mock.patch.object(api, "_safe_log"):
        app.scheduled_start(api)
    start.assert_not_called()
    assert api._scheduled_waiting is False


def test_scheduled_wait_can_be_stopped(scheduled_api):
    api = scheduled_api

    def stop_during_wait(_seconds):
        assert api.get_state()["running"] is True
        assert api.get_state()["status"] == "waiting"
        assert api.stop_run()["stopping"] is True
        return True

    with mock.patch.object(engine.DBOGRunner, "roblox_running", side_effect=[True, False]), mock.patch.object(
        api._stop_event, "wait", side_effect=stop_during_wait
    ), mock.patch.object(
        api, "start_run"
    ) as start, mock.patch.object(api, "_safe_log"):
        app.scheduled_start(api)
    start.assert_not_called()
    assert api.get_state()["running"] is False


def test_scheduled_wait_timeout_is_bounded_and_closes(scheduled_api):
    api = scheduled_api
    with mock.patch.object(engine.DBOGRunner, "roblox_running", return_value=True), mock.patch.object(
        api._stop_event, "wait", return_value=False
    ) as wait, mock.patch.object(
        api, "start_run"
    ) as start, mock.patch.object(api, "_safe_log"):
        app.scheduled_start(api)
    assert wait.call_count == 24
    assert sum(call.args[0] for call in wait.call_args_list) == 7200
    start.assert_not_called()
    api._window.destroy.assert_called_once()


def test_scheduled_busy_skip_does_not_wait(scheduled_api):
    api = scheduled_api
    save_settings(api.settings_path, replace(default_settings(), schedule_enabled=True, schedule_roblox_busy="skip"))
    with mock.patch.object(engine.DBOGRunner, "roblox_running", return_value=True), mock.patch.object(
        api._stop_event, "wait"
    ) as wait, mock.patch.object(
        api, "start_run"
    ) as start, mock.patch.object(api, "_safe_log"):
        app.scheduled_start(api)
    wait.assert_not_called()
    start.assert_not_called()
    api._window.destroy.assert_called_once()


def test_scheduled_wait_reloads_preferences_before_starting(scheduled_api):
    api = scheduled_api

    def edit_during_wait(_seconds):
        save_settings(api.settings_path, replace(default_settings(), schedule_enabled=True,
                                                slot_count=2, slots=(2,), close_when_done=True))
        return False

    with mock.patch.object(engine.DBOGRunner, "roblox_running", side_effect=[True, False]), mock.patch.object(
        api._stop_event, "wait", side_effect=edit_during_wait
    ), mock.patch.object(
        api, "start_run", return_value={"started": True}
    ) as start, mock.patch.object(api, "_safe_log"):
        app.scheduled_start(api)
    assert start.call_args.args[0]["slots"] == [2]
    assert start.call_args.args[0]["close_when_done"] is True


def test_scheduled_start_reports_settings_corruption(scheduled_api):
    api = scheduled_api
    api.settings_path.write_text("{broken", encoding="utf-8")
    with mock.patch.object(api, "_safe_log") as log, mock.patch.object(api, "start_run") as start:
        app.scheduled_start(api)
    start.assert_not_called()
    assert "unreadable" in log.call_args.args[0]


def test_scheduled_start_refuses_closed_window(scheduled_api):
    api = scheduled_api
    api.window_action("close")
    with mock.patch.object(engine.DBOGRunner, "roblox_running", return_value=False), mock.patch.object(
        api, "start_run"
    ) as start, mock.patch.object(api, "_safe_log"):
        app.scheduled_start(api)
    start.assert_not_called()


def test_scheduled_start_does_not_claim_when_automatic_runs_are_off(scheduled_api):
    api = scheduled_api
    save_settings(api.settings_path, default_settings())
    with mock.patch.object(engine.DBOGRunner, "roblox_running", return_value=False), mock.patch.object(
        api, "start_run"
    ) as start, mock.patch.object(api, "_safe_log"):
        app.scheduled_start(api)
    start.assert_not_called()


def test_schedule_disabled_during_wait_cancels_the_run(scheduled_api):
    api = scheduled_api

    def disable_during_wait(_seconds):
        save_settings(api.settings_path, default_settings())
        return False

    with mock.patch.object(engine.DBOGRunner, "roblox_running", side_effect=[True, False]), mock.patch.object(
        api._stop_event, "wait", side_effect=disable_during_wait
    ), mock.patch.object(api, "start_run") as start, mock.patch.object(api, "_safe_log"):
        app.scheduled_start(api)
    start.assert_not_called()
