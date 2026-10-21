from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from xynclaim import bridge, window_icon


@pytest.fixture
def winapi(monkeypatch):
    api = SimpleNamespace(shell32=mock.Mock(), user32=mock.Mock(), dwmapi=mock.Mock())
    monkeypatch.setattr(window_icon.ctypes, "windll", api)
    return api


def test_app_identity(winapi):
    window_icon.set_app_identity()
    winapi.shell32.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with("Xyn.XynClaim")


def test_source_and_frozen_icon_paths(monkeypatch, tmp_path):
    monkeypatch.delattr(window_icon.sys, "_MEIPASS", raising=False)
    assert window_icon.icon_path() == Path(window_icon.__file__).resolve().parent.parent / "packaging/xynclaim.ico"
    monkeypatch.setattr(window_icon.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert window_icon.icon_path() == tmp_path / "packaging/xynclaim.ico"
    spec = Path("packaging/XynClaim.spec").read_text()
    assert '(str(ROOT / "packaging" / "xynclaim.ico"), "packaging")' in spec


def test_sets_big_and_small_icons_with_pointer_sized_handles(winapi, monkeypatch, tmp_path):
    path = tmp_path / "icon.ico"
    path.touch()
    monkeypatch.setattr(window_icon, "icon_path", lambda: path)
    winapi.user32.GetSystemMetrics.side_effect = [32, 16]
    handles = [0x1234567800, 0x1234567900]
    winapi.user32.LoadImageW.side_effect = handles
    hwnd = 0x1234567000
    assert window_icon.set_window_icons(hwnd) == handles
    assert winapi.user32.LoadImageW.call_args_list == [
        mock.call(None, str(path), 1, 32, 32, 0x10), mock.call(None, str(path), 1, 16, 16, 0x10)]
    assert winapi.user32.SendMessageW.call_args_list == [
        mock.call(hwnd, 0x80, 1, handles[0]), mock.call(hwnd, 0x80, 0, handles[1])]
    winapi.user32.DestroyIcon.assert_not_called()
    window_icon.release_icons(handles)
    assert winapi.user32.DestroyIcon.call_args_list == [mock.call(handle) for handle in handles]


@pytest.mark.parametrize("missing,hwnd", [(True, 123), (False, 0)])
def test_missing_icon_or_window_does_nothing(winapi, monkeypatch, tmp_path, missing, hwnd):
    path = tmp_path / "icon.ico"
    if not missing:
        path.touch()
    monkeypatch.setattr(window_icon, "icon_path", lambda: path)
    assert window_icon.set_window_icons(hwnd) == []
    winapi.user32.LoadImageW.assert_not_called()
    winapi.user32.SendMessageW.assert_not_called()


def test_failed_load_is_not_sent_or_released(winapi):
    winapi.user32.LoadImageW.side_effect = [0, 456]
    assert window_icon.set_window_icons(123) == [456]
    winapi.user32.SendMessageW.assert_called_once_with(123, 0x80, 0, 456)


def test_launch_sets_identity_before_creation_and_icons_after_shown(tmp_path, monkeypatch):
    api = bridge.XynClaimAPI(tmp_path / "settings.json")
    webview = mock.MagicMock(screens=[])
    window = webview.create_window.return_value
    window.native.Handle.ToInt64.return_value = 123
    shown_event = window.events.shown
    calls = []
    monkeypatch.setattr(bridge, "XynClaimAPI", lambda: api)
    monkeypatch.setattr(bridge, "_saved_window_position", lambda _: None)
    monkeypatch.setattr(api, "_register_hotkey", mock.Mock())
    monkeypatch.setattr(window_icon, "set_app_identity", lambda: calls.append("identity"))
    monkeypatch.setattr(window_icon, "set_window_icons", lambda hwnd: calls.append(("icons", hwnd)) or [456])
    monkeypatch.setattr(window_icon, "release_icons", lambda icons: calls.append(("release", list(icons))))
    webview.create_window.side_effect = lambda *a, **kw: calls.append("create") or window

    def start(*args, **kwargs):
        shown = shown_event.__iadd__.call_args.args[0]
        shown()
        calls.append("closed")

    webview.start.side_effect = start
    with mock.patch.dict("sys.modules", {"webview": webview}):
        bridge.launch_webview()
    assert calls == ["identity", "create", ("icons", 123), "closed", ("release", [456])]


def test_round_corners_asks_for_rounded_window(winapi):
    window_icon.round_corners(1234)
    args = winapi.dwmapi.DwmSetWindowAttribute.call_args.args
    assert args[0].value == 1234 and args[1] == 33 and args[3] == 4
    assert args[2]._obj.value == 2


def test_round_corners_skips_missing_window(winapi):
    window_icon.round_corners(0)
    winapi.dwmapi.DwmSetWindowAttribute.assert_not_called()
