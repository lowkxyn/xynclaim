import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from xynclaim import engine
from xynclaim import bridge


def display(device, left, top, width, height, primary):
    return engine.DisplayInfo(device, left, top, width, height, left, top, width, height - 40, primary)


PRIMARY = display("DISPLAY1", 0, 0, 2560, 1600, True)
SECOND = display("DISPLAY2", -1920, 183, 1920, 1080, False)


class FakeUser32:
    def __init__(self, rect, zoomed=False):
        self.rect = rect
        self.zoomed = zoomed
        self.calls = []

    def FindWindowW(self, _cls, _title):
        return 77

    def GetWindowThreadProcessId(self, _hwnd, pid_ref):
        pid_ref._obj.value = os.getpid()

    def GetWindowRect(self, _hwnd, rect_ref):
        rect = rect_ref._obj
        rect.left, rect.top, rect.right, rect.bottom = self.rect

    def IsZoomed(self, _hwnd):
        return self.zoomed

    def ShowWindow(self, hwnd, command):
        self.calls.append(("show", hwnd, command))

    def SetWindowPos(self, hwnd, _after, x, y, width, height, _flags):
        self.calls.append(("move", x, y, width, height))


def run_clear(monkeypatch, user32, displays, roblox=None, run_display="primary"):
    monkeypatch.setattr(bridge.ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    monkeypatch.setattr(engine, "list_displays", lambda: displays)
    monkeypatch.setattr(engine, "find_roblox_window", lambda: roblox)
    api = bridge.XynClaimAPI(Path(tempfile.mkdtemp()) / "settings.json")
    return api._clear_run_display(run_display)


def test_window_over_run_display_moves_to_other_display(monkeypatch):
    user32 = FakeUser32((100, 80, 1364, 901))
    assert run_clear(monkeypatch, user32, [PRIMARY, SECOND]) is None
    kind, x, y, width, height = user32.calls[-1]
    assert kind == "move" and (width, height) == (1264, 821)
    assert SECOND.left <= x and x + width <= SECOND.left + SECOND.width


def test_single_display_minimizes_and_returns_handle(monkeypatch):
    user32 = FakeUser32((100, 80, 1364, 901))
    assert run_clear(monkeypatch, user32, [PRIMARY]) == 77
    assert user32.calls == [("show", 77, bridge.SW_MINIMIZE)]


def test_window_already_elsewhere_is_left_alone(monkeypatch):
    user32 = FakeUser32((-1900, 190, -636, 1011))
    assert run_clear(monkeypatch, user32, [PRIMARY, SECOND]) is None
    assert user32.calls == []


def test_current_display_follows_the_open_roblox_window(monkeypatch):
    user32 = FakeUser32((-1900, 190, -636, 1011))
    monkeypatch.setattr(engine, "_display_for_window", lambda _hwnd: SECOND)
    run_clear(monkeypatch, user32, [PRIMARY, SECOND], roblox=SimpleNamespace(hwnd=5), run_display="current")
    kind, x, _y, width, _height = user32.calls[-1]
    assert kind == "move" and PRIMARY.left <= x and x + width <= PRIMARY.width


def test_maximized_window_is_restored_before_moving(monkeypatch):
    user32 = FakeUser32((0, 0, 2560, 1560), zoomed=True)
    run_clear(monkeypatch, user32, [PRIMARY, SECOND])
    assert user32.calls[0] == ("show", 77, bridge.SW_RESTORE)
    assert user32.calls[-1][3] <= SECOND.work_width
