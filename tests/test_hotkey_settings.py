from dataclasses import replace
import itertools
import json
import queue
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from xynclaim import bridge, hotkey
from xynclaim.bridge import XynClaimAPI
from xynclaim.settings import SettingsError, default_settings, load_settings, save_settings


@pytest.mark.parametrize("number", range(1, 13))
def test_function_keys_alone(number):
    assert hotkey.parse_hotkey(f"f{number}") == (f"F{number}", hotkey.MOD_NOREPEAT, 0x6F + number)


@pytest.mark.parametrize("modifiers", [permutation for size in range(1, 4)
                                     for permutation in itertools.permutations(("Ctrl", "Alt", "Shift"), size)])
@pytest.mark.parametrize("key", [*"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "F1", "F12"])
def test_all_modifier_orders_and_keys(modifiers, key):
    display, flags, virtual_key = hotkey.parse_hotkey(" + ".join((*modifiers, key)).lower())
    ordered = [name for name in hotkey.MODIFIERS if name in modifiers]
    assert display == "+".join([*ordered, key])
    assert flags == hotkey.MOD_NOREPEAT | sum(hotkey.MODIFIERS[name] for name in modifiers)
    assert virtual_key == (hotkey.FUNCTION_KEYS[key] if key.startswith("F") and len(key) > 1 else ord(key))


@pytest.mark.parametrize("key", [None, False, 6, [], {}, "A", "0", "F0", "F13", "F01", "Ctrl",
                                     "Control+X", "Win+F6", "Ctrl+", "+F6", "Alt++X", "Ctrl+Ctrl+X",
                                     "Ctrl+ALT+alt+X", "X+Ctrl", "Ctrl+A+B", "Ctrl+Space", "Ctrl+é",
                                     "Ctrl+ı", "Ctrl+ſ", "Ctrl+!", "Ctrl+NUM1", "Shift+F13"])
def test_invalid_hotkeys(key):
    with pytest.raises(ValueError, match="Hotkey must"):
        hotkey.parse_hotkey(key)


@pytest.mark.parametrize("key", ["", "  "])
def test_disabled_hotkey(key):
    assert hotkey.parse_hotkey(key) == ("", 0, 0)


def test_settings_default_old_file_and_normalized_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    assert default_settings().hotkey == "F6"
    assert load_settings(path).hotkey == "F6"
    path.write_text('{}', encoding="utf-8")
    assert load_settings(path).hotkey == "F6"
    save_settings(path, replace(default_settings(), hotkey="shift+alt+ctrl+x"))
    assert json.loads(path.read_text(encoding="utf-8"))["hotkey"] == "Ctrl+Alt+Shift+X"
    assert load_settings(path).hotkey == "Ctrl+Alt+Shift+X"
    save_settings(path, replace(default_settings(), hotkey=""))
    assert load_settings(path).hotkey == ""
    path.write_text('{"hotkey": "alt+ctrl+f12"}', encoding="utf-8")
    assert load_settings(path).hotkey == "Ctrl+Alt+F12"


@pytest.mark.parametrize("key", [None, False, [], "X", "Ctrl+Space"])
def test_settings_reject_invalid_hotkeys_without_overwriting(tmp_path, key):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hotkey": key}), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(SettingsError, match="Hotkey"):
        load_settings(path)
    with pytest.raises(ValueError, match="Hotkey"):
        save_settings(path, replace(default_settings(), hotkey=key))
    assert path.read_bytes() == original


@pytest.fixture
def win32(monkeypatch):
    messages = queue.Queue()
    calls = []
    blocked = set()
    bindings = {}
    user32 = mock.Mock()

    def register(hwnd, identifier, modifiers, key):
        calls.append(("register", threading.get_ident(), identifier, modifiers, key))
        if key in blocked:
            return 0
        bindings[identifier] = (modifiers, key)
        return 1

    def unregister(hwnd, identifier):
        calls.append(("unregister", threading.get_ident(), identifier))
        bindings.pop(identifier, None)
        return 1

    def get_message(pointer, hwnd, minimum, maximum):
        message, wparam = messages.get(timeout=5)
        pointer._obj.message = message
        pointer._obj.wParam = wparam
        return 0 if message == hotkey.WM_QUIT else -1 if message == -1 else 1

    def post(thread_id, message, wparam, lparam):
        messages.put((message, wparam))
        return 1

    user32.RegisterHotKey.side_effect = register
    user32.UnregisterHotKey.side_effect = unregister
    user32.GetMessageW.side_effect = get_message
    user32.PostThreadMessageW.side_effect = post
    kernel32 = mock.Mock()
    kernel32.GetCurrentThreadId.side_effect = threading.get_ident
    monkeypatch.setattr(hotkey.ctypes, "windll", SimpleNamespace(user32=user32, kernel32=kernel32, shell32=mock.Mock()))
    monkeypatch.setattr(bridge.XynClaimAPI, "_monitor_options", lambda self: [])
    monkeypatch.setattr(bridge.engine, "load_claim_state", lambda: {})
    monkeypatch.setattr(bridge.XynClaimAPI, "_all_history", lambda self: [])
    return SimpleNamespace(user32=user32, messages=messages, calls=calls, blocked=blocked, bindings=bindings)


@pytest.fixture
def api(tmp_path, win32):
    api = XynClaimAPI(tmp_path / "settings.json")
    yield api
    if api._hotkey_listener is not None:
        api._hotkey_listener.close()


def test_save_hotkey_registers_changes_and_disables_on_one_thread(api, win32):
    assert api.save_hotkey("f6") == {"hotkey": "F6"}
    listener = api._hotkey_listener
    assert api.get_state()["hotkey"] == "F6"
    assert api.save_hotkey("shift+alt+ctrl+9") == {"hotkey": "Ctrl+Alt+Shift+9"}
    assert api._hotkey_listener is listener
    assert load_settings(api.settings_path).hotkey == "Ctrl+Alt+Shift+9"
    assert win32.calls[1][0] == "register"
    assert win32.calls[2][0] == "unregister"
    assert all(call[1] == listener._thread.ident != threading.get_ident() for call in win32.calls)
    assert all(call[3] & hotkey.MOD_NOREPEAT for call in win32.calls if call[0] == "register")
    assert api.save_hotkey("") == {"hotkey": ""}
    assert api.get_state()["hotkey"] == ""
    assert load_settings(api.settings_path).hotkey == ""
    assert win32.bindings == {}


def test_conflict_preserves_active_hotkey_and_settings(api, win32):
    api.save_hotkey("Ctrl+X")
    original = api.settings_path.read_bytes()
    win32.blocked.add(0x75)
    with pytest.raises(RuntimeError, match="F6 is already used by another program"):
        api.save_hotkey("F6")
    assert api.get_state()["hotkey"] == "Ctrl+X"
    assert api.settings_path.read_bytes() == original
    assert len(win32.bindings) == 1


def test_initial_conflict_does_not_create_settings_and_can_recover(api, win32):
    win32.blocked.add(0x75)
    with pytest.raises(RuntimeError, match="already used"):
        api.save_hotkey("F6")
    listener = api._hotkey_listener
    assert api.get_state()["hotkey"] == ""
    assert not api.settings_path.exists()
    assert api.save_hotkey("F7") == {"hotkey": "F7"}
    assert api._hotkey_listener is listener


def test_same_binding_does_not_register_twice(api, win32):
    api.save_hotkey("F6")
    api.save_hotkey("f6")
    assert win32.user32.RegisterHotKey.call_count == 1


def test_invalid_save_does_not_start_listener_or_save(api, win32):
    with pytest.raises(ValueError, match="Hotkey"):
        api.save_hotkey("Win+X")
    assert api._hotkey_listener is None
    assert not api.settings_path.exists()
    win32.user32.RegisterHotKey.assert_not_called()


def test_settings_write_failure_restores_old_binding(api, win32, monkeypatch):
    api.save_hotkey("F6")
    original = api.settings_path.read_bytes()
    monkeypatch.setattr(bridge, "save_settings", mock.Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        api.save_hotkey("Alt+Q")
    assert api.get_state()["hotkey"] == "F6"
    assert api.settings_path.read_bytes() == original
    assert list(win32.bindings.values()) == [(hotkey.MOD_NOREPEAT, 0x75)]


def test_message_post_failure_preserves_binding_and_can_retry(api, win32):
    api.save_hotkey("F6")
    with mock.patch.object(win32.user32, "PostThreadMessageW", return_value=0):
        with pytest.raises(RuntimeError, match="Couldn't update"):
            api.save_hotkey("F7")
    assert api.get_state()["hotkey"] == "F6"
    assert load_settings(api.settings_path).hotkey == "F6"
    assert api.save_hotkey("F8") == {"hotkey": "F8"}


def test_unregister_failure_preserves_old_binding(api, win32):
    api.save_hotkey("F6")
    unregister = win32.user32.UnregisterHotKey.side_effect
    win32.user32.UnregisterHotKey.side_effect = lambda hwnd, identifier: 0 if identifier == 1 else unregister(hwnd, identifier)
    with pytest.raises(RuntimeError, match="already used"):
        api.save_hotkey("F7")
    assert api.get_state()["hotkey"] == "F6"
    assert len(win32.bindings) == 1
    with pytest.raises(RuntimeError, match="Couldn't turn off"):
        api.save_hotkey("")
    assert load_settings(api.settings_path).hotkey == "F6"
    win32.user32.UnregisterHotKey.side_effect = unregister


def test_listener_ignores_stale_hotkey_and_survives_callback_failure(win32):
    called = threading.Event()
    callback = mock.Mock(side_effect=[RuntimeError("toggle failed"), lambda: None])
    listener = hotkey.HotkeyListener(callback)
    try:
        assert listener.change("F6")
        win32.messages.put((hotkey.WM_HOTKEY, 2))
        win32.messages.put((hotkey.WM_HOTKEY, 1))
        listener.change("F6")  # Acknowledges all earlier messages.
        assert callback.call_count == 1
        callback.side_effect = lambda: called.set()
        win32.messages.put((hotkey.WM_HOTKEY, 1))
        assert called.wait(2)
        assert listener.change("F7")
    finally:
        listener.close()
    assert listener.label == ""
    assert win32.bindings == {}
    with pytest.raises(RuntimeError, match="unavailable"):
        listener.change("F6")


def test_message_loop_failure_clears_active_display(api, win32):
    api.save_hotkey("F6")
    win32.messages.put((-1, 0))
    api._hotkey_listener._thread.join(2)
    assert not api._hotkey_listener._thread.is_alive()
    assert api.get_state()["hotkey"] == ""
    assert win32.bindings == {}


def test_disabled_listener_does_not_dispatch_hotkey_zero(win32):
    callback = mock.Mock()
    listener = hotkey.HotkeyListener(callback)
    try:
        win32.messages.put((hotkey.WM_HOTKEY, 0))
        assert listener.change("")
        callback.assert_not_called()
    finally:
        listener.close()


def test_simultaneous_hotkey_saves_keep_registration_and_settings_in_sync(api, win32):
    errors = []

    def save(key):
        try:
            api.save_hotkey(key)
        except Exception as error:
            errors.append(error)

    workers = [threading.Thread(target=save, args=(key,)) for key in ("F6", "F7", "Alt+X")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    assert not errors
    assert api.get_state()["hotkey"] == load_settings(api.settings_path).hotkey
    assert len(win32.bindings) == 1
    assert len({call[1] for call in win32.calls}) == 1


def test_listener_hotkey_stops_scheduled_wait(api, win32):
    api.save_hotkey("F6")
    api._scheduled_waiting = True
    win32.messages.put((hotkey.WM_HOTKEY, api._hotkey_listener._hotkey_id))
    api._hotkey_listener.change("F6")
    assert api._stop_event.is_set()
    assert api.get_state()["hotkey_start_requested"] is False


def test_missing_ui_does_not_leave_hotkey_registered(tmp_path, win32, monkeypatch):
    monkeypatch.setattr(bridge, "APP_DIR", tmp_path)
    with mock.patch.dict("sys.modules", {"webview": mock.Mock()}):
        with pytest.raises(FileNotFoundError, match="Missing bundled UI"):
            bridge.launch_webview()
    win32.user32.RegisterHotKey.assert_not_called()


def test_webview_failure_closes_hotkey_thread(tmp_path, win32, monkeypatch):
    api = XynClaimAPI(tmp_path / "settings.json")
    webview = mock.MagicMock()
    webview.screens = []
    webview.start.side_effect = RuntimeError("webview failed")
    monkeypatch.setattr(bridge, "XynClaimAPI", lambda: api)
    monkeypatch.setattr(bridge, "_saved_window_position", lambda screens: None)
    with mock.patch.dict("sys.modules", {"webview": webview}):
        with pytest.raises(RuntimeError, match="webview failed"):
            bridge.launch_webview()
    assert not api._hotkey_listener._thread.is_alive()
    assert win32.bindings == {}


@pytest.mark.parametrize("key,blocked,active", [("Shift+F8", False, "Shift+F8"), ("", False, ""), ("F6", True, "")])
def test_launch_registers_saved_hotkey_and_exposes_new_api(tmp_path, win32, monkeypatch, key, blocked, active):
    api = XynClaimAPI(tmp_path / "settings.json")
    save_settings(api.settings_path, replace(default_settings(), hotkey=key))
    original = api.settings_path.read_bytes()
    if blocked:
        win32.blocked.add(0x75)
    webview = mock.MagicMock()
    webview.screens = []
    monkeypatch.setattr(bridge, "XynClaimAPI", lambda: api)
    monkeypatch.setattr(bridge, "_saved_window_position", lambda screens: None)
    states = []
    webview.start.side_effect = lambda *args, **kwargs: states.append(api.get_state()["hotkey"])
    with mock.patch.dict("sys.modules", {"webview": webview}):
        bridge.launch_webview()
    assert states == [active]
    assert api.settings_path.read_bytes() == original
    exposed = webview.create_window.return_value.expose.call_args.args
    assert api.save_hotkey in exposed
    assert api.get_slot_stats in exposed
    assert api.export_stats_csv in exposed
    assert not api._hotkey_listener._thread.is_alive()
