import argparse
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from xynclaim import engine as claimer


def bare_runner(**attrs):
    """A DBOGRunner with __init__ skipped, so tests can drive individual
    methods without VisualDetector needing real reference screenshots or
    any of run()'s Windows/game side effects."""
    runner = object.__new__(claimer.DBOGRunner)
    runner.log = attrs.pop("log", lambda message: None)
    runner.results = attrs.pop("results", [])
    runner.slots = attrs.pop("slots", [])
    runner.access_mode = attrs.pop("access_mode", "owner")
    runner.server_code = attrs.pop("server_code", "")
    runner.configured_slot_count = attrs.pop("configured_slot_count", 12)
    runner.allow_public_fallback = attrs.pop("allow_public_fallback", False)
    runner._shared_code_may_be_visible = attrs.pop(
        "_shared_code_may_be_visible", False
    )
    runner.errors = attrs.pop("errors", [])
    runner.stop_event = attrs.pop("stop_event", threading.Event())
    runner.detector = attrs.pop("detector", mock.MagicMock())
    runner.detector.matches.return_value = (False, 0.0)
    runner.mark_pending_claim = attrs.pop("mark_pending_claim", mock.MagicMock())
    runner.min_claim_day = attrs.pop("min_claim_day", 1)
    runner.inspect_only = attrs.pop("inspect_only", False)
    for key, value in attrs.items():
        setattr(runner, key, value)
    return runner


class ValidationTests(unittest.TestCase):
    def test_shared_code_accepts_visible_ascii(self):
        self.assertEqual(claimer.normalize_server_code("  AbC-123_!  "), "AbC-123_!")

    def test_shared_code_rejects_empty_whitespace_and_non_ascii(self):
        for value in ("", "abc def", "abc\ndef", "cafÃ©"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                claimer.normalize_server_code(value)

    def test_shared_code_rejects_over_64_characters(self):
        with self.assertRaises(ValueError):
            claimer.normalize_server_code("x" * 65)

    def test_profile_cannot_contain_secret_or_control_characters(self):
        for value in ("Friend ABC123", "line\nbreak"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                claimer.normalize_profile_name(value, "shared", "ABC123")

    def test_slot_parser_handles_ranges_and_rejects_invalid_values(self):
        self.assertEqual(claimer.parse_slots("1,3-5,3"), [1, 3, 4, 5])
        for value in ("", "0", "13", "5-3"):
            with self.subTest(value=value), self.assertRaises(Exception):
                claimer.parse_slots(value)

    def test_slot_parser_rejects_out_of_range_via_argument_type_error(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            claimer.parse_slots("13")
        with self.assertRaises(argparse.ArgumentTypeError):
            claimer.parse_slots("0")

    def test_slot_parser_boundary_values_are_accepted(self):
        self.assertEqual(claimer.parse_slots("1-12"), list(range(1, 13)))
        self.assertEqual(claimer.parse_slots("1"), [1])
        self.assertEqual(claimer.parse_slots("12"), [12])

    def test_slot_parser_rejects_reversed_range_next_to_valid_slots(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            claimer.parse_slots("5-3,2")

    def test_slot_parser_negative_number_raises_argument_type_error(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            claimer.parse_slots("-1")

    def test_shared_code_boundary_length_accepted(self):
        self.assertEqual(claimer.normalize_server_code("x" * 64), "x" * 64)

    def test_profile_name_defaults_by_access_mode(self):
        self.assertEqual(
            claimer.normalize_profile_name("   ", "shared", "ABC123"),
            "Shared private server",
        )
        self.assertEqual(claimer.normalize_profile_name("  ", "owner"), "My private server")

    def test_profile_name_boundary_length_accepted(self):
        name = "x" * 40
        self.assertEqual(claimer.normalize_profile_name(name, "owner"), name)

    def test_profile_name_over_40_characters_rejected(self):
        with self.assertRaises(ValueError):
            claimer.normalize_profile_name("x" * 41, "owner")

    def test_profile_name_rejects_unpaired_unicode_surrogates(self):
        with self.assertRaisesRegex(ValueError, "valid one-line text"):
            claimer.normalize_profile_name("bad\ud800name", "owner")

    def test_profile_name_rejects_unicode_line_separators(self):
        for separator in ("\u0085", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)), self.assertRaisesRegex(
                ValueError, "valid one-line text"
            ):
                claimer.normalize_profile_name(f"line{separator}break", "owner")

    def test_redact_secret_replaces_only_the_secret(self):
        self.assertEqual(
            claimer.redact_secret("join failed for code ABC123", "ABC123"),
            "join failed for code [REDACTED]",
        )

    def test_redact_secret_is_noop_with_empty_secret(self):
        self.assertEqual(claimer.redact_secret("no secret here", ""), "no secret here")

    def test_daily_rewards_cover_all_14_days(self):
        self.assertEqual(sorted(claimer.DAILY_REWARDS), list(range(1, 15)))

    def test_replace_text_uses_layout_independent_unicode_input(self):
        focus_guard = mock.Mock()
        controls = claimer.DirectInput.__new__(claimer.DirectInput)
        controls._send_unicode_character = mock.Mock()
        with mock.patch.object(
            claimer.pydirectinput, "keyDown", return_value=True
        ) as key_down, mock.patch.object(
            claimer.pydirectinput, "keyUp", return_value=True
        ) as key_up, mock.patch.object(
            claimer.pydirectinput, "press", return_value=True
        ) as press, mock.patch.object(claimer.pydirectinput, "write") as write:
            controls.replace_text("Az_1", focus_guard)

        self.assertEqual(key_down.call_args_list, [mock.call("ctrl")])
        self.assertEqual(key_up.call_args_list, [mock.call("ctrl")])
        self.assertEqual(press.call_args_list, [mock.call("a")])
        self.assertEqual(
            controls._send_unicode_character.call_args_list,
            [mock.call("A"), mock.call("z"), mock.call("_"), mock.call("1")],
        )
        write.assert_not_called()

    def test_unicode_input_sends_down_and_up_utf16_packets(self):
        controls = claimer.DirectInput.__new__(claimer.DirectInput)
        captured = []
        controls.user32 = mock.Mock()

        def send_input(count, packet, _size):
            captured.extend(
                (packet[index].ki.wScan, packet[index].ki.dwFlags)
                for index in range(count)
            )
            return count

        controls.user32.SendInput.side_effect = send_input
        controls._send_unicode_character("A")

        self.assertEqual(
            captured,
            [
                (ord("A"), claimer.DirectInput.KEYEVENTF_UNICODE),
                (
                    ord("A"),
                    claimer.DirectInput.KEYEVENTF_UNICODE
                    | claimer.DirectInput.KEYEVENTF_KEYUP,
                ),
            ],
        )


class PersistenceTests(unittest.TestCase):
    def test_claim_state_write_is_atomic_and_marks_reward_unverified(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "claim_state.json"
            lock_path = root / ".claim_state.lock"
            with mock.patch.object(claimer, "CLAIM_STATE_PATH", state_path), mock.patch.object(
                claimer, "STATE_LOCK_PATH", lock_path
            ):
                claimer.update_claim_state(2, 14)
                state = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertEqual(state["2"]["claimed_day"], 14)
            self.assertEqual(state["2"]["verification"], "unverified")
            self.assertFalse(list(root.glob("*.tmp")))

    def test_corrupt_claim_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "claim_state.json"
            state_path.write_text("{broken", encoding="utf-8")
            with mock.patch.object(claimer, "CLAIM_STATE_PATH", state_path), mock.patch.object(
                claimer, "STATE_LOCK_PATH", root / ".claim_state.lock"
            ):
                with self.assertRaisesRegex(RuntimeError, "preserved"):
                    claimer.load_claim_state()

            self.assertFalse(state_path.exists())
            self.assertEqual(len(list(root.glob("claim_state.corrupt_*.json"))), 1)

    def test_report_history_is_append_only_and_has_no_code_field(self):
        report = claimer.RunReport(
            app_version="test",
            started_utc="start",
            finished_utc="finish",
            total_seconds=1.0,
            access_mode="shared",
            server_profile="Friend server",
            configured_slot_count=1,
            slots=[
                claimer.SlotResult(
                    slot=1,
                    status="claimed_unverified",
                    day=1,
                    seconds=1.0,
                    reward="expected",
                    verification="unverified",
                )
            ],
            errors=[],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with mock.patch.object(claimer, "REPORT_DIR", root), mock.patch.object(
                claimer, "HISTORY_PATH", root / "history.jsonl"
            ), mock.patch.object(claimer, "HISTORY_LOCK_PATH", root / ".history.lock"):
                claimer.save_report(report)
                claimer.save_report(report)
                history = (root / "history.jsonl").read_text(encoding="utf-8").splitlines()

            self.assertEqual(len(history), 2)
            self.assertNotIn("server_code", json.loads(history[0]))
            self.assertEqual(json.loads(history[0])["slots"][0]["verification"], "unverified")

    def test_update_claim_state_wraps_day_14_to_day_1(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with mock.patch.object(claimer, "CLAIM_STATE_PATH", root / "claim_state.json"), mock.patch.object(
                claimer, "STATE_LOCK_PATH", root / ".claim_state.lock"
            ):
                claimer.update_claim_state(1, 14)
                state = claimer.load_claim_state()

            self.assertEqual(state["1"]["claimed_day"], 14)
            self.assertEqual(state["1"]["next_day"], 1)
            self.assertEqual(state["1"]["next_reward"], claimer.DAILY_REWARDS[1])

    def test_missing_claim_state_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with mock.patch.object(claimer, "CLAIM_STATE_PATH", root / "claim_state.json"), mock.patch.object(
                claimer, "STATE_LOCK_PATH", root / ".claim_state.lock"
            ):
                self.assertEqual(claimer.load_claim_state(), {})

    def test_non_dict_claim_state_json_is_preserved_as_corrupt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "claim_state.json"
            state_path.write_text("[1, 2, 3]", encoding="utf-8")
            with mock.patch.object(claimer, "CLAIM_STATE_PATH", state_path), mock.patch.object(
                claimer, "STATE_LOCK_PATH", root / ".claim_state.lock"
            ):
                with self.assertRaises(RuntimeError):
                    claimer.load_claim_state()
            self.assertFalse(state_path.exists())
            self.assertEqual(len(list(root.glob("claim_state.corrupt_*.json"))), 1)

    def test_unicode_corrupt_claim_state_is_quarantined(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "claim_state.json"
            state_path.write_bytes(b'{"slot": "\xff"}')
            with mock.patch.object(claimer, "CLAIM_STATE_PATH", state_path), mock.patch.object(
                claimer, "STATE_LOCK_PATH", root / ".claim_state.lock"
            ):
                with self.assertRaisesRegex(RuntimeError, "corrupt"):
                    claimer.load_claim_state()

            quarantined = list(root.glob("claim_state.corrupt_*.json"))
            self.assertFalse(state_path.exists())
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_bytes(), b'{"slot": "\xff"}')

    def test_report_text_file_marks_reward_as_unverified(self):
        report = claimer.RunReport(
            app_version="test",
            started_utc="start",
            finished_utc="finish",
            total_seconds=1.0,
            access_mode="owner",
            server_profile="My private server",
            configured_slot_count=1,
            slots=[
                claimer.SlotResult(
                    slot=1,
                    status="claimed_unverified",
                    day=7,
                    seconds=1.0,
                    reward="+200 Mastery Points",
                    next_day=8,
                    next_reward=claimer.DAILY_REWARDS[8],
                    verification="unverified",
                    server_route="public_fallback",
                )
            ],
            errors=[],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with mock.patch.object(claimer, "REPORT_DIR", root), mock.patch.object(
                claimer, "HISTORY_PATH", root / "history.jsonl"
            ), mock.patch.object(claimer, "HISTORY_LOCK_PATH", root / ".history.lock"):
                json_path, text_path = claimer.save_report(report)
                text = text_path.read_text(encoding="utf-8")
                payload = json.loads(json_path.read_text(encoding="utf-8"))

        self.assertIn("CLAIMED (UNVERIFIED) Day 7", text)
        self.assertIn("[PUBLIC FALLBACK]", text)
        self.assertNotIn("CLAIMED\n", text)
        self.assertEqual(payload["slots"][0]["verification"], "unverified")

    def test_report_files_are_atomically_replaced_without_temporary_files(self):
        report = claimer.RunReport(
            app_version="test",
            started_utc="start",
            finished_utc="finish",
            total_seconds=1.0,
            access_mode="owner",
            server_profile="My private server",
            configured_slot_count=1,
            slots=[],
            errors=[],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with mock.patch.object(claimer, "REPORT_DIR", root), mock.patch.object(
                claimer, "HISTORY_PATH", root / "history.jsonl"
            ), mock.patch.object(claimer, "HISTORY_LOCK_PATH", root / ".history.lock"):
                json_path, text_path = claimer.save_report(report)

            self.assertTrue(json_path.is_file())
            self.assertTrue(text_path.is_file())
            self.assertFalse(list(root.glob("*.tmp")))


class JoinCodeRejectionTests(unittest.TestCase):
    """open_private_slot() is exercised via a bare_runner() instance so no
    real screen capture, click, or window focus ever happens."""

    def _rejected(self, **runner_attrs):
        runner = bare_runner(**runner_attrs)
        runner.wait_for = mock.MagicMock()
        runner.open_slot_select = mock.MagicMock()
        runner.click_transition = mock.MagicMock()
        runner.click = mock.MagicMock()
        runner.require_focus = mock.MagicMock()
        runner.input = mock.MagicMock()
        runner.detector = mock.MagicMock()
        runner.detector.matches.return_value = (False, 0.0)
        runner.tap = mock.MagicMock()
        runner.wait_until_gone = mock.MagicMock(side_effect=TimeoutError("no confirmation"))
        runner.screenshot = mock.MagicMock(return_value="screen")
        runner.save_diagnostic = mock.MagicMock(return_value=Path("diagnostic_join_rejected.png"))
        with mock.patch.object(claimer.time, "sleep"), self.assertRaises(
            claimer.JoinCodeRejected
        ) as context:
            runner.open_private_slot(1)
        return str(context.exception)

    def test_shared_mode_rejection_mentions_current_code_failure_causes(self):
        message = self._rejected(access_mode="shared", server_code="ABC123")
        self.assertIn("invalid, manually regenerated", message)
        self.assertIn("lost private-network access", message)
        self.assertIn("diagnostic_join_rejected.png", message)

    def test_shared_mode_rejection_never_leaks_the_raw_code(self):
        message = self._rejected(access_mode="shared", server_code="TOPSECRET")
        self.assertNotIn("TOPSECRET", message)

    def test_partial_shared_code_input_is_marked_for_cleanup_on_stop_or_focus_loss(self):
        for failure in (claimer.RunStopped(), claimer.FocusLost("lost focus")):
            with self.subTest(failure=type(failure).__name__):
                runner = bare_runner(access_mode="shared", server_code="TOPSECRET")
                runner.screenshot = mock.MagicMock(return_value="screen")
                runner.wait_for = mock.MagicMock()
                runner.click_transition = mock.MagicMock()
                runner.click_until_gone = mock.MagicMock()
                runner.click = mock.MagicMock()
                runner.require_focus = mock.MagicMock()
                runner.input = mock.MagicMock()
                runner.input.replace_text.side_effect = failure
                runner.detector.matches.return_value = (True, 1.0)
                with mock.patch.object(claimer.time, "sleep"), self.assertRaises(
                    type(failure)
                ):
                    runner.open_private_slot(1)
                self.assertTrue(runner._shared_code_may_be_visible)

    def test_cleanup_clears_code_only_after_confirming_private_panel(self):
        geometry = claimer.RobloxGeometry(123, 0, 0, 1920, 1080)
        runner = bare_runner(
            access_mode="shared",
            server_code="TOPSECRET",
            geometry=geometry,
            capture=mock.MagicMock(),
            _shared_code_may_be_visible=True,
        )
        runner.process_running = mock.MagicMock(return_value=True)
        runner.input = mock.MagicMock()
        runner.require_focus = mock.MagicMock()
        runner.click = mock.MagicMock()
        runner.detector.matches.return_value = (True, 0.99)
        with mock.patch.object(claimer, "focus_roblox", return_value=True), mock.patch.object(
            claimer, "_client_geometry", return_value=geometry
        ):
            runner.clear_visible_shared_code()

        runner.click.assert_called_once_with(*claimer.PRIVATE_CODE_FIELD, cleanup=True)
        runner.input.replace_text.assert_called_once_with("", runner.require_focus)
        self.assertFalse(runner._shared_code_may_be_visible)

    def test_owner_mode_rejection_message_differs_from_shared_mode(self):
        message = self._rejected(access_mode="owner")
        self.assertIn("auto-filled private server code", message)
        self.assertNotIn("manually regenerated", message)

    def test_slot_above_configured_limit_raises_before_touching_the_screen(self):
        runner = bare_runner(configured_slot_count=3)
        runner.wait_for = mock.MagicMock(side_effect=AssertionError("should not be called"))
        with self.assertRaises(claimer.SlotUnavailable) as context:
            runner.open_private_slot(4)
        self.assertIn("Slot 4", str(context.exception))
        self.assertIn("3-slot limit", str(context.exception))

    def test_click_refuses_a_point_obstructed_by_another_window(self):
        geometry = claimer.RobloxGeometry(123, 10, 20, 1920, 1080)
        runner = bare_runner(geometry=geometry)
        runner.require_focus = mock.MagicMock()
        runner.input = mock.MagicMock()

        with mock.patch.object(
            claimer, "process_name_at_screen_point", return_value="MorisMacroMaker.exe"
        ), self.assertRaises(claimer.FocusLost) as context:
            runner.click(1520, 625)

        self.assertIn("obstructed by MorisMacroMaker.exe", str(context.exception))
        runner.input.click.assert_not_called()

    def test_click_accepts_an_unobstructed_roblox_point(self):
        geometry = claimer.RobloxGeometry(123, 10, 20, 1920, 1080)
        runner = bare_runner(geometry=geometry)
        runner.require_focus = mock.MagicMock()
        runner.input = mock.MagicMock()

        with mock.patch.object(
            claimer, "process_name_at_screen_point", return_value="RobloxPlayerBeta.exe"
        ) as process_at_point:
            runner.click(1420, 625)

        process_at_point.assert_called_once_with(1430, 645)
        runner.input.click.assert_called_once_with(1420, 625, mock.ANY, 1)
        runner.stop_event.set()
        with self.assertRaises(claimer.RunStopped):
            runner.input.click.call_args.args[2]()

    def test_navigation_uses_the_exact_confirmed_click_sequence(self):
        runner = bare_runner(access_mode="shared", server_code="FriendCode")
        runner.screenshot = mock.MagicMock(return_value="screen")
        runner.detector.matches.return_value = (False, 0.0)
        runner.wait_for = mock.MagicMock()
        runner.open_slot_select = mock.MagicMock()
        runner.click_transition = mock.MagicMock()
        runner.click_until_gone = mock.MagicMock()
        runner.click = mock.MagicMock()
        runner.tap = mock.MagicMock()
        runner.wait_until_gone = mock.MagicMock()
        runner.require_focus = mock.MagicMock()
        runner.input = mock.MagicMock()

        with mock.patch.object(claimer.time, "sleep"):
            runner.open_private_slot(3)

        self.assertEqual(
            runner.click_transition.call_args_list,
            [
                mock.call(*runner.slot_point(3), "server_menu", 0.8, "slot_select"),
                mock.call(*claimer.DESTINATION_NORTH_EARTH, "private_panel", 8.0, "server_menu"),
            ],
        )
        runner.open_slot_select.assert_called_once_with()
        runner.wait_for.assert_not_called()
        self.assertEqual(
            runner.click.call_args_list,
            [mock.call(*claimer.PRIVATE_CODE_FIELD)],
        )
        runner.input.replace_text.assert_called_once_with("FriendCode", runner.normal_input_guard)
        runner.tap.assert_called_once_with("enter")
        runner.wait_until_gone.assert_called_once_with("private_panel", 15.0)
        runner.click_until_gone.assert_not_called()
        self.assertFalse(runner._shared_code_may_be_visible)

    def test_recognized_slot_list_after_slot_click_becomes_slot_unavailable(self):
        runner = bare_runner()
        runner.screenshot = mock.MagicMock(return_value="slot-screen")
        runner.detector.matches.side_effect = [(True, 0.9), (True, 0.9)]
        runner.click_transition = mock.MagicMock(side_effect=TimeoutError("server menu absent"))
        runner.wait_for = mock.MagicMock(side_effect=TimeoutError("still absent after the load grace"))

        with mock.patch.object(claimer.time, "sleep"), self.assertRaises(
            claimer.SlotUnavailable
        ) as context:
            runner.open_private_slot(2)

        self.assertIn("empty, unused, locked, or unpurchased", str(context.exception))

    def test_unrecognized_screen_after_slot_click_stops_as_unsafe(self):
        runner = bare_runner()
        runner.screenshot = mock.MagicMock(return_value="character-creation-screen")
        runner.detector.matches.side_effect = [(True, 0.9), (False, 0.1)]
        runner.click_transition = mock.MagicMock(side_effect=TimeoutError("server menu absent"))
        runner.wait_for = mock.MagicMock(side_effect=TimeoutError("still absent after the load grace"))

        with mock.patch.object(claimer.time, "sleep"), self.assertRaises(
            claimer.UnsafeGameState
        ) as context:
            runner.open_private_slot(2)

        self.assertIn("unrecognized screen", str(context.exception))
        runner.click_transition.assert_called_once_with(
            *runner.slot_point(2), "server_menu", 0.8, "slot_select"
        )

    def test_shared_rejection_clears_the_field_without_leaking_the_code(self):
        secret = "TOPSECRET"
        logged: list[str] = []
        runner = bare_runner(access_mode="shared", server_code=secret, log=logged.append)
        runner.screenshot = mock.MagicMock(return_value="screen")
        runner.detector.matches.return_value = (True, 0.9)
        runner.click_transition = mock.MagicMock()
        runner.click = mock.MagicMock()
        runner.require_focus = mock.MagicMock()
        runner.input = mock.MagicMock()
        runner.tap = mock.MagicMock()
        runner.wait_until_gone = mock.MagicMock(side_effect=TimeoutError("not accepted"))
        runner.save_diagnostic = mock.MagicMock(return_value=Path("join.png"))

        with mock.patch.object(claimer.time, "sleep"), self.assertRaises(
            claimer.JoinCodeRejected
        ) as context:
            runner.open_private_slot(1)

        self.assertNotIn(secret, str(context.exception))
        self.assertNotIn(secret, "\n".join(logged))
        self.assertEqual(
            runner.input.replace_text.call_args_list,
            [mock.call(secret, runner.normal_input_guard), mock.call("", runner.require_focus)],
        )
        self.assertEqual(runner.tap.call_args_list, [mock.call("enter")] * 2)
        self.assertEqual(runner.wait_until_gone.call_args_list, [mock.call("private_panel", 15.0)] * 2)
        self.assertEqual(runner.click.call_args_list, [mock.call(*claimer.PRIVATE_CODE_FIELD)] * 3)
        self.assertFalse(runner._shared_code_may_be_visible)


class ProcessSlotTests(unittest.TestCase):
    """process_slot() drives claim_state + reward reporting; exercised on a
    bare_runner() with every screen/click/timing call replaced by a mock."""

    def test_claim_is_reported_truthfully_as_unverified(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("daily", "screen"))
        runner.tap = mock.MagicMock()
        runner.detect_claim_day = mock.MagicMock(return_value=5)
        runner.click_claim_button = mock.MagicMock(return_value="screen-after-claim")
        runner.read_receipt_rewards = mock.MagicMock(return_value=(None, "screen-after-claim"))
        runner.screenshot = mock.MagicMock(return_value="screen-after-claim")
        runner.save_receipt_region = mock.MagicMock(return_value=Path("receipt_slot_01.png"))
        runner.return_to_main_menu = mock.MagicMock()

        with mock.patch.object(claimer, "load_claim_state", return_value={}), mock.patch.object(
            claimer, "update_claim_state"
        ) as update_state, mock.patch(
            "time.sleep"
        ):
            result = runner.process_slot(1)

        update_state.assert_called_once_with(1, 5)
        self.assertEqual(result.status, "claimed_unverified")
        self.assertEqual(result.verification, "unverified")
        self.assertEqual(result.reward, claimer.DAILY_REWARDS[5])
        self.assertEqual(result.next_day, 6)
        self.assertIn("wasn't machine-verified", result.detail)
        runner.click_claim_button.assert_called_once_with(1, 5, mock.ANY)
        runner.read_receipt_rewards.assert_called_once_with("screen-after-claim")
        runner.screenshot.assert_not_called()
        runner.return_to_main_menu.assert_called_once()

    def test_claim_result_survives_a_failed_return_to_main_menu(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("daily", "screen"))
        runner.tap = mock.MagicMock()
        runner.detect_claim_day = mock.MagicMock(return_value=5)
        runner.click_claim_button = mock.MagicMock(return_value="screen-after-claim")
        runner.read_receipt_rewards = mock.MagicMock(return_value=(None, "screen-after-claim"))
        runner.screenshot = mock.MagicMock(return_value="screen-after-claim")
        runner.save_receipt_region = mock.MagicMock(return_value=Path("receipt_slot_01.png"))
        runner.return_to_main_menu = mock.MagicMock(side_effect=TimeoutError("travel menu stayed open"))
        runner.detector.matches.return_value = (False, 0.0)

        with mock.patch.object(claimer, "load_claim_state", return_value={}), mock.patch.object(
            claimer, "update_claim_state"
        ), mock.patch("time.sleep"), self.assertRaises(
            claimer.PostClaimNavigationFailed
        ) as context:
            runner.process_slot(1)

        self.assertEqual(context.exception.result.status, "claimed_unverified")
        self.assertEqual(context.exception.result.day, 5)

    def test_confirmed_claim_is_persisted_before_receipt_focus_failure(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("daily", "screen"))
        runner.tap = mock.MagicMock()
        runner.detect_claim_day = mock.MagicMock(return_value=5)
        runner.click_claim_button = mock.MagicMock(return_value="screen-after-claim")
        runner.read_receipt_rewards = mock.MagicMock(side_effect=claimer.FocusLost("focus changed"))
        runner.screenshot = mock.MagicMock(side_effect=claimer.FocusLost("focus changed"))

        with mock.patch.object(claimer, "load_claim_state", return_value={}), mock.patch.object(
            claimer, "update_claim_state"
        ) as update_state, mock.patch("time.sleep"), self.assertRaises(claimer.PostClaimNavigationFailed) as caught:
            runner.process_slot(1)

        update_state.assert_called_once_with(1, 5)
        self.assertEqual(caught.exception.result.status, "claimed_unverified")
        self.assertEqual(caught.exception.result.day, 5)
        runner.read_receipt_rewards.assert_called_once_with("screen-after-claim")
        runner.screenshot.assert_not_called()

    def test_streak_mismatch_refuses_claim_instead_of_guessing(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("daily", "screen"))
        runner.tap = mock.MagicMock()
        runner.detect_claim_day = mock.MagicMock(return_value=1)
        runner.click_claim_button = mock.MagicMock()

        with mock.patch.object(
            claimer, "load_claim_state", return_value={"1": {"next_day": 8}}
        ), self.assertRaisesRegex(RuntimeError, "expected Day 8"):
            runner.process_slot(1)

        runner.click_claim_button.assert_not_called()

    def test_no_daily_result_survives_a_failed_return_to_main_menu(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("world", "screen"))
        runner.tap = mock.MagicMock()
        runner.wait_for_daily_grace = mock.MagicMock(return_value=(False, None))
        runner.return_to_main_menu = mock.MagicMock(side_effect=TimeoutError("travel menu stayed open"))

        with mock.patch.object(claimer, "load_claim_state", return_value={}), self.assertRaises(
            claimer.PostClaimNavigationFailed
        ) as context:
            runner.process_slot(1)

        self.assertEqual(context.exception.result.status, "no_daily")
        self.assertIsNone(context.exception.result.day)

    def test_claim_on_day_14_reports_next_day_as_1(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("daily", "screen"))
        runner.tap = mock.MagicMock()
        runner.detect_claim_day = mock.MagicMock(return_value=14)
        runner.click_claim_button = mock.MagicMock(return_value="screen-after-claim")
        runner.read_receipt_rewards = mock.MagicMock(return_value=(None, "screen-after-claim"))
        runner.screenshot = mock.MagicMock(return_value="screen-after-claim")
        runner.save_receipt_region = mock.MagicMock(return_value=Path("receipt_slot_01.png"))
        runner.return_to_main_menu = mock.MagicMock()

        with mock.patch.object(claimer, "load_claim_state", return_value={}), mock.patch.object(
            claimer, "update_claim_state"
        ), mock.patch("time.sleep"):
            result = runner.process_slot(1)

        self.assertEqual(result.next_day, 1)
        self.assertEqual(result.next_reward, claimer.DAILY_REWARDS[1])

    def test_global_reward_lock_is_classified_and_saved_as_received_reward(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("daily", "screen"))
        runner.tap = mock.MagicMock()
        runner.detect_claim_day = mock.MagicMock(return_value=11)
        runner.click_claim_button = mock.MagicMock(return_value="receipt-screen")
        runner.read_receipt_rewards = mock.MagicMock()
        runner.screenshot = mock.MagicMock(return_value="receipt-screen")
        runner.save_receipt_region = mock.MagicMock(return_value=Path("receipt_slot_01.png"))
        runner.return_to_main_menu = mock.MagicMock()
        runner.detector.matches.side_effect = lambda _screen, name: (name == "global_reward_locked", 0.95)

        with mock.patch.object(claimer, "load_claim_state", return_value={}), mock.patch.object(
            claimer, "update_claim_state"
        ) as update_state, mock.patch(
            "time.sleep"
        ):
            result = runner.process_slot(1)

        self.assertEqual(result.status, "claimed_global_locked")
        self.assertEqual(result.verification, "global_locked")
        self.assertEqual(result.received_reward, "+10 Mastery Points")
        self.assertEqual(result.server_route, "private")
        self.assertEqual(
            update_state.call_args_list,
            [
                mock.call(1, 11),
                mock.call(
                    1,
                    11,
                    verification="global_locked",
                    received_reward="+10 Mastery Points",
                ),
            ],
        )
        self.assertEqual(runner.detector.matches.call_args_list, [
            mock.call("screen", "restore_streak"),
            mock.call("receipt-screen", "global_reward_locked"),
        ])
        runner.read_receipt_rewards.assert_not_called()
        runner.save_receipt_region.assert_called_once_with("receipt-screen", 1)
        runner.screenshot.assert_not_called()

    def test_public_fallback_is_opt_in_and_runs_only_after_private_rejection(self):
        runner = bare_runner(allow_public_fallback=True)
        private = mock.MagicMock(side_effect=claimer.JoinCodeRejected("rejected"))
        fallback = mock.MagicMock()
        runner.open_private_slot = private
        runner.open_public_fallback = fallback
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("world", "screen"))
        runner.tap = mock.MagicMock()
        runner.wait_for_daily_grace = mock.MagicMock(return_value=(False, None))
        runner.return_to_main_menu = mock.MagicMock()

        with mock.patch.object(claimer, "load_claim_state", return_value={}):
            result = runner.process_slot(1)

        private.assert_called_once_with(1)
        fallback.assert_called_once_with(1)
        self.assertEqual(result.server_route, "public_fallback")

    def test_private_rejection_does_not_use_public_fallback_without_opt_in(self):
        runner = bare_runner(allow_public_fallback=False)
        runner.open_private_slot = mock.MagicMock(side_effect=claimer.JoinCodeRejected("rejected"))
        runner.open_public_fallback = mock.MagicMock()

        with self.assertRaises(claimer.JoinCodeRejected):
            runner.process_slot(1)

        runner.open_public_fallback.assert_not_called()

    def test_no_daily_reports_last_known_state_without_reclaiming(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("world", "screen"))
        runner.tap = mock.MagicMock()
        runner.wait_for_daily_grace = mock.MagicMock(return_value=(False, None))
        runner.return_to_main_menu = mock.MagicMock()
        saved_state = {
            "1": {
                "claimed_day": 3,
                "reward": "Random Dragon Ball (+100 MP if all held)",
                "next_day": 4,
                "next_reward": "30 min 2Ã— XP",
            }
        }

        with mock.patch.object(claimer, "update_claim_state") as update_state, mock.patch.object(
            claimer, "load_claim_state", return_value=saved_state
        ):
            result = runner.process_slot(1)

        update_state.assert_not_called()
        self.assertEqual(result.status, "no_daily")
        self.assertEqual(result.day, 3)
        self.assertEqual(result.next_day, 4)
        self.assertEqual(result.verification, "not_applicable")

    def test_no_daily_with_no_recorded_history_reports_unrecorded_streak(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("world", "screen"))
        runner.tap = mock.MagicMock()
        runner.wait_for_daily_grace = mock.MagicMock(return_value=(False, None))
        runner.return_to_main_menu = mock.MagicMock()

        with mock.patch.object(claimer, "update_claim_state") as update_state, mock.patch.object(
            claimer, "load_claim_state", return_value={}
        ):
            result = runner.process_slot(1)

        update_state.assert_not_called()
        self.assertEqual(result.status, "no_daily")
        self.assertIsNone(result.day)


class AppendUnattemptedTests(unittest.TestCase):
    def test_marks_only_slots_not_already_recorded(self):
        runner = bare_runner(
            slots=[1, 2, 3],
            results=[claimer.SlotResult(slot=1, status="claimed_unverified", day=1, seconds=1.0)],
        )
        runner.append_unattempted("Run stopped by user")
        statuses = {result.slot: result.status for result in runner.results}
        self.assertEqual(statuses[1], "claimed_unverified")
        self.assertEqual(statuses[2], "not_attempted")
        self.assertEqual(statuses[3], "not_attempted")


class RunnerStateRoutingTests(unittest.TestCase):
    def test_slot_unavailable_is_recorded_and_the_next_slot_continues(self):
        geometry = claimer.RobloxGeometry(hwnd=100, left=0, top=0, width=1920, height=1080)
        runner = bare_runner(
            slots=[1, 2],
            geometry=geometry,
            server_profile="My private server",
            close_when_done=False,
            roblox_handle=None,
            run_mutex=None,
            _window_restore=None,
        )
        runner.acquire_run_mutex = mock.MagicMock()
        runner.stop_event = mock.MagicMock()
        runner.stop_event.is_set.return_value = False
        runner.ensure_roblox_window = mock.MagicMock(return_value=True)
        runner.attach_roblox_process = mock.MagicMock()
        runner.input = mock.MagicMock()
        runner.finish_launch = mock.MagicMock()
        runner.process_running = mock.MagicMock(return_value=False)
        runner.process_slot = mock.MagicMock(
            side_effect=[
                claimer.SlotUnavailable("Slot 1 stayed unavailable after selection"),
                claimer.SlotResult(slot=2, status="no_daily", day=None, seconds=0.1),
            ]
        )

        with mock.patch.object(claimer, "keep_display_awake"), mock.patch.object(
            claimer, "release_display_awake"
        ), mock.patch.object(claimer, "wait_for_stable_geometry", return_value=geometry), mock.patch.object(
            claimer, "focus_roblox", return_value=True
        ), mock.patch.object(claimer, "prepare_roblox_window", return_value=(geometry, None)), mock.patch.object(
            claimer, "ScreenCapture", return_value=mock.MagicMock()
        ), mock.patch.object(claimer, "save_report"), mock.patch.object(claimer, "save_partial_report"):
            report = runner.run()

        self.assertEqual([result.status for result in report.slots], ["slot_missing", "no_daily"])
        self.assertEqual(runner.process_slot.call_count, 2)

    def test_post_claim_navigation_failure_preserves_result_and_stops_later_slots(self):
        geometry = claimer.RobloxGeometry(hwnd=100, left=0, top=0, width=1920, height=1080)
        preserved = claimer.SlotResult(slot=1, status="claimed_unverified", day=5, seconds=1.0)
        runner = bare_runner(
            slots=[1, 2],
            geometry=geometry,
            server_profile="My private server",
            close_when_done=False,
            roblox_handle=None,
            run_mutex=None,
            _window_restore=None,
        )
        runner.acquire_run_mutex = mock.MagicMock()
        runner.stop_event = mock.MagicMock()
        runner.stop_event.is_set.return_value = False
        runner.ensure_roblox_window = mock.MagicMock(return_value=True)
        runner.attach_roblox_process = mock.MagicMock()
        runner.input = mock.MagicMock()
        runner.finish_launch = mock.MagicMock()
        runner.process_running = mock.MagicMock(return_value=False)
        runner.process_slot = mock.MagicMock(
            side_effect=claimer.PostClaimNavigationFailed(preserved, TimeoutError("travel menu stayed open"))
        )
        runner.screenshot = mock.MagicMock(return_value="failure-screen")
        runner.save_diagnostic = mock.MagicMock(return_value=Path("navigation.png"))

        with mock.patch.object(claimer, "keep_display_awake"), mock.patch.object(
            claimer, "release_display_awake"
        ), mock.patch.object(claimer, "wait_for_stable_geometry", return_value=geometry), mock.patch.object(
            claimer, "focus_roblox", return_value=True
        ), mock.patch.object(claimer, "prepare_roblox_window", return_value=(geometry, None)), mock.patch.object(
            claimer, "ScreenCapture", return_value=mock.MagicMock()
        ), mock.patch.object(claimer, "save_report"), mock.patch.object(claimer, "save_partial_report"):
            report = runner.run()

        self.assertEqual([result.status for result in report.slots], ["claimed_unverified", "not_attempted"])
        self.assertIn("post-result navigation failed", report.slots[1].detail)
        self.assertEqual(runner.process_slot.call_count, 1)
        runner.save_diagnostic.assert_called_once_with("failure-screen", "slot_1_post_result_navigation")
        self.assertIn("navigation.png", report.errors[0])


class SafeNavigationTests(unittest.TestCase):
    def test_open_slot_select_returns_without_clicking_when_already_visible(self):
        runner = bare_runner()
        runner.screenshot = mock.MagicMock(return_value="slot-screen")
        runner.detector.matches.return_value = (True, 0.99)
        runner.click = mock.MagicMock()

        runner.open_slot_select()

        runner.click.assert_not_called()

    def test_open_slot_select_clicks_immediately_and_waits_for_target(self):
        runner = bare_runner()
        runner.screenshot = mock.MagicMock(return_value="main-screen")
        runner.detector.matches.side_effect = [(False, 0.0), (True, 0.99)]
        runner.click = mock.MagicMock()
        runner.wait_for = mock.MagicMock(return_value=("slot-screen", 0.99))

        runner.open_slot_select()

        runner.click.assert_called_once_with(*claimer.PLAY_BUTTON)
        runner.wait_for.assert_called_once_with("slot_select", mock.ANY)

    def test_title_screen_gets_a_click_while_waiting_for_the_main_menu(self):
        runner = bare_runner()
        runner.click = mock.MagicMock()
        runner.detector.matches.return_value = (False, 0.0)

        def wait_for(_target, _timeout, on_frame):
            with mock.patch.object(claimer.time, "monotonic", return_value=2.0):
                on_frame("title-screen", 0.0)
            return "main-screen", 1.0

        runner.wait_for = mock.MagicMock(side_effect=wait_for)
        with mock.patch.object(claimer.time, "monotonic", return_value=0.0), mock.patch.object(
            claimer, "roblox_focused", return_value=True
        ):
            runner.wait_for_main_menu(5.0)

        runner.click.assert_called_once_with(960, 540)

    def test_title_click_waits_while_roblox_is_not_in_front(self):
        runner = bare_runner()
        runner.click = mock.MagicMock()
        runner.detector.matches.return_value = (False, 0.0)

        def wait_for(_target, _timeout, on_frame):
            with mock.patch.object(claimer.time, "monotonic", return_value=2.0):
                on_frame("title-screen", 0.0)
            return "main-screen", 1.0

        runner.wait_for = mock.MagicMock(side_effect=wait_for)
        with mock.patch.object(claimer.time, "monotonic", return_value=0.0), mock.patch.object(
            claimer, "roblox_focused", return_value=False
        ):
            runner.wait_for_main_menu(5.0)

        runner.click.assert_not_called()

    def test_title_click_never_fires_while_world_is_visible(self):
        runner = bare_runner()
        runner.click = mock.MagicMock()
        runner.detector.matches.return_value = (True, 0.99)

        def wait_for(_target, _timeout, on_frame):
            with mock.patch.object(claimer.time, "monotonic", return_value=2.0):
                on_frame("world-screen", 0.0)
            return "main-screen", 1.0

        runner.wait_for = mock.MagicMock(side_effect=wait_for)
        with mock.patch.object(claimer.time, "monotonic", return_value=0.0):
            runner.wait_for_main_menu(5.0)

        runner.click.assert_not_called()

    def test_title_click_never_fires_over_any_known_live_panel(self):
        for unsafe_state in ("daily_rewards", "game_menu", "travel_menu"):
            with self.subTest(unsafe_state=unsafe_state):
                runner = bare_runner()
                runner.click = mock.MagicMock()
                runner.detector.matches.side_effect = lambda _screen, name: (
                    name == unsafe_state,
                    0.99 if name == unsafe_state else 0.0,
                )

                def wait_for(_target, _timeout, on_frame):
                    with mock.patch.object(claimer.time, "monotonic", return_value=2.0):
                        on_frame("unsafe-screen", 0.0)
                    return "main-screen", 1.0

                runner.wait_for = mock.MagicMock(side_effect=wait_for)
                with mock.patch.object(claimer.time, "monotonic", return_value=0.0):
                    runner.wait_for_main_menu(5.0)

                runner.click.assert_not_called()

    def test_confirmation_retry_stops_when_source_disappeared(self):
        runner = bare_runner()
        runner.click = mock.MagicMock()
        runner.wait_until_gone = mock.MagicMock(side_effect=TimeoutError("late transition"))
        runner.screenshot = mock.MagicMock(return_value="screen")
        runner.detector.matches.side_effect = [(True, 0.9), (False, 0.0)]

        runner.click_until_gone(*claimer.DAILY_BACK_BUTTON, "daily_rewards", 5.0)

        runner.click.assert_called_once_with(*claimer.DAILY_BACK_BUTTON, 1)

    def test_pending_callback_runs_only_after_fresh_source_guard(self):
        events: list[str] = []
        runner = bare_runner()
        runner.screenshot = mock.MagicMock(return_value="daily")
        def panel_visible(*_args):
            events.append("panel")
            return True, 0.9

        runner.detector.matches.side_effect = panel_visible
        runner.read_streak = mock.MagicMock(side_effect=[4, 5])
        runner.mark_pending_claim.side_effect = lambda *_args: events.append("pending")

        def send_claim(*_args, before_button_send):
            before_button_send()
            events.append("click")

        runner.click = mock.MagicMock(side_effect=send_claim)
        with mock.patch.object(claimer.time, "sleep"):
            self.assertEqual(runner.click_claim_button(1, 5, lambda frame: events.append("validated")), "daily")
        self.assertEqual(events, ["panel", "validated", "pending", "click"])
        runner.mark_pending_claim.assert_called_once_with(1, 5)
        runner.click.assert_called_once_with(*claimer.CLAIM_BUTTON, before_button_send=mock.ANY)

    def test_no_pending_marker_or_claim_when_daily_source_disappeared(self):
        runner = bare_runner()
        runner.screenshot = mock.MagicMock(return_value="unknown")
        runner.detector.matches.return_value = (False, 0.0)
        runner.click = mock.MagicMock()

        validate_source = mock.MagicMock()
        with self.assertRaises(claimer.UnsafeGameState):
            runner.click_claim_button(1, 5, validate_source)

        validate_source.assert_not_called()
        runner.mark_pending_claim.assert_not_called()
        runner.click.assert_not_called()


class WindowLifecycleTests(unittest.TestCase):
    def test_wait_for_stable_geometry_follows_a_replaced_launch_hwnd(self):
        replacement = claimer.RobloxGeometry(hwnd=222, left=10, top=20, width=1920, height=1080)
        with mock.patch.object(
            claimer, "_client_geometry", side_effect=[None, replacement]
        ) as client_geometry, mock.patch.object(
            claimer, "find_roblox_window", return_value=replacement
        ) as find_window, mock.patch.object(claimer.time, "monotonic", return_value=0), mock.patch.object(
            claimer.time, "sleep"
        ):
            stable = claimer.wait_for_stable_geometry(111, timeout=1.0, stable_for=0.0)

        self.assertEqual(stable, replacement)
        self.assertEqual(client_geometry.call_args_list, [mock.call(111), mock.call(222)])
        find_window.assert_called_once_with()

    def test_live_log_is_created_only_after_the_run_mutex_is_acquired(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            live_log = root / "live_run.log"
            kernel32 = mock.MagicMock()
            kernel32.CreateMutexW.return_value = 55
            runner = bare_runner()
            with mock.patch.object(claimer, "REPORT_DIR", root), mock.patch.object(
                claimer, "LIVE_LOG_PATH", live_log
            ), mock.patch.object(claimer.ctypes.windll, "kernel32", kernel32):
                kernel32.GetLastError.return_value = 183
                with self.assertRaisesRegex(RuntimeError, "already controlling"):
                    runner.acquire_run_mutex()
                self.assertFalse(live_log.exists())

                kernel32.GetLastError.return_value = 0
                runner.acquire_run_mutex()

            self.assertTrue(live_log.exists())
            self.assertIn(claimer.APP_NAME, live_log.read_text(encoding="utf-8"))
            kernel32.CloseHandle.assert_called_once_with(55)


class DirectInputClickTests(unittest.TestCase):
    def test_click_uses_raw_virtual_desktop_sendinput(self):
        geometry = claimer.RobloxGeometry(hwnd=123, left=-1920, top=0, width=1920, height=1080)
        direct_input = claimer.DirectInput(geometry)
        direct_input.user32 = mock.MagicMock()
        direct_input.user32.GetSystemMetrics.side_effect = {
            76: -1920,
            77: 0,
            78: 4480,
            79: 1600,
        }.get
        direct_input.user32.SetCursorPos.return_value = True
        direct_input.user32.SendInput.side_effect = lambda count, _events, _size: count
        focus_guard = mock.MagicMock()

        with mock.patch.object(claimer, "focus_roblox", return_value=True), mock.patch.object(
            claimer, "_client_geometry", return_value=geometry
        ), mock.patch.object(claimer.pydirectinput, "click") as legacy_click, mock.patch(
            "time.sleep"
        ):
            direct_input.click(960, 540, focus_guard)

        direct_input.user32.SetCursorPos.assert_called_once_with(-960, 540)
        self.assertEqual(direct_input.user32.SendInput.call_count, 5)
        legacy_click.assert_not_called()
        self.assertEqual(focus_guard.call_count, 4)

    def test_click_rejects_partial_sendinput(self):
        geometry = claimer.RobloxGeometry(hwnd=123, left=0, top=0, width=1920, height=1080)
        direct_input = claimer.DirectInput(geometry)
        direct_input.user32 = mock.MagicMock()
        direct_input.user32.GetSystemMetrics.side_effect = {
            76: 0,
            77: 0,
            78: 1920,
            79: 1080,
        }.get
        direct_input.user32.SetCursorPos.return_value = True
        direct_input.user32.SendInput.side_effect = [1, 1, 1, 1, 1]

        with mock.patch.object(claimer, "focus_roblox", return_value=True), mock.patch.object(
            claimer, "_client_geometry", return_value=geometry
        ), mock.patch("time.sleep"), self.assertRaisesRegex(
            RuntimeError, "accepted only 1/2"
        ):
            direct_input.click(960, 540, mock.MagicMock())


class DisplayCleanupHelperTests(unittest.TestCase):
    """Pure WinAPI-call-shape tests: user32/kernel32 are mocked out so no real
    display mode, power state, or mouse event is ever touched."""

    def test_keep_display_awake_sets_continuous_system_and_display_flags(self):
        with mock.patch.object(claimer.ctypes.windll, "kernel32") as kernel32, mock.patch.object(
            claimer.ctypes.windll, "user32"
        ) as user32:
            claimer.keep_display_awake()

        kernel32.SetThreadExecutionState.assert_called_once_with(
            claimer.ES_CONTINUOUS | claimer.ES_SYSTEM_REQUIRED | claimer.ES_DISPLAY_REQUIRED
        )
        self.assertEqual(user32.mouse_event.call_count, 2)

    def test_release_display_awake_clears_system_and_display_flags(self):
        with mock.patch.object(claimer.ctypes.windll, "kernel32") as kernel32:
            claimer.release_display_awake()

        kernel32.SetThreadExecutionState.assert_called_once_with(claimer.ES_CONTINUOUS)


class RunEnvironmentTests(unittest.TestCase):
    def test_secondary_display_selection_fails_closed_without_one(self):
        primary = claimer.DisplayInfo("primary", 0, 0, 1920, 1080, 0, 0, 1920, 1040, True)
        with mock.patch.object(claimer, "list_displays", return_value=[primary]), self.assertRaisesRegex(
            RuntimeError, "none is attached"
        ):
            claimer.resolve_run_display(123, "secondary")

    def test_secondary_display_selection_uses_non_primary_display(self):
        primary = claimer.DisplayInfo("primary", 0, 0, 2560, 1600, 0, 0, 2560, 1560, True)
        secondary = claimer.DisplayInfo("second", -1920, 183, 1920, 1080, -1920, 183, 1920, 1080, False)
        with mock.patch.object(claimer, "list_displays", return_value=[primary, secondary]):
            self.assertEqual(claimer.resolve_run_display(123, "secondary"), secondary)

    def test_high_resolution_display_gets_a_centred_windowed_1080p_client(self):
        display = claimer.DisplayInfo(
            "primary", 0, 0, 2560, 1600, 0, 0, 2560, 1560, True
        )
        self.assertEqual(
            claimer._windowed_target_bounds(display, 16, 39),
            (312, 220, 1936, 1119),
        )

    def test_windowed_1080p_client_is_rejected_when_the_work_area_cannot_fit_it(self):
        display = claimer.DisplayInfo(
            "primary", 0, 0, 1920, 1080, 0, 0, 1920, 1040, True
        )
        self.assertIsNone(claimer._windowed_target_bounds(display, 16, 39))

    def test_process_mute_touches_only_matching_audio_sessions(self):
        from pycaw.pycaw import AudioUtilities

        matching_volume = mock.MagicMock()
        matching_volume.GetMute.return_value = 0
        matching = mock.MagicMock(ProcessId=42, SimpleAudioVolume=matching_volume)
        other_volume = mock.MagicMock()
        other = mock.MagicMock(ProcessId=99, SimpleAudioVolume=other_volume)
        with mock.patch.object(AudioUtilities, "GetAllSessions", return_value=[other, matching]):
            states = claimer.mute_process_audio(42)

        matching_volume.SetMute.assert_called_once_with(1, None)
        other_volume.SetMute.assert_not_called()
        claimer.restore_process_audio(states)
        matching_volume.SetMute.assert_called_with(0, None)


class WindowPreparationRollbackTests(unittest.TestCase):
    def test_partial_window_setup_failure_rolls_back_before_propagating(self):
        geometry = claimer.RobloxGeometry(123, 0, 0, 2560, 1600)
        restore = claimer.WindowRestore(True, (0, 0, 2560, 1600))
        controls = mock.Mock()
        focus_guard = mock.Mock()
        with mock.patch.object(
            claimer, "_window_restore_snapshot", return_value=restore
        ), mock.patch.object(
            claimer,
            "_prepare_roblox_window_unchecked",
            side_effect=RuntimeError("resize failed"),
        ), mock.patch.object(claimer, "restore_roblox_window") as rollback:
            with self.assertRaisesRegex(RuntimeError, "resize failed"):
                claimer.prepare_roblox_window(geometry, controls, focus_guard)

        rollback.assert_called_once_with(restore, geometry, controls, focus_guard)

    def test_partial_setup_reports_when_rollback_also_fails(self):
        geometry = claimer.RobloxGeometry(123, 0, 0, 2560, 1600)
        restore = claimer.WindowRestore(True, (0, 0, 2560, 1600))
        with mock.patch.object(
            claimer, "_window_restore_snapshot", return_value=restore
        ), mock.patch.object(
            claimer,
            "_prepare_roblox_window_unchecked",
            side_effect=RuntimeError("resize failed"),
        ), mock.patch.object(
            claimer,
            "restore_roblox_window",
            side_effect=RuntimeError("rollback failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "rollback also failed"):
                claimer.prepare_roblox_window(geometry, mock.Mock(), mock.Mock())


class FinalAuditRegressionTests(unittest.TestCase):
    def test_find_roblox_window_fails_closed_with_two_usable_windows(self):
        user32 = mock.MagicMock()
        user32.IsWindowVisible.return_value = True
        user32.EnumWindows.side_effect = lambda callback, _data: (
            callback(101, 0) and callback(202, 0)
        )
        geometries = {
            101: claimer.RobloxGeometry(101, 0, 0, 1920, 1080),
            202: claimer.RobloxGeometry(202, -1920, 0, 1920, 1080),
        }
        with mock.patch.object(claimer.ctypes.windll, "user32", user32), mock.patch.object(
            claimer, "_process_image_name", return_value="RobloxPlayerBeta.exe"
        ), mock.patch.object(claimer, "_client_geometry", side_effect=geometries.get):
            with self.assertRaisesRegex(RuntimeError, "Multiple usable Roblox windows"):
                claimer.find_roblox_window()

    def test_exact_display_fullscreen_fallback_supports_negative_origin(self):
        geometry = claimer.RobloxGeometry(123, 0, 0, 1800, 1000)
        target = claimer.DisplayInfo(
            "secondary", -1920, 183, 1920, 1080, -1920, 183, 1920, 1080, False
        )
        exact = claimer.RobloxGeometry(123, -1920, 183, 1920, 1080)
        user32 = mock.MagicMock()
        user32.GetWindowRect.side_effect = lambda _hwnd, pointer: (
            setattr(pointer._obj, "left", 0),
            setattr(pointer._obj, "top", 0),
            setattr(pointer._obj, "right", 1820),
            setattr(pointer._obj, "bottom", 1040),
            True,
        )[-1]
        user32.SetWindowPos.return_value = True
        controls = mock.MagicMock()
        with mock.patch.object(claimer.ctypes.windll, "user32", user32), mock.patch.object(
            claimer, "_display_for_window", return_value=target
        ), mock.patch.object(claimer, "_client_geometry", return_value=exact), mock.patch.object(
            claimer.time, "sleep"
        ), mock.patch.object(claimer.time, "monotonic", return_value=0.0):
            result = claimer._prepare_exact_fullscreen(
                geometry, target, controls, mock.MagicMock()
            )

        self.assertEqual(result, exact)
        user32.SetWindowPos.assert_called_once_with(
            123, None, -1920, 183, 1820, 1040, 0x0004 | 0x0010
        )
        controls.tap.assert_called_once()

    def test_window_prepare_uses_fullscreen_when_frame_cannot_fit_exact_display(self):
        geometry = claimer.RobloxGeometry(123, -1920, 183, 1800, 1000)
        target = claimer.DisplayInfo(
            "secondary", -1920, 183, 1920, 1080, -1920, 183, 1920, 1080, False
        )
        exact = claimer.RobloxGeometry(123, -1920, 183, 1920, 1080)
        monitor = claimer._MONITORINFOEXW()
        monitor.rcMonitor.left = -1920
        monitor.rcMonitor.top = 183
        monitor.rcMonitor.right = 0
        monitor.rcMonitor.bottom = 1263
        user32 = mock.MagicMock()
        user32.GetWindowRect.side_effect = lambda _hwnd, pointer: (
            setattr(pointer._obj, "left", -1920),
            setattr(pointer._obj, "top", 183),
            setattr(pointer._obj, "right", -100),
            setattr(pointer._obj, "bottom", 1223),
            True,
        )[-1]
        with mock.patch.object(claimer.ctypes.windll, "user32", user32), mock.patch.object(
            claimer, "resolve_run_display", return_value=target
        ), mock.patch.object(claimer, "_display_for_window", return_value=target), mock.patch.object(
            claimer, "_monitor_info_for_window", return_value=monitor
        ), mock.patch.object(claimer, "_prepare_exact_fullscreen", return_value=exact) as fallback:
            result, restore = claimer._prepare_roblox_window_unchecked(
                geometry, mock.MagicMock(), mock.MagicMock(), "secondary"
            )

        self.assertEqual(result, exact)
        self.assertFalse(restore.was_fullscreen)
        fallback.assert_called_once()

    def test_window_prepare_sizes_a_large_display_to_windowed_1080p(self):
        geometry = claimer.RobloxGeometry(123, 0, 0, 1800, 1000)
        target = claimer.DisplayInfo(
            "primary", 0, 0, 2560, 1600, 0, 0, 2560, 1560, True
        )
        monitor = claimer._MONITORINFOEXW()
        monitor.rcMonitor.left = 0
        monitor.rcMonitor.top = 0
        monitor.rcMonitor.right = 2560
        monitor.rcMonitor.bottom = 1600
        user32 = mock.MagicMock()

        def window_rect(_hwnd, pointer):
            pointer._obj.left = 0
            pointer._obj.top = 0
            pointer._obj.right = 1816
            pointer._obj.bottom = 1039
            return True

        user32.GetWindowRect.side_effect = window_rect
        user32.SetWindowPos.return_value = True
        updated = claimer.RobloxGeometry(123, 312, 220, 1920, 1080)
        with mock.patch.object(claimer.ctypes.windll, "user32", user32), mock.patch.object(
            claimer, "resolve_run_display", return_value=target
        ), mock.patch.object(claimer, "_display_for_window", return_value=target), mock.patch.object(
            claimer, "_monitor_info_for_window", return_value=monitor
        ), mock.patch.object(claimer, "_client_geometry", return_value=updated), mock.patch.object(
            claimer.time, "monotonic", return_value=0.0
        ):
            result, restore = claimer._prepare_roblox_window_unchecked(
                geometry, mock.MagicMock(), mock.MagicMock(), "current"
            )

        self.assertEqual(result, updated)
        self.assertFalse(restore.was_fullscreen)
        user32.SetWindowPos.assert_called_once_with(
            123, None, 312, 220, 1936, 1119, 0x0004 | 0x0010
        )

    def test_retry_guard_refuses_unknown_source_and_target_without_click(self):
        runner = bare_runner()
        runner.screenshot = mock.MagicMock(return_value="unknown")
        runner.click = mock.MagicMock()
        runner.detector.matches.return_value = (False, 0.0)

        with self.assertRaisesRegex(claimer.UnsafeGameState, "Neither main_menu nor slot_select"):
            runner.click_transition(960, 970, "slot_select", 1.0, "main_menu")

        runner.click.assert_not_called()

    def test_transition_retry_rechecks_source_immediately_before_second_click(self):
        runner = bare_runner()
        runner.screenshot = mock.MagicMock(side_effect=["source", "unknown"])
        runner.click = mock.MagicMock()
        runner.wait_for = mock.MagicMock(side_effect=TimeoutError("late"))
        runner.wait_for_either = mock.MagicMock(return_value=(None, None, 0.0))

        def matches(screen, name):
            return (screen == "source" and name == "main_menu", 0.9)

        runner.detector.matches.side_effect = matches
        with self.assertRaises(claimer.UnsafeGameState):
            runner.click_transition(960, 970, "slot_select", 1.0, "main_menu")

        runner.click.assert_called_once_with(960, 970, 1)

    def test_settled_daily_screen_needs_three_samples_and_minimum_dwell(self):
        runner = bare_runner()
        runner.detector.matches.return_value = (True, 0.9)
        runner.detect_claim_day = mock.MagicMock(return_value=1)
        runner.screenshot = mock.MagicMock(side_effect=["f2", "f3", "f4"])
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(claimer.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(
            claimer.time, "sleep", side_effect=sleep
        ):
            settled = runner.settled_daily_screen("f1")

        self.assertEqual(settled, "f4")
        self.assertEqual(runner.screenshot.call_count, 3)
        self.assertGreaterEqual(clock[0], claimer.DAILY_SETTLE_MIN_DWELL)

    def test_mismatch_always_claims_visually_observed_day_with_warning(self):
        logged: list[str] = []
        runner = bare_runner(log=logged.append, slot_mismatch_policies={1: "always"})
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("daily", "screen"))
        runner.tap = mock.MagicMock()
        runner.detect_claim_day = mock.MagicMock(return_value=3)
        def accept_claim(slot, day, validate_source):
            runner.mark_pending_claim(slot, day)
            return "receipt"

        runner.click_claim_button = mock.MagicMock(side_effect=accept_claim)
        runner.read_receipt_rewards = mock.MagicMock(return_value=(None, "receipt"))
        runner.screenshot = mock.MagicMock(return_value="receipt")
        runner.save_receipt_region = mock.MagicMock(return_value=Path("receipt.png"))
        runner.return_to_main_menu = mock.MagicMock()
        with mock.patch.object(
            claimer, "load_claim_state", return_value={"1": {"next_day": 8}}
        ), mock.patch.object(claimer, "update_claim_state"), mock.patch.object(
            claimer.time, "sleep"
        ):
            result = runner.process_slot(1)

        self.assertEqual(result.day, 3)
        self.assertTrue(any("WARNING" in message for message in logged))
        runner.mark_pending_claim.assert_called_once_with(1, 3)

    def test_hold_does_not_click_on_mismatch(self):
        for policy in ("hold",):
            with self.subTest(policy=policy):
                runner = bare_runner(slot_mismatch_policies={1: policy})
                runner.open_private_slot = mock.MagicMock()
                runner.wait_for_daily_or_world = mock.MagicMock(return_value=("daily", "screen"))
                runner.tap = mock.MagicMock()
                runner.detect_claim_day = mock.MagicMock(return_value=3)
                runner.click_claim_button = mock.MagicMock()
                with mock.patch.object(
                    claimer, "load_claim_state", return_value={"1": {"next_day": 8}}
                ), self.assertRaises(claimer.SlotHeld):
                    runner.process_slot(1)
                runner.click_claim_button.assert_not_called()
                runner.mark_pending_claim.assert_not_called()

    def test_pending_marker_is_atomic_and_preserves_previous_history(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "claim_state.json"
            state_path.write_text(
                json.dumps({"1": {"claimed_day": 4, "next_day": 5}}), encoding="utf-8"
            )
            with mock.patch.object(claimer, "CLAIM_STATE_PATH", state_path), mock.patch.object(
                claimer, "STATE_LOCK_PATH", root / ".claim_state.lock"
            ):
                claimer.mark_pending_claim(1, 5)
                state = claimer.load_claim_state()

            self.assertEqual(state["1"]["claimed_day"], 4)
            self.assertEqual(state["1"]["pending_claim"]["day"], 5)
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_pending_same_day_is_held_without_reclick(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("daily", "screen"))
        runner.tap = mock.MagicMock()
        runner.detect_claim_day = mock.MagicMock(return_value=5)
        runner.click_claim_button = mock.MagicMock()
        saved = {"1": {"next_day": 5, "pending_claim": {"day": 5}}}
        with mock.patch.object(claimer, "load_claim_state", return_value=saved), self.assertRaises(
            claimer.SlotHeld
        ):
            runner.process_slot(1)
        runner.click_claim_button.assert_not_called()

    def test_pending_claim_is_held_when_daily_is_no_longer_available(self):
        runner = bare_runner()
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("world", "screen"))
        runner.tap = mock.MagicMock()
        runner.wait_for_daily_grace = mock.MagicMock(return_value=(False, None))
        runner.return_to_main_menu = mock.MagicMock()
        pending = {"1": {"pending_claim": {"day": 5}}}
        with mock.patch.object(
            claimer, "load_claim_state", return_value=pending
        ), mock.patch.object(claimer, "update_claim_state") as update_state:
            with self.assertRaises(claimer.SlotHeld):
                runner.process_slot(1)

        update_state.assert_not_called()

    def test_audio_mute_failure_rolls_back_every_touched_session(self):
        from pycaw.pycaw import AudioUtilities

        first = mock.MagicMock()
        first.GetMute.return_value = 0
        second = mock.MagicMock()
        second.GetMute.return_value = 1
        second.SetMute.side_effect = [RuntimeError("mute failed"), None]
        sessions = [
            mock.MagicMock(ProcessId=42, SimpleAudioVolume=first),
            mock.MagicMock(ProcessId=42, SimpleAudioVolume=second),
        ]
        with mock.patch.object(AudioUtilities, "GetAllSessions", return_value=sessions), self.assertRaisesRegex(
            RuntimeError, "audio mute failed"
        ):
            claimer.mute_process_audio(42)

        self.assertEqual(first.SetMute.call_args_list, [mock.call(1, None), mock.call(0, None)])
        self.assertEqual(second.SetMute.call_args_list, [mock.call(1, None), mock.call(1, None)])

    def test_audio_restore_attempts_all_sessions_and_aggregates_errors(self):
        first = mock.MagicMock()
        second = mock.MagicMock()
        first.SetMute.side_effect = RuntimeError("first")
        second.SetMute.side_effect = RuntimeError("second")
        with self.assertRaisesRegex(RuntimeError, "2 audio session") as context:
            claimer.restore_process_audio([(first, False), (second, True)])

        self.assertIn("first", str(context.exception))
        self.assertIn("second", str(context.exception))
        first.SetMute.assert_called_once_with(0, None)
        second.SetMute.assert_called_once_with(1, None)

    def test_close_when_done_closes_before_audio_restore(self):
        events: list[str] = []
        runner = bare_runner(
            close_when_done=True,
            _audio_restore=[("session", False)],
            errors=[],
        )
        runner.close_roblox = mock.MagicMock(side_effect=lambda: events.append("close"))
        with mock.patch.object(
            claimer,
            "restore_process_audio",
            side_effect=lambda _states: events.append("restore"),
        ):
            runner.close_then_restore_audio()

        self.assertEqual(events, ["close", "restore"])
        self.assertEqual(runner._audio_restore, [])

    def test_process_slot_min_day_three_holds_day_two_without_clicking(self):
        runner = bare_runner(min_claim_day=3)
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(
            return_value=("daily", mock.MagicMock())
        )
        runner.tap = mock.MagicMock()
        runner.detect_claim_day = mock.MagicMock(return_value=2)
        runner.click_claim_button = mock.MagicMock()
        runner.return_after_recorded_result = lambda res: res

        with mock.patch.object(claimer, "load_claim_state", return_value={}), mock.patch.object(
            claimer, "update_claim_state"
        ) as update_state:
            result = runner.process_slot(2)

        self.assertEqual(result.status, "held")
        self.assertEqual(result.day, 2)
        self.assertIn("below minimum claim day 3", result.detail)
        runner.click_claim_button.assert_not_called()
        runner.mark_pending_claim.assert_not_called()
        update_state.assert_not_called()

    def test_process_slot_inspect_only_records_without_claiming(self):
        runner = bare_runner(inspect_only=True)
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(
            return_value=("daily", mock.MagicMock())
        )
        runner.tap = mock.MagicMock()
        runner.detect_claim_day = mock.MagicMock(return_value=5)
        runner.click_claim_button = mock.MagicMock()
        runner.return_after_recorded_result = lambda res: res

        result = runner.process_slot(3)
        self.assertEqual(result.status, "inspected")
        self.assertEqual(result.day, 5)
        self.assertEqual(result.reward, claimer.DAILY_REWARDS[5])
        self.assertIn("inspected without claiming", result.detail)
        runner.click_claim_button.assert_not_called()
        runner.mark_pending_claim.assert_not_called()

    def test_process_slot_inspect_only_no_daily_does_not_reconcile_pending_claim(self):
        runner = bare_runner(inspect_only=True)
        runner.open_private_slot = mock.MagicMock()
        runner.wait_for_daily_or_world = mock.MagicMock(return_value=("world", "screen"))
        runner.tap = mock.MagicMock()
        runner.wait_for_daily_grace = mock.MagicMock(return_value=(False, None))
        runner.return_after_recorded_result = lambda res: res
        pending = {"3": {"pending_claim": {"day": 5}}}

        with mock.patch.object(claimer, "load_claim_state", return_value=pending), mock.patch.object(
            claimer, "update_claim_state"
        ) as update_state:
            result = runner.process_slot(3)

        self.assertEqual(result.status, "no_daily")
        update_state.assert_not_called()

if __name__ == "__main__":
    unittest.main()
