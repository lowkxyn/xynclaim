import tempfile
import threading
import unittest
import inspect
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xynclaim import engine
from xynclaim import bridge
from xynclaim.settings import default_settings, load_settings, save_settings


class WebviewAPITests(unittest.TestCase):
    def make_api(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return bridge.XynClaimAPI(Path(directory.name) / "settings.json")

    def test_state_never_exposes_secrets(self):
        api = self.make_api()
        settings = replace(default_settings(), webhook_enabled=True, webhook_blob="DPAPI-BLOB")
        with mock.patch.object(bridge, "load_settings", return_value=settings), mock.patch.object(
            bridge, "read_history", return_value=[{"access_mode": "shared", "server_profile": "TOPSECRET", "slots": []}]
        ):
            state = api.get_state()
        rendered = repr(state)
        self.assertNotIn("DPAPI-BLOB", rendered)
        self.assertNotIn("TOPSECRET", rendered)
        self.assertEqual(state["history"][0]["access"], "Shared private code")

    def test_start_run_rejects_invalid_shared_code_before_runner(self):
        api = self.make_api()
        with mock.patch.object(bridge.engine, "DBOGRunner") as runner:
            with self.assertRaises(ValueError):
                api.start_run({"access_mode": "shared", "server_code": "", "slot_count": 1, "slots": [1], "close_when_done": False, "public_fallback": False})
        runner.assert_not_called()

    def test_start_run_applies_sequence_modes_and_clears_shared_secret_after_finish(self):
        api = self.make_api()
        report = engine.RunReport("test", "start", "finish", 1.0, "shared", "TOPSECRET", 2, [], [])
        runner = mock.MagicMock()
        runner.run.return_value = report
        modes = dict(default_settings().sequence_modes)
        modes[1] = "hold"
        scheduled = replace(default_settings(), sequence_modes=modes)
        with mock.patch.object(bridge.engine, "load_claim_state", return_value={}), mock.patch.object(bridge, "load_settings", return_value=scheduled), mock.patch.object(
            bridge.engine, "DBOGRunner", return_value=runner
        ) as runner_type:
            started = api.start_run({"access_mode": "shared", "server_code": "TOPSECRET", "slot_count": 2, "slots": [1, 2], "close_when_done": False, "public_fallback": False})
            api._worker.join(2)
        self.assertEqual(started["slots"], [2])
        self.assertEqual(runner_type.call_args.args[0], [2])
        self.assertEqual(runner_type.call_args.kwargs["slot_mismatch_policies"], {2: "align"})
        self.assertEqual(api.get_state()["report"]["server_profile"], "Shared private code")
        self.assertNotIn("TOPSECRET", repr(api.get_state()))
        self.assertFalse(api._worker.daemon)

    def test_stop_run_signals_active_worker(self):
        api = self.make_api()
        api._worker = mock.MagicMock()
        api._worker.is_alive.return_value = True
        result = api.stop_run()
        self.assertTrue(result["stopping"])
        self.assertTrue(api._stop_event.is_set())

    def test_start_run_refuses_a_second_concurrent_run(self):
        api = self.make_api()
        api._worker = mock.MagicMock()
        api._worker.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, "already active"):
            api.start_run({"access_mode": "owner", "slot_count": 1, "slots": [1], "close_when_done": False, "public_fallback": False})

    def test_concurrent_starts_recheck_after_validation(self):
        api = self.make_api()
        validating = threading.Barrier(2)
        release_run = threading.Event()
        runner_started = threading.Event()
        report = engine.RunReport("test", "start", "finish", 1.0, "owner", "private", 1, [], [])
        runner = mock.MagicMock()
        runner.run.side_effect = lambda: release_run.wait(2) and report
        errors = []
        workers = []

        def settings():
            if threading.current_thread().name != "XynClaimRunner":
                validating.wait(2)
            return default_settings()

        def start():
            try:
                api.start_run({"slot_count": 1, "slots": [1]})
                workers.append(api._worker)
            except RuntimeError as error:
                errors.append(str(error))

        def construct(*_args, **_kwargs):
            runner_started.set()
            return runner

        with mock.patch.object(api, "_settings", side_effect=settings), mock.patch.object(
            engine, "load_claim_state", return_value={}
        ), mock.patch.object(engine, "DBOGRunner", side_effect=construct) as constructor:
            starters = [threading.Thread(target=start) for _ in range(2)]
            try:
                for starter in starters:
                    starter.start()
                for starter in starters:
                    starter.join(2)
                self.assertTrue(runner_started.wait(2))
                self.assertEqual(constructor.call_count, 1)
                self.assertEqual(len(errors), 1)
                self.assertIn("already active", errors[0])
            finally:
                release_run.set()
                for worker in set(workers + [api._worker]):
                    if worker is not None:
                        worker.join(2)

    def test_stopping_status_survives_late_runner_logs(self):
        api = self.make_api()
        api._worker = mock.MagicMock()
        api._worker.is_alive.return_value = True
        with mock.patch.object(api, "_safe_log", wraps=api._safe_log), mock.patch.object(
            engine, "exclusive_file_lock"
        ), mock.patch.object(Path, "open", mock.mock_open()):
            api.stop_run()
            api._safe_log("Slot 1: launching Roblox")
        self.assertEqual(api._status, "stopping")

    def test_elapsed_freezes_when_run_finishes(self):
        api = self.make_api()
        report = engine.RunReport("test", "start", "finish", 65.0, "owner", "private", 1, [], [])
        runner = mock.MagicMock()
        runner.run.return_value = report
        with mock.patch.object(engine, "load_claim_state", return_value={}), mock.patch.object(
            engine, "DBOGRunner", return_value=runner
        ):
            api.start_run({"slot_count": 1, "slots": [1]})
            api._worker.join(2)
        state = api.get_state()
        self.assertEqual(state["elapsed_seconds"], 65.0)
        self.assertEqual(state["elapsed"], "01:05")

    def test_failed_run_keeps_elapsed_and_finished_live_results(self):
        api = self.make_api()
        runner = mock.MagicMock()
        runner.results = [engine.SlotResult(1, "claimed_verified", 3, 10, received_reward="1x Holy Water")]

        def run():
            api._started_at = time.monotonic() - 65
            raise RuntimeError("failed after one slot")

        runner.run.side_effect = run
        with mock.patch.object(engine, "load_claim_state", return_value={}), mock.patch.object(
            engine, "DBOGRunner", return_value=runner
        ), mock.patch.object(api, "_safe_log"):
            api.start_run({"slot_count": 1, "slots": [1]})
            api._worker.join(2)
        state = api.get_state()
        self.assertEqual(state["elapsed"], "01:05")
        self.assertEqual(state["live_results"][0]["slot"], 1)

    def test_start_refuses_close_that_arrives_during_validation(self):
        api = self.make_api()
        api._bind_window(mock.MagicMock())

        def settings():
            api.window_action("close")
            return default_settings()

        with mock.patch.object(api, "_settings", side_effect=settings), mock.patch.object(
            engine, "load_claim_state", return_value={}
        ), mock.patch.object(engine, "DBOGRunner") as runner:
            with self.assertRaisesRegex(RuntimeError, "closing"):
                api.start_run({"slot_count": 1, "slots": [1]})
        runner.assert_not_called()

    def test_failed_status_survives_error_text_containing_launching(self):
        api = self.make_api()
        with mock.patch.object(engine, "load_claim_state", return_value={}), mock.patch.object(
            engine, "DBOGRunner", side_effect=RuntimeError("error launching Roblox")
        ), mock.patch.object(engine, "exclusive_file_lock"), mock.patch.object(Path, "open", mock.mock_open()):
            api.start_run({"slot_count": 1, "slots": [1]})
            api._worker.join(2)
        self.assertEqual(api._status, "failed")

    def test_compact_expand_fit_the_saved_monitor(self):
        api = self.make_api()
        screen = SimpleNamespace(x=1280, y=0, width=1024, height=768)
        window = mock.MagicMock(x=2100, y=650, width=1280, height=860)
        api._bind_window(window)
        api._screens = [screen]
        api.window_action("compact")
        api.window_action("expand")
        width, height = window.resize.call_args.args
        x, y = window.move.call_args.args
        self.assertLessEqual(width, screen.width)
        self.assertLessEqual(height, screen.height)
        self.assertGreaterEqual(x, screen.x)
        self.assertGreaterEqual(y, screen.y)
        self.assertLessEqual(x + width, screen.x + screen.width)
        self.assertLessEqual(y + height, screen.y + screen.height)

    def test_compact_from_maximized_restores_then_remaximizes(self):
        api = self.make_api()
        window = mock.MagicMock(x=0, y=0, width=1280, height=860)
        api._bind_window(window)
        api.window_action("maximize")
        api.window_action("compact")
        window.restore.assert_called_once()
        api.window_action("expand")
        self.assertEqual(window.maximize.call_count, 2)

    def test_launch_clamps_geometry_on_the_saved_small_monitor(self):
        api = self.make_api()
        screen = SimpleNamespace(x=1280, y=0, width=1024, height=768)
        webview = mock.MagicMock()
        webview.screens = [SimpleNamespace(x=0, y=0, width=1920, height=1080), screen]
        with mock.patch.dict("sys.modules", {"webview": webview}), mock.patch.object(
            bridge, "XynClaimAPI", return_value=api
        ), mock.patch.object(bridge, "_saved_window_position", return_value=(2200, 700)), mock.patch.object(
            bridge, "HotkeyListener"
        ):
            bridge.launch_webview()
        geometry = webview.create_window.call_args.kwargs
        self.assertLessEqual(geometry["width"], screen.width)
        self.assertLessEqual(geometry["height"], screen.height)
        self.assertGreaterEqual(geometry["x"], screen.x)
        self.assertGreaterEqual(geometry["y"], screen.y)
        self.assertLessEqual(geometry["x"] + geometry["width"], screen.x + screen.width)
        self.assertLessEqual(geometry["y"] + geometry["height"], screen.y + screen.height)

    def test_new_run_clears_previous_report_elapsed_and_live_results(self):
        api = self.make_api()
        result = engine.SlotResult(1, "claimed_verified", 3, 10, received_reward="10 Zeni")
        report = engine.RunReport("test", "start", "finish", 65.0, "owner", "private", 2, [result], [])
        first = mock.MagicMock(results=[result])
        first.run.return_value = report
        constructing = threading.Event()
        release = threading.Event()
        second = mock.MagicMock(results=[])
        second.run.return_value = replace(report, slots=[], total_seconds=1.0)

        def construct_second(*_args, **_kwargs):
            constructing.set()
            release.wait(2)
            return second

        with mock.patch.object(engine, "load_claim_state", return_value={}), mock.patch.object(
            engine, "DBOGRunner", return_value=first
        ) as constructor:
            api.start_run({"slot_count": 2, "slots": [1]})
            api._worker.join(2)
            constructor.side_effect = construct_second
            api.start_run({"slot_count": 2, "slots": [2]})
            try:
                self.assertTrue(constructing.wait(2))
                state = api.get_state()
                self.assertTrue(state["running"])
                self.assertIsNone(state["report"])
                self.assertEqual(state["live_results"], [])
                self.assertEqual(state["active_slots"], [2])
                self.assertEqual(state["elapsed"], "00:00")
            finally:
                release.set()
                api._worker.join(2)

    def test_thread_start_failure_does_not_leave_a_running_timer(self):
        api = self.make_api()
        with mock.patch.object(engine, "load_claim_state", return_value={}), mock.patch.object(
            threading.Thread, "start", side_effect=RuntimeError("can't start new thread")
        ):
            with self.assertRaisesRegex(RuntimeError, "can't start"):
                api.start_run({"slot_count": 1, "slots": [1]})
        state = api.get_state()
        self.assertFalse(state["running"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["elapsed_seconds"], 0.0)
        self.assertIn("can't start", state["error"])

    def test_save_plan_validates_sequence_and_runtime_preferences(self):
        api = self.make_api()
        settings = default_settings()
        with mock.patch.object(bridge, "load_settings", return_value=settings), mock.patch.object(
            bridge, "save_settings"
        ) as save:
            api.save_plan({
                "sequence_modes": {"1": "align", "2": "always"},
                "sequence_align_targets": {"1": 2},
                "run_monitor": "secondary",
                "mute_roblox": True,
                "low_graphics": True,
                "low_cpu_priority": True,
            })
        saved = save.call_args.args[1]
        self.assertEqual(saved.sequence_modes[1], "align")
        self.assertEqual(saved.sequence_align_targets[1], 2)
        self.assertEqual(saved.run_monitor, "secondary")
        self.assertTrue(saved.mute_roblox)
        with self.assertRaises(ValueError):
            api.save_plan({"sequence_align_targets": {"1": 15}})

    def test_notifications_preserve_blank_url_and_require_secret_to_enable(self):
        api = self.make_api()
        existing = replace(default_settings(), webhook_blob="existing")
        with mock.patch.object(bridge, "load_settings", return_value=existing), mock.patch.object(
            bridge, "save_settings"
        ) as save:
            api.save_notifications({"webhook_url": "", "enabled": False})
        self.assertEqual(save.call_args.args[1].webhook_blob, "existing")
        with mock.patch.object(bridge, "load_settings", return_value=default_settings()):
            with self.assertRaises(ValueError):
                api.save_notifications({"webhook_url": "", "enabled": True})

    def test_concurrent_plan_and_notification_saves_preserve_both_changes(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_path = Path(directory.name) / "settings.json"
        save_settings(
            settings_path,
            replace(default_settings(), webhook_enabled=True, webhook_blob="existing-dpapi-blob"),
        )
        notifications_api = bridge.XynClaimAPI(settings_path)
        plan_api = bridge.XynClaimAPI(settings_path)
        transaction_lock = threading.Lock()
        first_save_started = threading.Event()
        second_lock_attempted = threading.Event()
        release_first_save = threading.Event()
        start_writers = threading.Barrier(3)
        save_count = 0
        save_count_lock = threading.Lock()
        errors: list[Exception] = []
        original_save = bridge.save_settings

        @contextmanager
        def exclusive_transaction_lock(_path):
            if transaction_lock.locked():
                second_lock_attempted.set()
            with transaction_lock:
                yield

        def pause_first_save(path, settings):
            nonlocal save_count
            with save_count_lock:
                save_count += 1
                is_first_save = save_count == 1
            if is_first_save:
                first_save_started.set()
                self.assertTrue(release_first_save.wait(2))
            original_save(path, settings)

        def disable_notifications():
            try:
                start_writers.wait()
                notifications_api.save_notifications({"webhook_url": "", "enabled": False})
            except Exception as error:
                errors.append(error)

        def save_plan():
            try:
                start_writers.wait()
                plan_api.save_plan({"sequence_modes": {"1": "align", "2": "always"}, "sequence_align_targets": {"1": 2}})
            except Exception as error:
                errors.append(error)

        with mock.patch.object(bridge.engine, "exclusive_file_lock", exclusive_transaction_lock), mock.patch.object(
            bridge, "save_settings", side_effect=pause_first_save
        ):
            notification_writer = threading.Thread(target=disable_notifications)
            plan_writer = threading.Thread(target=save_plan)
            notification_writer.start()
            plan_writer.start()
            start_writers.wait()
            self.assertTrue(first_save_started.wait(2))
            self.assertTrue(second_lock_attempted.wait(2))
            self.assertEqual(save_count, 1)
            release_first_save.set()
            notification_writer.join(2)
            plan_writer.join(2)

        self.assertFalse(notification_writer.is_alive())
        self.assertFalse(plan_writer.is_alive())
        self.assertEqual(errors, [])
        settings = load_settings(settings_path)
        self.assertFalse(settings.webhook_enabled)
        self.assertEqual(settings.webhook_blob, "existing-dpapi-blob")
        self.assertEqual(settings.sequence_modes[1], "align")
        self.assertEqual(settings.sequence_align_targets[1], 2)

    def test_notification_test_uses_fixed_safe_payload_asynchronously(self):
        api = self.make_api()
        settings = replace(default_settings(), webhook_blob="protected")
        sent = threading.Event()
        with mock.patch.object(bridge, "load_settings", return_value=settings), mock.patch.object(
            bridge, "unprotect_secret", return_value="https://discord.com/api/webhooks/1/token"
        ), mock.patch.object(bridge, "send_webhook", side_effect=lambda *_args: sent.set()) as send:
            api.test_notification()
            self.assertTrue(sent.wait(2))
        payload = send.call_args.args[1]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("TOPSECRET", repr(payload))

    def test_window_actions_use_injected_window(self):
        api = self.make_api()
        window = mock.MagicMock()
        api._bind_window(window)
        api.window_action("maximize")
        window.maximize.assert_called_once_with()
        api.window_action("close")
        window.destroy.assert_called_once_with()

    def test_close_stops_first_and_second_request_can_force_close(self):
        api = self.make_api()
        window = mock.MagicMock()
        api._bind_window(window)
        api._worker = mock.MagicMock()
        api._worker.is_alive.return_value = True

        self.assertEqual(api.window_action("close")["action"], "stopping-before-close")
        window.destroy.assert_not_called()
        self.assertTrue(api._stop_event.is_set())
        self.assertEqual(api.window_action("close")["action"], "close")
        window.destroy.assert_called_once_with()

    def test_corrupt_settings_are_blocked_until_explicit_preserving_reset(self):
        api = self.make_api()
        api.settings_path.write_text("{broken", encoding="utf-8")

        state = api.get_state()
        self.assertIn("unreadable", state["settings_error"])
        result = api.reset_settings()

        self.assertTrue(result["reset"])
        self.assertTrue((api.settings_path.parent / result["backup"]).is_file())
        self.assertEqual(api.get_state()["settings_error"], "")

    def test_launch_exposes_only_explicit_bridge_functions(self):
        source = inspect.getsource(bridge.launch_webview)
        self.assertNotIn("js_api=api", source)
        self.assertIn("window.expose(", source)
        self.assertNotIn("api._mutate_settings,", source)
        self.assertNotIn("api._settings,", source)


if __name__ == "__main__":
    unittest.main()
