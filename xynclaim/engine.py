from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import msvcrt
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2
import mss
import numpy as np
import pydirectinput

from xynclaim import APP_VERSION, ocr


APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
REFERENCE_DIR = APP_DIR / "assets" / "reference"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "XynClaim"
REPORT_DIR = DATA_DIR / "reports"
LIVE_LOG_PATH = REPORT_DIR / "live_run.log"
HISTORY_PATH = REPORT_DIR / "history.jsonl"
CLAIM_STATE_PATH = DATA_DIR / "claim_state.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
WINDOW_STATE_PATH = DATA_DIR / "window.json"
ROBLOX_SETTINGS_PATH = Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "Roblox" / "GlobalBasicSettings_13.xml"
GRAPHICS_RESTORE_PATH = DATA_DIR / "graphics_restore.json"
GRAPHICS_BARS = 10
STATE_LOCK_PATH = DATA_DIR / ".claim_state.lock"
HISTORY_LOCK_PATH = REPORT_DIR / ".history.lock"
MIGRATION_LOCK_PATH = DATA_DIR / ".migration.lock"
LEGACY_REPORT_DIR = APP_DIR / "reports"
LEGACY_CLAIM_STATE_PATH = APP_DIR / "claim_state.json"
GAME_URI = "roblox://placeId=4195834406"
APP_NAME = "XynClaim"
SCREEN_WIDTH = 1920
SCREEN_HEIGHT = 1080
MAX_DIAGNOSTICS = 10
MAX_RECEIPTS = 30
# The "Daily reward" toast sits top-centre, above the still-open daily panel.
RECEIPT_REGION = (55, 215, 640, 1290)  # top, bottom, left, right
# Daily panel (post-October 2026 UI): the button reads "Claim Day N" and turns to
# "Claimed"; the footer reads "Streak N / 14". Back closes the panel.
CLAIM_BUTTON = (960, 738)
CLAIM_BUTTON_TEXT_REGION = (712, 765, 845, 1075)  # top, bottom, left, right
STREAK_TEXT_REGION = (720, 760, 420, 560)
# Right end of Back: Roblox's chat window can cover the middle (its Friends tab).
DAILY_BACK_BUTTON = (500, 820)
PLAY_BUTTON = (956, 886)
DESTINATION_NORTH_EARTH = (940, 270)
PRIVATE_CODE_FIELD = (633, 540)
MENU_TRAVEL_TAB = (34, 598)
TRAVEL_MAIN_MENU = (146, 484)
MAX_RUN_REPORTS = 50
MAX_HISTORY_BYTES = 5 * 1024 * 1024

POLL_INTERVAL = 0.12
# The new title sequence (loading art, then "Press Any Button") can take ~2.5 min.
MAIN_MENU_TIMEOUT = 180.0
# How long a menu transition may show neither screen after a click landed.
TRANSITION_GRACE_SECONDS = 10.0
# How long a source screen may take to finish fading in before the first click.
SOURCE_SETTLE_SECONDS = 2.0
WORLD_TIMEOUT = 90.0
DAILY_GRACE_SECONDS = 10.0
DAILY_SETTLE_STABLE_SAMPLES = 3
DAILY_SETTLE_MIN_DWELL = 0.6


def _configure_winapi() -> None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        # Per-monitor DPI awareness keeps client geometry, capture, and SendInput
        # in the same physical-pixel coordinate space.
        dpi_result = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        if dpi_result not in (0, -2147024891):  # S_OK or already configured
            raise RuntimeError(f"Windows rejected per-monitor DPI awareness ({dpi_result})")
    except AttributeError:
        if not user32.SetProcessDPIAware():
            raise RuntimeError("Windows couldn't enable DPI-aware coordinates")
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    kernel32.GetPriorityClass.restype = wintypes.DWORD
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]


_configure_winapi()


def migrate_legacy_data() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(MIGRATION_LOCK_PATH):
        if not CLAIM_STATE_PATH.exists() and LEGACY_CLAIM_STATE_PATH.exists():
            temporary = CLAIM_STATE_PATH.with_name(f".{CLAIM_STATE_PATH.name}.migration.tmp")
            shutil.copy2(LEGACY_CLAIM_STATE_PATH, temporary)
            os.replace(temporary, CLAIM_STATE_PATH)
        if not REPORT_DIR.exists() and LEGACY_REPORT_DIR.exists():
            shutil.copytree(LEGACY_REPORT_DIR, REPORT_DIR)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)

DAILY_REWARDS = {
    1: "10 min 2× XP",
    2: "Zeni (Level × 25)",
    3: "Random Dragon Ball (+100 MP if all held)",
    4: "30 min 2× XP",
    5: "Zeni (Level × 50)",
    6: "10 random consumables",
    7: "+200 Mastery Points",
    8: "Zeni (Level × 75)",
    9: "10 random consumables",
    10: "60 min 2× Zeni",
    11: "Zeni (Level × 100)",
    12: "60 min 2× Drops",
    13: "Random Dragon Ball (+100 MP if all held)",
    # If all four Add-On Packs are already owned this becomes 1h of 2x Zeni,
    # 2x Drops and 2x XP instead (per the 2026-01-24 patch note).
    14: "Random Add-On Pack gamepass",
}


@dataclass(frozen=True)
class AnchorSpec:
    reference: str
    template_rect: tuple[int, int, int, int]
    search_rect: tuple[int, int, int, int]
    threshold: float


ANCHORS = {
    # References were re-captured after DBOG's October 2026 UI overhaul. Each
    # template is a static header/label, never a hover-highlighted button.
    "main_menu": AnchorSpec(
        # Training Mode + Options rows; Play turns green under the cursor.
        "main_menu.png", (700, 925, 1220, 1032), (650, 890, 1270, 1065), 0.60
    ),
    "slot_select": AnchorSpec(
        "slot_select.png", (790, 180, 1160, 230), (740, 150, 1210, 260), 0.60
    ),
    # "Choose Your Destination..." list shown after a slot is picked.
    "server_menu": AnchorSpec(
        "server_menu.png", (595, 80, 1330, 160), (560, 50, 1360, 190), 0.60
    ),
    "private_panel": AnchorSpec(
        # "Join a friend." header: the private-server card grows on hover, this one doesn't.
        "private_panel.png", (1088, 335, 1488, 400), (1050, 300, 1530, 430), 0.60
    ),
    "daily_rewards": AnchorSpec(
        "daily_rewards.png", (700, 245, 1220, 295), (650, 215, 1270, 330), 0.60
    ),
    "restore_streak": AnchorSpec(
        "restore_streak.png", (1297, 247, 1494, 292), (1270, 225, 1520, 315), 0.80
    ),
    "global_reward_locked": AnchorSpec(
        "global_reward_locked.png", (790, 120, 1200, 178), (700, 90, 1280, 215), 0.70
    ),
    # Side MENU tab while the menu is open (it slides right of the panel).
    "game_menu": AnchorSpec(
        "travel_menu.png", (536, 515, 580, 675), (515, 495, 600, 695), 0.60
    ),
    "travel_menu": AnchorSpec(
        "travel_menu.png", (70, 395, 300, 430), (50, 380, 330, 445), 0.70
    ),
    # HP/KI/FR labels and the left ends of the bars; the username row is excluded.
    "world": AnchorSpec(
        "world.png", (158, 125, 330, 205), (130, 95, 360, 235), 0.55
    ),
}


class RunStopped(Exception):
    pass


class FocusLost(Exception):
    """Roblox stopped being the foreground window mid-run. Every click and key
    this tool sends goes to whatever is focused, so the run has to stop rather
    than type into whatever stole focus."""


class JoinCodeRejected(RuntimeError):
    """DBOG kept the private-network panel open after a join request."""


class SlotUnavailable(RuntimeError):
    """A selected slot is outside the user's configured slot count."""


class SlotHeld(RuntimeError):
    """A slot was intentionally left unclaimed by its slot rule."""

    def __init__(self, message: str, day: int | None = None) -> None:
        super().__init__(message)
        self.day = day


class UnsafeGameState(RuntimeError):
    """The current screen wasn't a recognized safe source or destination."""


def normalize_server_code(value: str) -> str:
    """Validate without assuming DBOG's undocumented code alphabet."""
    code = value.strip()
    if not code:
        raise ValueError("Enter the shared server code before starting")
    if len(code) > 64:
        raise ValueError("Shared server codes can't exceed 64 characters")
    if not code.isascii() or any(character.isspace() for character in code):
        raise ValueError("Shared server codes must use visible ASCII characters without spaces")
    if not all(33 <= ord(character) <= 126 for character in code):
        raise ValueError("Shared server code contains unsupported characters")
    return code


def normalize_profile_name(value: str, access_mode: str, server_code: str = "") -> str:
    default = "Shared private server" if access_mode == "shared" else "My private server"
    name = value.strip() or default
    if len(name) > 40 or any(not character.isprintable() for character in name):
        raise ValueError(
            "Server label must be valid one-line text and no more than 40 characters"
        )
    if server_code and server_code.casefold() in name.casefold():
        raise ValueError("Server label can't contain the private server code")
    return name


def redact_secret(value: str, secret: str) -> str:
    return value.replace(secret, "[REDACTED]") if secret else value


def prune_report_files(pattern: str, keep: int) -> None:
    files = sorted(
        REPORT_DIR.glob(pattern), key=lambda path: path.stat().st_mtime_ns, reverse=True
    )
    for path in files[keep:]:
        path.unlink(missing_ok=True)


def trim_history_file() -> None:
    if not HISTORY_PATH.exists() or HISTORY_PATH.stat().st_size <= MAX_HISTORY_BYTES:
        return
    with HISTORY_PATH.open("rb") as history:
        history.seek(-MAX_HISTORY_BYTES, os.SEEK_END)
        tail = history.read()
    newline = tail.find(b"\n")
    if newline >= 0:
        tail = tail[newline + 1 :]
    temporary = HISTORY_PATH.with_name(f".{HISTORY_PATH.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(tail)
        os.replace(temporary, HISTORY_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a text file only after its complete contents reach disk."""
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def find_roblox_executable() -> str | None:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Classes\roblox\shell\open\command"
        ) as key:
            val, _ = winreg.QueryValueEx(key, "")
            val = val.strip()
            if val.startswith('"'):
                end = val.find('"', 1)
                if end != -1:
                    return val[1:end]
            return val.split()[0]
    except Exception:
        pass
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        candidates = list(
            Path(local_appdata).glob("Roblox/Versions/*/RobloxPlayerBeta.exe")
        )
        if candidates:
            return str(candidates[-1])
    return None


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_char_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def launch_roblox_on_default_desktop(game_uri: str = GAME_URI) -> bool:
    ensure_default_desktop()
    exe = find_roblox_executable()
    if exe and os.path.exists(exe):
        cmd = f'"{exe}" {game_uri}'
        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(_STARTUPINFOW)
        si.lpDesktop = "WinSta0\\Default"
        pi = _PROCESS_INFORMATION()
        res = ctypes.windll.kernel32.CreateProcessW(
            None,
            cmd,
            None,
            None,
            False,
            0,
            None,
            None,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if res:
            ctypes.windll.kernel32.CloseHandle(pi.hThread)
            ctypes.windll.kernel32.CloseHandle(pi.hProcess)
            return True
    try:
        os.startfile(game_uri)
        return True
    except Exception:
        return False


ROBLOX_PROCESS = "robloxplayerbeta.exe"


@dataclass(frozen=True)
class RobloxGeometry:
    hwnd: int
    left: int
    top: int
    width: int
    height: int


def _client_geometry(hwnd: int) -> RobloxGeometry | None:
    client = wintypes.RECT()
    origin = wintypes.POINT(0, 0)
    user32 = ctypes.windll.user32
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    width = client.right - client.left
    height = client.bottom - client.top
    if width < 640 or height < 480:
        return None
    return RobloxGeometry(hwnd, origin.x, origin.y, width, height)


def wait_for_stable_geometry(
    hwnd: int, timeout: float = 30.0, stable_for: float = 2.0,
    cancel_check: Callable[[], None] | None = None,
) -> RobloxGeometry:
    """Wait through Roblox's launch window replacement and resize animation."""
    deadline = time.monotonic() + timeout
    last_bounds: tuple[int, int, int, int, int] | None = None
    stable_since = time.monotonic()
    latest: RobloxGeometry | None = None
    while time.monotonic() < deadline:
        if cancel_check is not None:
            cancel_check()
        latest = _client_geometry(hwnd)
        if latest is None:
            replacement = find_roblox_window()
            if replacement is not None:
                hwnd = replacement.hwnd
                latest = replacement
            else:
                last_bounds = None
                stable_since = time.monotonic()
                time.sleep(0.2)
                continue
        bounds = (latest.hwnd, latest.left, latest.top, latest.width, latest.height)
        if bounds != last_bounds:
            last_bounds = bounds
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= stable_for:
            return latest
        time.sleep(0.2)
    if latest is None:
        raise RuntimeError("Roblox client area became unavailable")
    raise RuntimeError("Roblox window size didn't stabilize after launch")


def _process_image_name(hwnd: int) -> str:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    handle = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        return ""
    try:
        name = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, name, ctypes.byref(size)
        ):
            return ""
        return name.value
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def foreground_process_name() -> str:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return os.path.basename(_process_image_name(hwnd)) if hwnd else ""


def process_name_at_screen_point(x: int, y: int) -> str:
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    return os.path.basename(_process_image_name(hwnd)) if hwnd else ""


def roblox_focused() -> bool:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return False
    if os.path.basename(_process_image_name(hwnd)).lower() != ROBLOX_PROCESS:
        return False
    target_pid = os.environ.get("XYNCLAIM_ROBLOX_PID")
    if target_pid and target_pid.isdigit():
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) == int(target_pid)
    target_hwnd = os.environ.get("XYNCLAIM_ROBLOX_HWND")
    if target_hwnd and target_hwnd.isdigit():
        return int(hwnd) == int(target_hwnd)
    return True


def _process_age_seconds(process_id: int) -> float | None:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, process_id)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        now = wintypes.FILETIME()
        kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))
    finally:
        kernel32.CloseHandle(handle)

    def ticks(filetime: wintypes.FILETIME) -> int:
        return (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime

    return (ticks(now) - ticks(created)) / 10_000_000


def _roblox_pids_with_visible_windows() -> set[int]:
    user32 = ctypes.windll.user32
    pids: set[int] = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd) and os.path.basename(_process_image_name(hwnd)).lower() == ROBLOX_PROCESS:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(int(pid.value))
        return True

    user32.EnumWindows(visit, 0)
    return pids


def stuck_roblox_pids(min_age_seconds: float = 20.0) -> list[int]:
    """Roblox processes with no visible window that are old enough to have drawn one.

    A Roblox someone is playing on (even minimized) or one that's still loading is
    left alone, so a second account on the same PC never gets closed.
    """
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq RobloxPlayerBeta.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    pids = {
        int(match.group(1))
        for match in re.finditer(r'^"RobloxPlayerBeta\.exe","(\d+)"', result.stdout, re.IGNORECASE | re.MULTILINE)
    }
    windowed = _roblox_pids_with_visible_windows()
    return sorted(
        pid
        for pid in pids - windowed
        if (age := _process_age_seconds(pid)) is not None and age >= min_age_seconds
    )


def find_roblox_window(target_pid: int | None = None, target_hwnd: int | None = None) -> RobloxGeometry | None:
    ensure_default_desktop()
    if target_pid is None:
        env_pid = os.environ.get("XYNCLAIM_ROBLOX_PID")
        if env_pid and env_pid.isdigit():
            target_pid = int(env_pid)
    if target_hwnd is None:
        env_hwnd = os.environ.get("XYNCLAIM_ROBLOX_HWND")
        if env_hwnd and env_hwnd.isdigit():
            target_hwnd = int(env_hwnd)

    candidates: list[RobloxGeometry] = []
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if target_hwnd is not None and int(hwnd) != int(target_hwnd):
            return True
        if os.path.basename(_process_image_name(hwnd)).lower() != ROBLOX_PROCESS:
            return True
        if target_pid is not None:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) != int(target_pid):
                return True
        geometry = _client_geometry(hwnd)
        if geometry is not None:
            candidates.append(geometry)
        return True

    user32.EnumWindows(visit, 0)
    if len(candidates) > 1:
        if target_pid is not None or target_hwnd is not None:
            candidates.sort(key=lambda g: g.width * g.height, reverse=True)
            return candidates[0]
        raise RuntimeError(
            "Multiple usable Roblox windows are open; close all but the DBOG client"
        )
    return candidates[0] if candidates else None


def ensure_default_desktop() -> None:
    try:
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        pass


def focus_roblox(hwnd: int | None = None) -> bool:
    """Bring Roblox to the front. Attaching to both the current foreground thread
    and Roblox's thread allows Windows to reliably grant foreground activation."""
    ensure_default_desktop()
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    geometry = _client_geometry(hwnd) if hwnd else find_roblox_window()
    if geometry is None:
        return False

    hwnd = geometry.hwnd
    fore_hwnd = user32.GetForegroundWindow()
    if fore_hwnd == hwnd:
        return True

    user32.AllowSetForegroundWindow(0xFFFFFFFF)
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    fore_thread = user32.GetWindowThreadProcessId(fore_hwnd, None) if fore_hwnd else 0
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    this_thread = kernel32.GetCurrentThreadId()

    if fore_thread and fore_thread != this_thread:
        user32.AttachThreadInput(this_thread, fore_thread, True)
    if target_thread and target_thread != this_thread:
        user32.AttachThreadInput(this_thread, target_thread, True)
    try:
        user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU (Alt) down
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)  # VK_MENU (Alt) up
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if fore_thread and fore_thread != this_thread:
            user32.AttachThreadInput(this_thread, fore_thread, False)
        if target_thread and target_thread != this_thread:
            user32.AttachThreadInput(this_thread, target_thread, False)
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return user32.GetForegroundWindow() == hwnd


@dataclass
class SlotResult:
    slot: int
    status: str
    day: int | None
    seconds: float
    reward: str | None = None
    next_day: int | None = None
    next_reward: str | None = None
    received_reward: str | None = None
    verification: str = "not_applicable"
    receipt_image: str | None = None
    server_route: str = "private"
    detail: str = ""


class PostClaimNavigationFailed(RuntimeError):
    """Preserve a completed result when later work fails."""

    def __init__(
        self, result: SlotResult, error: Exception | KeyboardInterrupt, phase: str = "navigation"
    ) -> None:
        super().__init__(f"Post-result {phase} failed: {type(error).__name__}: {error}")
        self.result = result


@dataclass
class RunReport:
    app_version: str
    started_utc: str
    finished_utc: str
    total_seconds: float
    access_mode: str
    server_profile: str
    configured_slot_count: int
    slots: list[SlotResult]
    errors: list[str]


class VisualDetector:
    def __init__(self) -> None:
        missing = [spec.reference for spec in ANCHORS.values() if not (REFERENCE_DIR / spec.reference).exists()]
        if missing:
            raise FileNotFoundError(f"Missing screen references: {', '.join(sorted(set(missing)))}")

        self.templates: dict[str, np.ndarray] = {}
        for name, spec in ANCHORS.items():
            reference = cv2.imread(str(REFERENCE_DIR / spec.reference), cv2.IMREAD_GRAYSCALE)
            if reference is None:
                raise RuntimeError(f"Couldn't read {spec.reference}")
            x1, y1, x2, y2 = spec.template_rect
            self.templates[name] = reference[y1:y2, x1:x2]

    def score(self, screen: np.ndarray, name: str) -> float:
        spec = ANCHORS[name]
        x1, y1, x2, y2 = spec.search_rect
        search = cv2.cvtColor(screen[y1:y2, x1:x2], cv2.COLOR_BGRA2GRAY)
        template = self.templates[name]
        if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
            return 0.0
        result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        return float(cv2.minMaxLoc(result)[1])

    def matches(self, screen: np.ndarray, name: str) -> tuple[bool, float]:
        score = self.score(screen, name)
        return score >= ANCHORS[name].threshold, score


class ScreenCapture:
    def __init__(self, geometry: RobloxGeometry) -> None:
        # mss exposes the capture class as `MSS` in some releases and `mss`
        # in others (e.g. some Python 3.14 environments).
        capture_type = getattr(mss, "MSS", None) or getattr(mss, "mss", None)
        if capture_type is None:
            raise RuntimeError("Installed mss package has no supported capture class")
        self._capture = capture_type()
        if geometry.width < 640 or geometry.height < 480:
            raise RuntimeError(
                f"Roblox client area is too small: {geometry.width}x{geometry.height}"
            )
        self.monitor = {
            "left": geometry.left,
            "top": geometry.top,
            "width": geometry.width,
            "height": geometry.height,
        }

    def grab(self) -> np.ndarray:
        screen = np.asarray(self._capture.grab(self.monitor))
        if screen.shape[1] != SCREEN_WIDTH or screen.shape[0] != SCREEN_HEIGHT:
            raise RuntimeError(
                f"Roblox capture must remain {SCREEN_WIDTH}x{SCREEN_HEIGHT}; got "
                f"{screen.shape[1]}x{screen.shape[0]}"
            )
        return screen

    def close(self) -> None:
        self._capture.close()


class DirectInput:
    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004

    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong),
        ]

    class KeyboardInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong),
        ]

    class InputUnion(ctypes.Union):
        pass

    class Input(ctypes.Structure):
        pass

    InputUnion._fields_ = [("mi", MouseInput), ("ki", KeyboardInput)]
    Input._anonymous_ = ("union",)
    Input._fields_ = [("type", ctypes.c_ulong), ("union", InputUnion)]

    # Bumped by the runner after each focus pause.
    pauses = 0

    def __init__(self, geometry: RobloxGeometry | None = None) -> None:
        pydirectinput.PAUSE = 0.1
        self.user32 = ctypes.windll.user32
        self.geometry = geometry

    def set_geometry(self, geometry: RobloxGeometry) -> None:
        self.geometry = geometry

    def _absolute_point(self, x: int, y: int) -> tuple[int, int]:
        if self.geometry is None:
            raise RuntimeError("Roblox window geometry is unavailable")
        absolute_x = self.geometry.left + round(x * self.geometry.width / SCREEN_WIDTH)
        absolute_y = self.geometry.top + round(y * self.geometry.height / SCREEN_HEIGHT)
        return absolute_x, absolute_y

    def _mouse_event(self, x: int, y: int, flags: int) -> Input:
        absolute_x, absolute_y = self._absolute_point(x, y)
        virtual_left = self.user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        virtual_top = self.user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        virtual_width = self.user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        virtual_height = self.user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        normalized_x = round((absolute_x - virtual_left) * 65535 / max(1, virtual_width - 1))
        normalized_y = round((absolute_y - virtual_top) * 65535 / max(1, virtual_height - 1))
        return self.Input(
            type=self.INPUT_MOUSE,
            mi=self.MouseInput(
                normalized_x,
                normalized_y,
                0,
                flags | self.MOUSEEVENTF_VIRTUALDESK,
                0,
                0,
            ),
        )

    def _plain_mouse_event(self, flags: int, dx: int = 0, dy: int = 0) -> Input:
        return self.Input(
            type=self.INPUT_MOUSE,
            mi=self.MouseInput(dx, dy, 0, flags, 0, 0),
        )

    def _send_mouse_events(self, events: ctypes.Array[Input]) -> None:
        expected = len(events)
        sent = self.user32.SendInput(expected, events, ctypes.sizeof(self.Input))
        if sent != expected:
            raise RuntimeError(
                f"Windows accepted only {sent}/{expected} mouse input events"
            )

    def _send_unicode_character(self, character: str) -> None:
        units = character.encode("utf-16-le")
        events: list[DirectInput.Input] = []
        for index in range(0, len(units), 2):
            scan = int.from_bytes(units[index : index + 2], "little")
            events.extend(
                (
                    self.Input(
                        type=self.INPUT_KEYBOARD,
                        ki=self.KeyboardInput(0, scan, self.KEYEVENTF_UNICODE, 0, 0),
                    ),
                    self.Input(
                        type=self.INPUT_KEYBOARD,
                        ki=self.KeyboardInput(
                            0,
                            scan,
                            self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP,
                            0,
                            0,
                        ),
                    ),
                )
            )
        packet = (self.Input * len(events))(*events)
        sent = self.user32.SendInput(len(packet), packet, ctypes.sizeof(self.Input))
        if sent != len(packet):
            raise RuntimeError(
                f"Windows accepted only {sent}/{len(packet)} shared-code input events"
            )

    def click(
        self, x: int, y: int, focus_guard: Callable[[], None], clicks: int = 1,
        before_button_send: Callable[[], None] | None = None,
    ) -> None:
        focus_guard()
        if self.geometry is None:
            raise FocusLost("Roblox couldn't be focused for input")
        already_in_front = ctypes.windll.user32.GetForegroundWindow() == self.geometry.hwnd
        if not focus_roblox(self.geometry.hwnd):
            raise FocusLost("Roblox couldn't be focused for input")
        # A window that just came to the front drops the first click for a moment.
        time.sleep(0.1 if already_in_front else 0.4)
        for click_index in range(clicks):
            focus_guard()
            if self.geometry is None or _client_geometry(self.geometry.hwnd) != self.geometry:
                raise RuntimeError("Roblox client geometry changed before input")

            while True:
                pauses = self.pauses
                self._aim(x, y)
                # Button-only packets are required on negative-origin monitors.
                focus_guard()
                # A focus pause let the user move the mouse, so aim again before pressing.
                if self.pauses == pauses:
                    break
            if before_button_send is not None:
                before_button_send()
            try:
                self._send_mouse_events(
                    (self.Input * 2)(
                        self._plain_mouse_event(self.MOUSEEVENTF_LEFTDOWN),
                        self._plain_mouse_event(self.MOUSEEVENTF_LEFTUP),
                    )
                )
            except (Exception, KeyboardInterrupt):
                # A partial packet can leave the button down; release without another press.
                try:
                    self._send_mouse_events(
                        (self.Input * 1)(self._plain_mouse_event(self.MOUSEEVENTF_LEFTUP))
                    )
                except Exception:
                    pass
                raise
            focus_guard()
            if click_index + 1 < clicks:
                time.sleep(0.15)

    def _aim(self, x: int, y: int) -> None:
        absolute_x, absolute_y = self._absolute_point(x, y)
        if not self.user32.SetCursorPos(absolute_x, absolute_y):
            raise RuntimeError("Windows couldn't position the Roblox cursor")
        time.sleep(0.02)

        # Roblox's raw-input cursor ignores a zero-delta synthetic move until
        # it sees a real delta. This mirrors XynMacro's proven click sequence.
        self._send_mouse_events(
            (self.Input * 1)(self._plain_mouse_event(self.MOUSEEVENTF_MOVE, 1, 0))
        )
        time.sleep(0.005)
        self._send_mouse_events(
            (self.Input * 1)(self._plain_mouse_event(self.MOUSEEVENTF_MOVE, -1, 0))
        )
        time.sleep(0.01)

        move = (self.Input * 1)(
            self._mouse_event(x, y, self.MOUSEEVENTF_MOVE | self.MOUSEEVENTF_ABSOLUTE)
        )
        self._send_mouse_events(move)
        time.sleep(0.01)
        self._send_mouse_events(move)
        time.sleep(0.01)

    @staticmethod
    def tap(key: str, focus_guard: Callable[[], None], modifier: str | None = None) -> None:
        focus_guard()
        if modifier:
            pydirectinput.keyDown(modifier)
        try:
            pydirectinput.keyDown(key)
            try:
                time.sleep(0.03)
            finally:
                pydirectinput.keyUp(key)
        finally:
            if modifier:
                pydirectinput.keyUp(modifier)

    def replace_text(self, value: str, focus_guard: Callable[[], None]) -> None:
        focus_guard()
        pydirectinput.keyDown("ctrl")
        try:
            focus_guard()
            pydirectinput.press("a")
        finally:
            pydirectinput.keyUp("ctrl")
        if not value:
            focus_guard()
            pydirectinput.press("backspace")
            return
        for character in value:
            focus_guard()
            self._send_unicode_character(character)

# A run captures Roblox with BitBlt. If the display has gone to sleep on an
# unattended machine, capture fails. Keep the panel powered for the run; a 1px
# input nudge wakes it if it was already off.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def keep_display_awake() -> None:
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    )
    ctypes.windll.user32.mouse_event(0x0001, 1, 0, 0, 0)
    ctypes.windll.user32.mouse_event(0x0001, -1, 0, 0, 0)


def release_display_awake() -> None:
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def read_saved_quality_level(path: Path | None = None) -> int | None:
    """Roblox's SavedQualityLevel: 0 is Automatic, 1-10 are the Manual graphics bars."""
    try:
        root = ET.parse(path or ROBLOX_SETTINGS_PATH).getroot()
    except (OSError, ET.ParseError):
        return None
    for item in root.iter():
        if item.attrib.get("name") == "SavedQualityLevel":
            try:
                level = int(item.text or "")
            except ValueError:
                return None
            return level if 0 <= level <= GRAPHICS_BARS else None
    return None


def load_graphics_note(path: Path | None = None) -> int | None:
    try:
        level = json.loads((path or GRAPHICS_RESTORE_PATH).read_text(encoding="utf-8"))["level"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return level if isinstance(level, int) and 1 <= level <= GRAPHICS_BARS else None


def save_graphics_note(level: int, path: Path | None = None) -> None:
    note = path or GRAPHICS_RESTORE_PATH
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(json.dumps({"level": level}), encoding="utf-8")


def clear_graphics_note(path: Path | None = None) -> None:
    (path or GRAPHICS_RESTORE_PATH).unlink(missing_ok=True)


def mute_process_audio(process_id: int) -> list[tuple[object, bool]]:
    """Mute only audio sessions owned by one process, returning restore state."""
    try:
        from pycaw.pycaw import AudioUtilities
    except ImportError as error:
        raise RuntimeError("Roblox mute support is not installed") from error
    restore_states: list[tuple[object, bool]] = []
    try:
        for session in AudioUtilities.GetAllSessions():
            if session.ProcessId != process_id:
                continue
            volume = session.SimpleAudioVolume
            was_muted = bool(volume.GetMute())
            # Record before muting. A COM call can apply the change and still fail,
            # so rollback must include the session whose call raised.
            restore_states.append((volume, was_muted))
            volume.SetMute(1, None)
    except (Exception, KeyboardInterrupt) as mute_error:
        try:
            restore_process_audio(restore_states)
        except Exception as rollback_error:
            raise RuntimeError(
                f"Roblox audio mute failed ({mute_error}); rollback also failed "
                f"({rollback_error})"
            ) from mute_error
        if isinstance(mute_error, KeyboardInterrupt):
            raise
        raise RuntimeError(f"Roblox audio mute failed: {mute_error}") from mute_error
    return restore_states


def restore_process_audio(states: list[tuple[object, bool]]) -> None:
    errors: list[str] = []
    for volume, was_muted in states:
        try:
            volume.SetMute(int(was_muted), None)
        except (Exception, KeyboardInterrupt) as error:
            errors.append(f"{type(error).__name__}: {error}")
    if errors:
        raise RuntimeError(
            f"Couldn't restore {len(errors)} audio session(s): {'; '.join(errors)}"
        )


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


@dataclass(frozen=True)
class DisplayInfo:
    device: str
    left: int
    top: int
    width: int
    height: int
    work_left: int
    work_top: int
    work_width: int
    work_height: int
    is_primary: bool


@dataclass(frozen=True)
class WindowRestore:
    was_fullscreen: bool
    original_rect: tuple[int, int, int, int]


def _monitor_info_for_window(hwnd: int) -> _MONITORINFOEXW:
    monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
    info = _MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
    if not monitor or not ctypes.windll.user32.GetMonitorInfoW(
        monitor, ctypes.byref(info)
    ):
        raise RuntimeError("Couldn't identify the display containing Roblox")
    return info


def list_displays() -> list[DisplayInfo]:
    """Return attached displays with the primary first and stable desktop order."""
    displays: list[DisplayInfo] = []
    monitor_callback = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def collect(monitor: int, _dc: int, _rect: object, _data: int) -> bool:
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            displays.append(
                DisplayInfo(
                    device=info.szDevice,
                    left=info.rcMonitor.left,
                    top=info.rcMonitor.top,
                    width=info.rcMonitor.right - info.rcMonitor.left,
                    height=info.rcMonitor.bottom - info.rcMonitor.top,
                    work_left=info.rcWork.left,
                    work_top=info.rcWork.top,
                    work_width=info.rcWork.right - info.rcWork.left,
                    work_height=info.rcWork.bottom - info.rcWork.top,
                    is_primary=bool(info.dwFlags & 1),
                )
            )
        return True

    callback = monitor_callback(collect)
    if not ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0):
        raise RuntimeError("Windows couldn't enumerate attached displays")
    if not displays:
        raise RuntimeError("Windows reported no attached displays")
    return sorted(displays, key=lambda item: (not item.is_primary, item.left, item.top))


def _display_for_window(hwnd: int) -> DisplayInfo:
    info = _monitor_info_for_window(hwnd)
    return DisplayInfo(
        device=info.szDevice,
        left=info.rcMonitor.left,
        top=info.rcMonitor.top,
        width=info.rcMonitor.right - info.rcMonitor.left,
        height=info.rcMonitor.bottom - info.rcMonitor.top,
        work_left=info.rcWork.left,
        work_top=info.rcWork.top,
        work_width=info.rcWork.right - info.rcWork.left,
        work_height=info.rcWork.bottom - info.rcWork.top,
        is_primary=bool(info.dwFlags & 1),
    )


def resolve_run_display(hwnd: int, preference: str) -> DisplayInfo:
    if preference == "current":
        return _display_for_window(hwnd)
    displays = list_displays()
    if preference == "primary":
        return next(display for display in displays if display.is_primary)
    if preference == "secondary":
        secondary = next((display for display in displays if not display.is_primary), None)
        if secondary is None:
            raise RuntimeError("Secondary display was selected, but none is attached")
        return secondary
    raise ValueError("Run display must be current, primary, or secondary")


def _window_restore_snapshot(geometry: RobloxGeometry) -> WindowRestore:
    user32 = ctypes.windll.user32
    monitor = _monitor_info_for_window(geometry.hwnd)
    window_rect = wintypes.RECT()
    if not user32.GetWindowRect(geometry.hwnd, ctypes.byref(window_rect)):
        raise RuntimeError("Couldn't read the Roblox window bounds")
    monitor_width = monitor.rcMonitor.right - monitor.rcMonitor.left
    monitor_height = monitor.rcMonitor.bottom - monitor.rcMonitor.top
    return WindowRestore(
        geometry.width >= monitor_width and geometry.height >= monitor_height,
        (
            window_rect.left,
            window_rect.top,
            window_rect.right - window_rect.left,
            window_rect.bottom - window_rect.top,
        ),
    )


def _prepare_exact_fullscreen(
    geometry: RobloxGeometry,
    target_display: DisplayInfo,
    controls: DirectInput,
    focus_guard: Callable[[], None],
) -> RobloxGeometry:
    """Move a framed client to an exact-size monitor, then enter fullscreen.

    A 1920x1080 framed window cannot fit on a 1920x1080 display because its
    title bar and borders make the outer rect larger. Fullscreen is the only
    exact-client fallback, and it is accepted only after both size and display
    are re-verified.
    """
    if (target_display.width, target_display.height) != (SCREEN_WIDTH, SCREEN_HEIGHT):
        raise RuntimeError("Fullscreen fallback requires an exact 1920x1080 display")
    focus_guard()
    user32 = ctypes.windll.user32
    window_rect = wintypes.RECT()
    if not user32.GetWindowRect(geometry.hwnd, ctypes.byref(window_rect)):
        raise RuntimeError("Couldn't read the Roblox window bounds for fullscreen")
    outer_width = window_rect.right - window_rect.left
    outer_height = window_rect.bottom - window_rect.top
    if not user32.SetWindowPos(
        geometry.hwnd,
        None,
        target_display.left,
        target_display.top,
        outer_width,
        outer_height,
        0x0004 | 0x0010,
    ):
        raise RuntimeError("Windows refused to move Roblox to the target display")
    time.sleep(0.25)
    if _display_for_window(geometry.hwnd).device != target_display.device:
        raise RuntimeError("Roblox did not move to the selected display")
    controls.tap("f11", focus_guard)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        focus_guard()
        time.sleep(0.15)
        updated = _client_geometry(geometry.hwnd)
        if (
            updated is not None
            and (updated.width, updated.height) == (SCREEN_WIDTH, SCREEN_HEIGHT)
            and _display_for_window(geometry.hwnd).device == target_display.device
        ):
            return updated
    final = _client_geometry(geometry.hwnd)
    size = f"{final.width}x{final.height}" if final else "unavailable"
    raise RuntimeError(
        f"Roblox fullscreen ended at {size} or on the wrong display"
    )


def _windowed_target_bounds(
    target_display: DisplayInfo, frame_width: int, frame_height: int
) -> tuple[int, int, int, int] | None:
    work_width = target_display.work_width
    work_height = target_display.work_height
    outer_width = SCREEN_WIDTH + frame_width
    outer_height = SCREEN_HEIGHT + frame_height
    if outer_width > work_width or outer_height > work_height:
        return None
    x = target_display.work_left + (work_width - outer_width) // 2
    y = target_display.work_top + (work_height - outer_height) // 2
    return x, y, outer_width, outer_height


def _leave_roblox_fullscreen(
    geometry: RobloxGeometry,
    monitor_width: int,
    monitor_height: int,
    controls: DirectInput,
    focus_guard: Callable[[], None],
) -> RobloxGeometry:
    for attempt in range(2):
        focus_guard()
        if not focus_roblox(geometry.hwnd):
            raise RuntimeError("Couldn't focus Roblox to leave fullscreen")
        controls.tap("f11", focus_guard)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            focus_guard()
            time.sleep(0.15)
            updated = _client_geometry(geometry.hwnd)
            if updated and (updated.width < monitor_width or updated.height < monitor_height):
                time.sleep(0.5)
                return _client_geometry(geometry.hwnd) or updated
        if attempt == 0:
            continue
    raise RuntimeError("Roblox didn't leave fullscreen after F11")


def _prepare_roblox_window_unchecked(
    geometry: RobloxGeometry,
    controls: DirectInput,
    focus_guard: Callable[[], None],
    run_display: str = "current",
) -> tuple[RobloxGeometry, WindowRestore | None]:
    """Use XynMacro's measured-window approach to create an exact 1080p client."""
    focus_guard()
    target_display = resolve_run_display(geometry.hwnd, run_display)
    current_display = _display_for_window(geometry.hwnd)
    if (
        (geometry.width, geometry.height) == (SCREEN_WIDTH, SCREEN_HEIGHT)
        and current_display.device == target_display.device
    ):
        return geometry, None

    user32 = ctypes.windll.user32
    monitor = _monitor_info_for_window(geometry.hwnd)
    window_rect = wintypes.RECT()
    if not user32.GetWindowRect(geometry.hwnd, ctypes.byref(window_rect)):
        raise RuntimeError("Couldn't read the Roblox window bounds")
    original_rect = (
        window_rect.left,
        window_rect.top,
        window_rect.right - window_rect.left,
        window_rect.bottom - window_rect.top,
    )
    monitor_width = monitor.rcMonitor.right - monitor.rcMonitor.left
    monitor_height = monitor.rcMonitor.bottom - monitor.rcMonitor.top
    was_fullscreen = geometry.width >= monitor_width and geometry.height >= monitor_height

    if was_fullscreen:
        geometry = _leave_roblox_fullscreen(
            geometry,
            monitor_width,
            monitor_height,
            controls,
            focus_guard,
        )

    if not user32.GetWindowRect(geometry.hwnd, ctypes.byref(window_rect)):
        raise RuntimeError("Couldn't measure the windowed Roblox frame")
    frame_width = (window_rect.right - window_rect.left) - geometry.width
    frame_height = (window_rect.bottom - window_rect.top) - geometry.height
    if frame_width < 0 or frame_height < 0:
        raise RuntimeError("Roblox reported invalid window-frame dimensions")

    bounds = _windowed_target_bounds(target_display, frame_width, frame_height)
    if bounds is None:
        if (target_display.width, target_display.height) == (
            SCREEN_WIDTH,
            SCREEN_HEIGHT,
        ):
            updated = _prepare_exact_fullscreen(
                geometry, target_display, controls, focus_guard
            )
            return updated, WindowRestore(was_fullscreen, original_rect)
        raise RuntimeError(
            f"The Roblox display work area ({target_display.work_width}x{target_display.work_height}) can't fit "
            f"a {SCREEN_WIDTH}x{SCREEN_HEIGHT} client window"
        )
    x, y, outer_width, outer_height = bounds
    updated: RobloxGeometry | None = None
    for _attempt in range(3):
        focus_guard()
        if not user32.SetWindowPos(
            geometry.hwnd, None, x, y, outer_width, outer_height, 0x0004 | 0x0010
        ):
            raise RuntimeError("Windows refused to size the Roblox window")

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            focus_guard()
            time.sleep(0.15)
            updated = _client_geometry(geometry.hwnd)
            if updated and (updated.width, updated.height) == (SCREEN_WIDTH, SCREEN_HEIGHT):
                return updated, WindowRestore(was_fullscreen, original_rect)
        if updated is None:
            break
        outer_width += SCREEN_WIDTH - updated.width
        outer_height += SCREEN_HEIGHT - updated.height
        if outer_width > target_display.work_width or outer_height > target_display.work_height:
            break
    final = _client_geometry(geometry.hwnd)
    size = f"{final.width}x{final.height}" if final else "unavailable"
    raise RuntimeError(
        f"Roblox window sizing ended at {size} instead of {SCREEN_WIDTH}x{SCREEN_HEIGHT}"
    )


def prepare_roblox_window(
    geometry: RobloxGeometry,
    controls: DirectInput,
    focus_guard: Callable[[], None],
    run_display: str = "current",
    rollback_guard: Callable[[], None] | None = None,
) -> tuple[RobloxGeometry, WindowRestore | None]:
    """Resize Roblox, rolling back immediately if setup fails partway through."""
    restore = _window_restore_snapshot(geometry)
    try:
        return _prepare_roblox_window_unchecked(
            geometry, controls, focus_guard, run_display
        )
    except (Exception, KeyboardInterrupt) as setup_error:
        try:
            restore_roblox_window(restore, geometry, controls, rollback_guard or focus_guard)
        except Exception as restore_error:
            raise RuntimeError(
                f"Roblox window setup failed ({setup_error}); immediate rollback also "
                f"failed ({restore_error})"
            ) from setup_error
        raise


def restore_roblox_window(
    restore: WindowRestore,
    geometry: RobloxGeometry,
    controls: DirectInput,
    focus_guard: Callable[[], None],
) -> None:
    x, y, width, height = restore.original_rect
    current = _client_geometry(geometry.hwnd)
    if current is None:
        raise RuntimeError("Roblox geometry is unavailable during window restore")
    monitor = _monitor_info_for_window(geometry.hwnd)
    monitor_width = monitor.rcMonitor.right - monitor.rcMonitor.left
    monitor_height = monitor.rcMonitor.bottom - monitor.rcMonitor.top
    if current.width >= monitor_width and current.height >= monitor_height:
        if (
            restore.was_fullscreen
            and (current.left, current.top, current.width, current.height) == restore.original_rect
        ):
            return
        # Move/size a framed window first; fullscreen sizing is controlled by Roblox.
        _leave_roblox_fullscreen(current, monitor_width, monitor_height, controls, focus_guard)
    if not ctypes.windll.user32.SetWindowPos(
        geometry.hwnd, None, x, y, width, height, 0x0004 | 0x0010
    ):
        raise RuntimeError("Windows refused to restore the Roblox window bounds")
    time.sleep(0.5)
    if restore.was_fullscreen:
        current = _client_geometry(geometry.hwnd)
        monitor = _monitor_info_for_window(geometry.hwnd)
        monitor_width = monitor.rcMonitor.right - monitor.rcMonitor.left
        monitor_height = monitor.rcMonitor.bottom - monitor.rcMonitor.top
        if current and current.width >= monitor_width and current.height >= monitor_height:
            if (current.left, current.top, current.width, current.height) == restore.original_rect:
                return
            raise RuntimeError("Roblox fullscreen bounds were not restored during cleanup")
        if not focus_roblox(geometry.hwnd):
            raise RuntimeError("Couldn't focus Roblox to restore fullscreen")
        controls.tap("f11", focus_guard)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            time.sleep(0.15)
            current = _client_geometry(geometry.hwnd)
            if (
                current and current.width >= monitor_width and current.height >= monitor_height
                and (current.left, current.top, current.width, current.height) == restore.original_rect
            ):
                return
        raise RuntimeError("Roblox didn't return to fullscreen during cleanup")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        current = _client_geometry(geometry.hwnd)
        monitor = _monitor_info_for_window(geometry.hwnd)
        monitor_width = monitor.rcMonitor.right - monitor.rcMonitor.left
        monitor_height = monitor.rcMonitor.bottom - monitor.rcMonitor.top
        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(geometry.hwnd, ctypes.byref(rect)):
            bounds = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
            if (
                current is not None
                and (current.width < monitor_width or current.height < monitor_height)
                and bounds == restore.original_rect
            ):
                return
        time.sleep(0.15)
    raise RuntimeError("Roblox window mode or bounds were not restored during cleanup")


class DBOGRunner:
    def __init__(
        self,
        slots: list[int],
        log: Callable[[str], None],
        stop_event: threading.Event | None = None,
        close_when_done: bool = False,
        access_mode: str = "owner",
        server_code: str = "",
        server_profile: str = "",
        configured_slot_count: int = 12,
        allow_public_fallback: bool = False,
        run_display: str = "current",
        mute_roblox: bool = False,
        low_graphics: bool = False,
        low_cpu_priority: bool = False,
        slot_mismatch_policies: dict[int, str] | None = None,
        min_claim_day: int = 1,
        inspect_only: bool = False,
        max_claim_day: int = 14,
        stop_on_focus_loss: bool = True,
        sequence_align_targets: dict[int, int] | None = None,
    ) -> None:
        self.slots = slots
        self._log = log
        self.stop_event = stop_event or threading.Event()
        self.close_when_done = close_when_done
        self.inspect_only = inspect_only
        if access_mode not in {"owner", "shared"}:
            raise ValueError("access_mode must be 'owner' or 'shared'")
        self.access_mode = access_mode
        self.server_code = normalize_server_code(server_code) if access_mode == "shared" else ""
        self.server_profile = normalize_profile_name(
            server_profile, access_mode, self.server_code
        )
        if configured_slot_count < 1 or configured_slot_count > 12:
            raise ValueError("Configured slot count must be between 1 and 12")
        self.configured_slot_count = configured_slot_count
        self.allow_public_fallback = allow_public_fallback
        if run_display not in {"current", "primary", "secondary"}:
            raise ValueError("run_display must be current, primary, or secondary")
        self.run_display = run_display
        self.mute_roblox = mute_roblox
        self.low_graphics = low_graphics
        self.low_cpu_priority = low_cpu_priority
        policies = dict(slot_mismatch_policies or {})
        if any(
            not isinstance(slot, int)
            or not 1 <= slot <= 12
            or policy not in {"always", "align", "hold"}
            for slot, policy in policies.items()
        ):
            raise ValueError(
                "slot_mismatch_policies must map slot numbers to always, align, or hold"
            )
        self.slot_mismatch_policies = policies
        from xynclaim.settings import order_sequence_slots, validate_sequence

        self.sequence_align_targets = dict(sequence_align_targets or {})
        validate_sequence(policies, self.sequence_align_targets, configured_slot_count)
        self.slots = order_sequence_slots(slots, policies, self.sequence_align_targets)
        self.claimed_days_today: dict[int, tuple[int, datetime]] = {}
        if not 1 <= min_claim_day <= 14:
            raise ValueError("min_claim_day must be between 1 and 14")
        self.min_claim_day = min_claim_day
        if not min_claim_day <= max_claim_day <= 14:
            raise ValueError("max_claim_day must be between min_claim_day and 14")
        self.max_claim_day = max_claim_day
        self.detector = VisualDetector()
        # ScreenCapture is created after the client window is normalized to 1080p.
        self.capture: ScreenCapture | None = None
        self.input = DirectInput()
        self.geometry: RobloxGeometry | None = None
        self.roblox_pid: int | None = None
        self.roblox_handle: int | None = None
        self.run_mutex: int | None = None
        self.results: list[SlotResult] = []
        self.errors: list[str] = []
        self._window_restore: WindowRestore | None = None
        self._shared_code_may_be_visible = False
        self._audio_restore: list[tuple[object, bool]] = []
        self._original_priority: int | None = None
        self.mark_pending_claim = mark_pending_claim
        # Switching away from Roblox stops the run instead of pulling Roblox back to
        # the front. Armed once window setup is done.
        self.stop_on_focus_loss = stop_on_focus_loss
        self._focus_stop_armed = False
        self._paused_seconds = 0.0

    def clock(self) -> float:
        """Monotonic time minus time spent paused, so waits don't expire while paused."""
        return time.monotonic() - getattr(self, "_paused_seconds", 0.0)

    def log(self, message: str) -> None:
        try:
            self._log(redact_secret(message, self.server_code))
        except Exception as error:
            detail = redact_secret(
                f"Log output failed: {type(error).__name__}: {error}", self.server_code
            )
            if detail not in self.errors:
                self.errors.append(detail)

    def check_stopped(self) -> None:
        if self.stop_event.is_set():
            raise RunStopped

    def append_unattempted(self, detail: str) -> None:
        recorded = {result.slot for result in self.results}
        for slot in self.slots:
            if slot not in recorded:
                self.results.append(
                    SlotResult(
                        slot=slot,
                        status="not_attempted",
                        day=None,
                        seconds=0.0,
                        detail=detail,
                    )
                )

    def make_report(self, started_utc: datetime, started: float) -> RunReport:
        if self.server_code:
            self.errors = [redact_secret(error, self.server_code) for error in self.errors]
            for result in self.results:
                result.detail = redact_secret(result.detail, self.server_code)
        return RunReport(
            app_version=APP_VERSION,
            started_utc=started_utc.isoformat(),
            finished_utc=datetime.now(timezone.utc).isoformat(),
            total_seconds=round(self.clock() - started, 2),
            access_mode=self.access_mode,
            server_profile=self.server_profile,
            configured_slot_count=self.configured_slot_count,
            slots=self.results,
            errors=self.errors,
        )

    def acquire_run_mutex(self) -> None:
        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, True, "Local\\XynClaim.DBOGRunner")
        if not mutex:
            raise RuntimeError("Couldn't create the XynClaim run lock")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(mutex)
            raise RuntimeError("Another XynClaim run is already controlling Roblox")
        self.run_mutex = mutex
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        LIVE_LOG_PATH.write_text(
            f"--- {APP_NAME} run {datetime.now(timezone.utc).isoformat()} ---\n",
            encoding="utf-8",
        )

    def normal_input_guard(self) -> None:
        self.check_stopped()
        self.require_focus(cancel_check=self.check_stopped)
        self.check_stopped()

    def click(
        self, x: int, y: int, clicks: int = 1, cleanup: bool = False,
        before_button_send: Callable[[], None] | None = None,
    ) -> None:
        focus_guard = self.require_focus if cleanup else self.normal_input_guard

        def click_guard() -> None:
            focus_guard()
            if self.geometry is not None:
                target_process = process_name_at_screen_point(
                    self.geometry.left + x,
                    self.geometry.top + y,
                )
                if target_process.lower() != ROBLOX_PROCESS:
                    raise FocusLost(
                        f"Roblox click point ({x}, {y}) is obstructed by "
                        f"{target_process or 'an unknown window'}"
                    )

        click_guard()
        if before_button_send is None:
            self.input.click(x, y, click_guard, clicks)
        else:
            self.input.click(x, y, click_guard, clicks, before_button_send=before_button_send)

    def tap(self, key: str, modifier: str | None = None) -> None:
        self.normal_input_guard()
        if modifier:
            self.input.tap(key, self.normal_input_guard, modifier)
        else:
            self.input.tap(key, self.normal_input_guard)

    def lower_graphics_in_game(self) -> None:
        """Drop to graphics bar 1 with Roblox's own Shift+F10, remembering where to go back to.

        The keys only work in Manual mode. The level comes from Roblox's settings file,
        or from a note left by a run that never got to raise it again.
        """
        level = load_graphics_note() or read_saved_quality_level()
        if level is None:
            self.log("Couldn't read the Roblox graphics level; leaving graphics alone")
            return
        if level == 0:
            self.log("Roblox graphics is on Automatic; set it to Manual for low graphics to work")
            return
        save_graphics_note(level)
        self._graphics_restore_level = level
        # Extra presses are harmless: Roblox stops at bar 1.
        for _ in range(GRAPHICS_BARS - 1):
            self.tap("f10", modifier="shift")
            time.sleep(0.1)
        self.log(f"Roblox graphics lowered to 1 for this run (was {level})")

    def restore_graphics_in_game(self) -> None:
        level = getattr(self, "_graphics_restore_level", None)
        if not level:
            return
        for _ in range(level - 1):
            self.tap("f10")
            time.sleep(0.1)
        self._graphics_restore_level = None
        clear_graphics_note()
        self.log(f"Roblox graphics back to {level}")

    def require_focus(
        self, timeout: float = 3.0, cancel_check: Callable[[], None] | None = None
    ) -> None:
        if cancel_check is not None:
            cancel_check()
        if self.geometry is not None:
            if ctypes.windll.user32.GetForegroundWindow() == self.geometry.hwnd:
                return
            if cancel_check is not None and getattr(self, "_focus_stop_armed", False):
                raise FocusLost(
                    "You switched away from Roblox, so the run stopped. "
                    "Start it again when you're ready"
                )
            deadline = self.clock() + timeout
            while self.clock() < deadline:
                if cancel_check is not None:
                    cancel_check()
                if focus_roblox(self.geometry.hwnd):
                    return
                time.sleep(0.1)
        raise FocusLost(
            f"Roblox lost focus (foreground window: {foreground_process_name() or 'unknown'})"
        )

    def screenshot(self) -> np.ndarray:
        self.normal_input_guard()
        if self.roblox_handle and not self.process_running():
            raise RuntimeError("The attached Roblox process exited during the run")
        if self.geometry is not None and _client_geometry(self.geometry.hwnd) != self.geometry:
            raise RuntimeError("Roblox client geometry changed during the run")
        return self.capture.grab()

    def save_diagnostic(self, screen: np.ndarray, label: str) -> Path:
        """Save a local-only screenshot with identity and server-code areas redacted."""
        self.require_focus()
        current_geometry = _client_geometry(self.geometry.hwnd) if self.geometry else None
        if current_geometry != self.geometry:
            raise RuntimeError("Roblox geometry changed; diagnostic capture was discarded")
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^a-z0-9_-]+", "_", label.lower()).strip("_") or "state"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        path = REPORT_DIR / f"diagnostic_{stamp}_{safe_label}.png"
        redacted = screen.copy()
        redacted[0:115, 0:650] = 0
        redacted[405:605, 1375:1665] = 0  # pre-October 2026 code field
        # October 2026: the private code sits in the left join card, (633, 540).
        redacted[495:590, 380:890] = 0
        if not cv2.imwrite(str(path), redacted):
            raise OSError(f"Couldn't save diagnostic image to {path}")
        prune_report_files("diagnostic_*.png", MAX_DIAGNOSTICS)
        return path

    def read_receipt_rewards(
        self, screen: np.ndarray
    ) -> tuple[list[str] | None, np.ndarray]:
        """OCR the reward popups. They can lag the panel closing, so look twice more."""
        top, bottom, left, right = RECEIPT_REGION
        for attempt in range(3):
            if attempt:
                time.sleep(0.6)
                screen = self.screenshot()
            try:
                lines = ocr.ocr_lines(screen[top:bottom, left:right, :3])
            except Exception as error:
                # The claim has already landed; a reader fault must not fail the slot.
                self.log(f"Reward OCR failed ({error.__class__.__name__}); reward stays unverified")
                return None, screen
            if lines is None:
                return None, screen
            rewards = ocr.parse_rewards(lines)
            if rewards:
                return rewards, screen
        return [], screen

    def save_receipt_region(self, screen: np.ndarray, slot: int) -> Path:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        path = REPORT_DIR / f"receipt_{stamp}_slot_{slot:02d}.png"
        top, bottom, left, right = RECEIPT_REGION
        receipt = screen[top:bottom, left:right, :3]
        if not cv2.imwrite(str(path), receipt):
            raise OSError(f"Couldn't save receipt image to {path}")
        prune_report_files("receipt_*.png", MAX_RECEIPTS)
        return path

    def wait_for(
        self,
        name: str,
        timeout: float,
        on_poll: Callable[[np.ndarray, float], None] | None = None,
    ) -> tuple[np.ndarray, float]:
        deadline = self.clock() + timeout
        best_score = 0.0
        while self.clock() < deadline:
            screen = self.screenshot()
            matched, score = self.detector.matches(screen, name)
            best_score = max(best_score, score)
            if matched:
                return screen, score
            if on_poll:
                on_poll(screen, score)
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(f"Timed out waiting for {name}; best score {best_score:.3f}")

    def wait_for_either(
        self, first: str, second: str, timeout: float
    ) -> tuple[str | None, np.ndarray | None, float]:
        """The first of two screens to appear, or (None, None, 0.0) on timeout."""
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            screen = self.screenshot()
            for name in (first, second):
                matched, score = self.detector.matches(screen, name)
                if matched:
                    return name, screen, score
            time.sleep(POLL_INTERVAL)
        return None, None, 0.0

    def wait_until_gone(self, name: str, timeout: float) -> None:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            matched, _ = self.detector.matches(self.screenshot(), name)
            if not matched:
                return
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(f"{name} never disappeared")

    def click_transition(
        self,
        x: int,
        y: int,
        target: str,
        timeout: float,
        source: str,
        clicks: int = 1,
    ) -> tuple[np.ndarray, float]:
        for attempt in range(3):
            before_click = self.screenshot()
            target_visible, target_score = self.detector.matches(
                before_click, target
            )
            if target_visible:
                return before_click, target_score
            source_visible, _ = self.detector.matches(before_click, source)
            if not source_visible:
                # A menu still fading in, or the flash after a click that only
                # previewed, can hide both screens for a moment. Wait for either.
                settle = TRANSITION_GRACE_SECONDS if attempt else SOURCE_SETTLE_SECONDS
                seen, frame, score = self.wait_for_either(target, source, settle)
                if seen == target:
                    return frame, score
                source_visible = seen == source
            if not source_visible:
                raise UnsafeGameState(
                    f"Neither {source} nor {target} was visible before click "
                    f"{attempt + 1}; refusing input"
                )
            self.click(x, y, clicks)
            try:
                return self.wait_for(target, timeout)
            except TimeoutError:
                if attempt < 2:
                    self.log(
                        f"{source} needs confirmation — retrying ({attempt + 2}/3)"
                    )
                    continue
                raise
        raise AssertionError("unreachable")

    def click_until_gone(
        self,
        x: int,
        y: int,
        source: str,
        timeout: float,
        clicks: int = 1,
        before_first_click: Callable[[], None] | None = None,
        on_input_not_sent: Callable[[], None] | None = None,
        validate_source: Callable[[np.ndarray], None] | None = None,
        retry: bool = True,
    ) -> bool:
        clicked = False
        button_attempted = False

        def before_button_send() -> None:
            nonlocal button_attempted
            button_attempted = True

        for attempt in range(2 if retry else 1):
            before_click = self.screenshot()
            source_visible, _ = self.detector.matches(before_click, source)
            if not source_visible:
                return clicked
            if validate_source is not None:
                validate_source(before_click)
            try:
                if attempt == 0 and before_first_click is not None:
                    before_first_click()
                if on_input_not_sent is None:
                    self.click(x, y, clicks)
                else:
                    self.click(x, y, clicks, before_button_send=before_button_send)
            except (Exception, KeyboardInterrupt):
                if not clicked and not button_attempted and on_input_not_sent is not None:
                    on_input_not_sent()
                raise
            clicked = True
            try:
                self.wait_until_gone(source, timeout)
                return True
            except TimeoutError:
                if attempt == 0 and retry:
                    self.log(f"{source} needs a confirmation click — retrying once")
                    continue
                raise

    def wait_for_main_menu(self, timeout: float = MAIN_MENU_TIMEOUT) -> None:
        # DBOG's title screen ("Press Any Button To Continue") waits for input,
        # so click the middle every 2 s until the main menu shows.
        click_count = 0
        next_click = 1.0
        start = self.clock()

        def press_through_title(_screen: np.ndarray, _score: float) -> None:
            nonlocal click_count, next_click
            for unsafe_state in ("world", "daily_rewards", "game_menu", "travel_menu"):
                visible, _ = self.detector.matches(_screen, unsafe_state)
                if visible:
                    return
            elapsed = self.clock() - start
            if elapsed >= next_click:
                # Non-fatal here: while the game is still coming up the foreground
                # window flickers, so a skipped click just waits for the next poll.
                if not roblox_focused():
                    return
                if click_count == 0:
                    self.log("Pressing through DBOG's title screen")
                self.click(960, 540)
                click_count += 1
                next_click = elapsed + 2.0

        self.wait_for("main_menu", timeout, press_through_title)

    @staticmethod
    def slot_point(slot: int) -> tuple[int, int]:
        """Slot card centre in the 2x6 "Select a slot" grid (row-major)."""
        column = (slot - 1) % 2
        row = (slot - 1) // 2
        return (715 if column == 0 else 1200), 330 + row * 122

    def open_slot_select(self, timeout: float = 25.0) -> None:
        """Click Play as soon as DBOG makes the main menu interactive."""
        deadline = self.clock() + timeout
        attempts = 0
        while self.clock() < deadline:
            screen = self.screenshot()
            slot_select_ready, _ = self.detector.matches(screen, "slot_select")
            if slot_select_ready:
                return
            main_menu_ready, _ = self.detector.matches(screen, "main_menu")
            if not main_menu_ready:
                if attempts == 0:
                    try:
                        self.wait_for("main_menu", min(5.0, deadline - self.clock()))
                    except TimeoutError:
                        raise UnsafeGameState(
                            "Neither main_menu nor slot_select was visible while opening Play"
                        )
                else:
                    time.sleep(POLL_INTERVAL)
                continue
            self.click(*PLAY_BUTTON)
            attempts += 1
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            # The menu draws a few seconds before Play accepts input; re-clicking
            # quickly is harmless and saves waiting out a long timeout.
            try:
                self.wait_for("slot_select", min(1.5, remaining))
                return
            except TimeoutError:
                if attempts == 4:
                    self.log("Play is still enabling — retrying while it loads")
        raise TimeoutError("Play didn't become interactive within 25 seconds")

    @staticmethod
    def read_streak(screen: np.ndarray) -> int | None:
        top, bottom, left, right = STREAK_TEXT_REGION
        lines = ocr.ocr_lines(screen[top:bottom, left:right, :3]) or []
        return ocr.parse_streak(lines)

    @staticmethod
    def read_claim_button(screen: np.ndarray) -> int | None:
        """The N in "Claim Day N", or None once the button reads "Claimed"."""
        top, bottom, left, right = CLAIM_BUTTON_TEXT_REGION
        lines = ocr.ocr_lines(screen[top:bottom, left:right, :3]) or []
        return ocr.parse_claim_day(lines)

    @staticmethod
    def detect_claim_day(screen: np.ndarray) -> int:
        """Read "Claim Day N" off the daily panel's button, else Streak + 1."""
        day = DBOGRunner.read_claim_button(screen)
        if day is None:
            streak = DBOGRunner.read_streak(screen)
            if streak is not None:
                day = streak % 14 + 1
        if day is None or not 1 <= day <= 14:
            raise UnsafeGameState("Couldn't read the claim day from the daily panel")
        return day

    def confirm_claim_day(self, screen: np.ndarray, day: int) -> None:
        """Refuse the Claim input unless the panel still reads the settled day.

        Uses the same button-then-streak reading as detect_claim_day: a cursor resting
        on the button highlights it and can make its text unreadable, which is not a
        changed streak.
        """
        visible_day = self.read_claim_button(screen)
        if visible_day is None:
            streak = self.read_streak(screen)
            if streak is None:
                raise UnsafeGameState("Daily panel became unreadable before Claim; refusing input")
            visible_day = streak % 14 + 1
        if visible_day != day:
            raise UnsafeGameState("Daily streak changed before Claim; refusing input")

    def settled_daily_screen(self, screen: np.ndarray) -> np.ndarray:
        """Re-grab the daily panel until its claim day reads the same several times.

        The panel can be detected while it is still fading in over the loading
        screen, when its button text is unreadable. Unreadable frames are skipped;
        a day is only trusted after three agreeing samples and a minimum dwell.
        """
        if not self.detector.matches(screen, "daily_rewards")[0]:
            raise UnsafeGameState("Daily panel disappeared before settling")

        def readable_day(frame: np.ndarray) -> int | None:
            # The panel can be detected while still fading in, with unreadable text.
            try:
                return self.detect_claim_day(frame)
            except UnsafeGameState:
                return None

        day = readable_day(screen)
        stable_samples = 1 if day is not None else 0
        stable_since = self.clock()
        deadline = self.clock() + 8.0
        while self.clock() < deadline:
            time.sleep(0.2)
            candidate = self.screenshot()
            if not self.detector.matches(candidate, "daily_rewards")[0]:
                raise UnsafeGameState("Daily panel disappeared during settling")
            candidate_day = readable_day(candidate)
            if candidate_day is None:
                continue
            if candidate_day == day:
                stable_samples += 1
                if (
                    stable_samples >= DAILY_SETTLE_STABLE_SAMPLES
                    and self.clock() - stable_since >= DAILY_SETTLE_MIN_DWELL
                ):
                    return candidate
            else:
                day = candidate_day
                stable_samples = 1
                stable_since = self.clock()
        raise TimeoutError("Daily panel day did not settle")

    def wait_for_daily_or_world(self) -> tuple[str, np.ndarray]:
        deadline = self.clock() + WORLD_TIMEOUT
        best_daily = 0.0
        best_world = 0.0
        while self.clock() < deadline:
            screen = self.screenshot()
            daily, daily_score = self.detector.matches(screen, "daily_rewards")
            best_daily = max(best_daily, daily_score)
            if daily:
                return "daily", self.settled_daily_screen(screen)
            world, world_score = self.detector.matches(screen, "world")
            best_world = max(best_world, world_score)
            if world:
                return "world", screen
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(
            f"World did not load; best daily score {best_daily:.3f}, "
            f"best world score {best_world:.3f}"
        )

    def wait_for_daily_grace(self) -> tuple[bool, np.ndarray | None]:
        deadline = self.clock() + DAILY_GRACE_SECONDS
        while self.clock() < deadline:
            screen = self.screenshot()
            daily, _ = self.detector.matches(screen, "daily_rewards")
            if daily:
                return True, self.settled_daily_screen(screen)
            time.sleep(POLL_INTERVAL)
        return False, None

    def open_private_slot(self, slot: int) -> None:
        if slot > self.configured_slot_count:
            raise SlotUnavailable(
                f"Slot {slot} is above the configured {self.configured_slot_count}-slot limit"
            )
        self.open_slot_select()
        try:
            # The first click only previews the slot; the second one picks it. The
            # screen is blank while the destination list loads, so wait for it
            # instead of treating the blank frame as unsafe.
            self.click_transition(
                *self.slot_point(slot), "server_menu", 0.8, "slot_select"
            )
        except TimeoutError as error:
            # The final pick can leave a blank frame for longer than 0.8 s while the
            # destination list loads, so wait without clicking before judging it.
            try:
                self.wait_for("server_menu", TRANSITION_GRACE_SECONDS)
            except TimeoutError:
                screen = self.screenshot()
                still_on_slots, _ = self.detector.matches(screen, "slot_select")
                if still_on_slots:
                    raise SlotUnavailable(
                        f"Slot {slot} stayed unavailable after selection; it may be empty, "
                        "unused, locked, or unpurchased"
                    ) from error
                raise UnsafeGameState(
                    f"Slot {slot} opened an unrecognized screen instead of the server menu"
                ) from error
        # Destination list -> North Earth opens "Join a private server" / "Join a friend".
        self.click_transition(
            *DESTINATION_NORTH_EARTH, "private_panel", 8.0, "server_menu"
        )
        time.sleep(0.5)

        # DBOG pre-fills the owner's code. Focusing the field and pressing Enter joins
        # it, the same as pasting a code. Generate is never clicked: it would replace
        # the code.
        self.click(*PRIVATE_CODE_FIELD)
        self.normal_input_guard()
        if self.access_mode == "shared":
            self.log(f"Slot {slot}: entering shared server code (value hidden)")
            # Set this before typing because a focus/stop/input failure can occur
            # after only part of the value reaches the visible field.
            self._shared_code_may_be_visible = True
            self.input.replace_text(self.server_code, self.normal_input_guard)
        try:
            for attempt in range(2):
                # The field needs a moment to take focus; an early Enter is dropped.
                time.sleep(0.4)
                self.tap("enter")
                try:
                    self.wait_until_gone("private_panel", 15.0)
                    break
                except TimeoutError:
                    if attempt:
                        raise
                    self.log("Private server join needs a second Enter — retrying once")
                    self.click(*PRIVATE_CODE_FIELD)
            self._shared_code_may_be_visible = False
        except TimeoutError as error:
            screen = self.screenshot()
            diagnostic = self.save_diagnostic(screen, "join_rejected")
            if self.access_mode == "shared":
                message = (
                    "The shared server code was rejected or the join wasn't accepted. "
                    "It may be invalid, manually regenerated, tied to an owner who lost "
                    "private-network access, or typed incorrectly."
                )
                try:
                    self.click(*PRIVATE_CODE_FIELD)
                    self.input.replace_text("", self.require_focus)
                    self._shared_code_may_be_visible = False
                except Exception:
                    pass
            else:
                message = (
                    "DBOG didn't accept the auto-filled private server code. "
                    "Create or select a private network in-game, then try again."
                )
            raise JoinCodeRejected(f"{message} Diagnostic: {diagnostic.name}") from error

    def clear_visible_shared_code(self) -> None:
        if not self._shared_code_may_be_visible:
            return
        if (
            self.access_mode != "shared"
            or self.geometry is None
            or self.capture is None
            or not self.process_running()
        ):
            raise RuntimeError("shared-code field cleanup could not inspect Roblox")
        if not focus_roblox(self.geometry.hwnd):
            raise RuntimeError("shared-code field cleanup could not focus Roblox")
        current = _client_geometry(self.geometry.hwnd)
        if current != self.geometry:
            raise RuntimeError("shared-code field cleanup found changed Roblox geometry")
        screen = self.capture.grab()
        private_panel, _ = self.detector.matches(screen, "private_panel")
        if not private_panel:
            self._shared_code_may_be_visible = False
            self.log("No shared-code field remained visible during cleanup")
            return
        self.click(*PRIVATE_CODE_FIELD, cleanup=True)
        self.input.replace_text("", self.require_focus)
        self._shared_code_may_be_visible = False
        self.log("Cleared the shared-code field during cleanup")

    def click_claim_button(
        self,
        slot: int,
        day: int,
        validate_source: Callable[[np.ndarray], None] | None = None,
    ) -> np.ndarray:
        """Click Claim and wait for the panel's streak counter to advance.

        The panel stays open after claiming (the button turns to "Claimed"), so the
        streak footer is the confirmation. Returns the first frame showing the
        advanced streak, which also holds the reward toast. A second click is only
        sent while the button still offers this same day, so an accepted claim is
        never clicked twice.
        """
        before = self.screenshot()
        if not self.detector.matches(before, "daily_rewards")[0]:
            raise UnsafeGameState(
                f"Slot {slot}: daily panel disappeared before the Claim input; "
                "claim state was not advanced"
            )
        if validate_source is not None:
            validate_source(before)
        start_streak = self.read_streak(before)
        expected = day if start_streak is None else start_streak + 1
        button_attempted = False

        def before_button_send() -> None:
            nonlocal button_attempted
            button_attempted = True

        self.mark_pending_claim(slot, day)
        try:
            self.click(*CLAIM_BUTTON, before_button_send=before_button_send)
        except (Exception, KeyboardInterrupt):
            if not button_attempted:
                clear_pending_claim(slot)
            raise
        for attempt in range(2):
            deadline = self.clock() + 5.0
            while self.clock() < deadline:
                time.sleep(0.25)
                screen = self.screenshot()
                streak = self.read_streak(screen)
                if streak is not None and streak in {expected, expected % 14}:
                    return screen
            if attempt == 0 and self.read_claim_button(self.screenshot()) == day:
                self.log(f"Slot {slot}: Claim Day {day} is still offered — clicking it once more")
                self.click(*CLAIM_BUTTON)
                continue
            break
        raise UnsafeGameState(
            f"Slot {slot}: the streak counter never advanced after clicking Claim Day {day}"
        )

    def open_public_fallback(self, slot: int) -> None:
        self.log(
            f"Slot {slot}: private access was rejected — using explicitly enabled "
            "public-server fallback"
        )
        self.click_until_gone(960, 910, "private_panel", 12.0)  # Quick Join

    def prepare_menu(self) -> None:
        screen = self.screenshot()
        daily_open, _ = self.detector.matches(screen, "daily_rewards")
        if daily_open:
            self.click_until_gone(*DAILY_BACK_BUTTON, "daily_rewards", 5.0)
            screen = self.screenshot()
        menu_open, _ = self.detector.matches(screen, "game_menu")
        if not menu_open:
            self.tap("m")
        self.wait_for("game_menu", 5.0)

    def return_to_main_menu(self) -> None:
        self.prepare_menu()
        self.click_transition(*MENU_TRAVEL_TAB, "travel_menu", 5.0, "game_menu")
        self.click_until_gone(*TRAVEL_MAIN_MENU, "travel_menu", 8.0)
        self.wait_for_main_menu(MAIN_MENU_TIMEOUT)

    def return_after_recorded_result(self, result: SlotResult) -> SlotResult:
        try:
            self.return_to_main_menu()
        except (Exception, KeyboardInterrupt) as error:
            raise PostClaimNavigationFailed(result, error) from error
        return result

    def process_slot(self, slot: int) -> SlotResult:
        started = self.clock()
        self.log(f"Slot {slot}: opening private server")
        server_route = "private"
        try:
            self.open_private_slot(slot)
        except JoinCodeRejected:
            if not self.allow_public_fallback:
                raise
            self.open_public_fallback(slot)
            server_route = "public_fallback"

        loaded_state, screen = self.wait_for_daily_or_world()

        if loaded_state == "world":
            has_daily, daily_screen = self.wait_for_daily_grace()
            if not has_daily:
                saved = load_claim_state().get(str(slot), {})
                if "pending_claim" in saved and not getattr(self, "inspect_only", False):
                    raise SlotHeld(
                        f"Slot {slot} has an unresolved pending claim; an absent daily "
                        "panel does not prove acceptance"
                    )
                saved_day = saved.get("claimed_day")
                saved_reward = saved.get("reward")
                saved_next_day = saved.get("next_day")
                saved_next_reward = saved.get("next_reward")
                if saved_day:
                    self.log(
                        f"Slot {slot}: no daily available; last recorded Day {saved_day} "
                        f"({saved_reward}), next is Day {saved_next_day} ({saved_next_reward})"
                    )
                else:
                    self.log(f"Slot {slot}: no daily available; streak day is not recorded yet")
                result = SlotResult(
                    slot=slot,
                    status="no_daily",
                    day=saved_day,
                    seconds=round(self.clock() - started, 2),
                    reward=saved_reward,
                    next_day=saved_next_day,
                    next_reward=saved_next_reward,
                    detail="Daily panel did not appear after the world loaded",
                    server_route=server_route,
                )
                return self.return_after_recorded_result(result)
            screen = daily_screen

        # The claim button names the day it will give ("Claim Day N"), so it is
        # authoritative even when a broken streak shows the paid Restore button.
        # Restore costs Robux and is never clicked.
        day = self.detect_claim_day(screen)
        broken_streak, _ = self.detector.matches(screen, "restore_streak")
        if broken_streak:
            self.log(f"Slot {slot}: Restore Streak (Robux) is offered; claiming Day {day} without it")
        if getattr(self, "inspect_only", False):
            self.log(
                f"Slot {slot}: currently on Day {day} ({DAILY_REWARDS[day]}) [INSPECT ONLY — not claiming]"
            )
            result = SlotResult(
                slot=slot,
                status="inspected",
                day=day,
                seconds=round(self.clock() - started, 2),
                reward=DAILY_REWARDS[day],
                next_day=day,
                next_reward=DAILY_REWARDS[day],
                detail=f"Currently on Day {day}: {DAILY_REWARDS[day]} (inspected without claiming)",
                server_route=server_route,
            )
            return self.return_after_recorded_result(result)
        saved_record = load_claim_state().get(str(slot), {})
        pending_day = pending_claim_day(saved_record)
        if "pending_claim" in saved_record and pending_day is None:
            raise SlotHeld(f"Slot {slot} has an invalid pending claim record; refusing input")
        if pending_day is not None:
            pending_next_day = 1 if pending_day == 14 else pending_day + 1
            if not broken_streak and day == pending_next_day:
                attempted_utc = saved_record["pending_claim"].get("started_utc")
                try:
                    attempted = datetime.fromisoformat(attempted_utc)
                    if attempted.tzinfo is None:
                        raise ValueError("attempt time has no timezone")
                except (TypeError, ValueError) as error:
                    raise SlotHeld(
                        f"Slot {slot} has a pending claim with an invalid attempt time; refusing input"
                    ) from error
                update_claim_state(
                    slot, pending_day, verification="reconciled", claimed_utc=attempted_utc
                )
                self.log(
                    f"Slot {slot}: reconciled pending Day {pending_day}; the visible "
                    f"panel advanced to Day {day}"
                )
                saved_record = load_claim_state().get(str(slot), {})
            else:
                raise SlotHeld(
                    f"Slot {slot} has an unresolved pending Day {pending_day} claim and "
                    f"currently shows Day {day}; refusing to click it again"
                )
        saved_next_day = saved_record.get("next_day")
        if day > getattr(self, "max_claim_day", 14):
            self.log(f"Slot {slot}: Day {day} exceeds maximum claim day {self.max_claim_day}; holding")
            return self.return_after_recorded_result(SlotResult(
                slot=slot, status="held", day=day, seconds=round(self.clock() - started, 2),
                reward=DAILY_REWARDS[day], next_day=day, next_reward=DAILY_REWARDS[day],
                server_route=server_route,
                detail=f"Visible Day {day} exceeds maximum claim day {self.max_claim_day}",
            ))
        if day < getattr(self, "min_claim_day", 1):
            self.log(
                f"Slot {slot}: visible Day {day} is below minimum claim day {self.min_claim_day}; "
                "holding slot without claiming"
            )
            result = SlotResult(
                slot=slot,
                status="held",
                day=day,
                seconds=round(self.clock() - started, 2),
                reward=DAILY_REWARDS[day],
                next_day=day,
                next_reward=DAILY_REWARDS[day],
                detail=f"Visible Day {day} is below minimum claim day {self.min_claim_day}",
                server_route=server_route,
            )
            return self.return_after_recorded_result(result)
        policy = getattr(self, "slot_mismatch_policies", {}).get(slot)
        if policy == "hold":
            raise SlotHeld(f"Slot {slot}: held by slot mode")
        if policy == "align":
            from xynclaim.settings import alignment_reason, recorded_day_today

            target = getattr(self, "sequence_align_targets", {}).get(slot, 1)
            live_claim = getattr(self, "claimed_days_today", {}).get(target)
            target_day = None
            if live_claim is not None:
                target_day, _ = recorded_day_today({
                    "claimed_day": live_claim[0], "claimed_utc": live_claim[1].isoformat(),
                })
            if target_day is None:
                target_record = load_claim_state().get(str(target), {})
                target_day, claimed_today = recorded_day_today(target_record)
                if target in getattr(self, "slots", []) and not claimed_today:
                    target_day = None
            target_result = next((result for result in getattr(self, "results", [])
                                  if result.slot == target), None)
            if target_result is not None and target_result.status == "held" and target_result.day is not None:
                raise SlotHeld(f"Slot {slot}: waiting to line up with slot {target} (target skipped today)", day)
            if target_day is None:
                self.log(f"Slot {slot}: slot {target}'s day today is unknown; claiming the live Day {day}")
            elif day != target_day:
                raise SlotHeld(alignment_reason(slot, day, target, target_day), day)
            else:
                self.log(f"Slot {slot}: lined up with slot {target} on Day {day}")
        if saved_next_day is not None and day != saved_next_day:
            if policy in {"always", "align"}:
                self.log(
                    f"Slot {slot}: WARNING ? visible Day {day} differs from local "
                    f"Day {saved_next_day}; using the visible day"
                )
            else:
                raise RuntimeError(
                    f"Detected Day {day}, but local history expected Day {saved_next_day}; "
                    "refusing to claim until the streak is checked"
                )
        reward = DAILY_REWARDS[day]
        next_day = 1 if day == 14 else day + 1
        next_reward = DAILY_REWARDS[next_day]
        self.log(f"Slot {slot}: claiming Day {day} — {reward}")

        claim_screen = self.click_claim_button(
            slot, day, lambda claim_screen: self.confirm_claim_day(claim_screen, day)
        )
        result = SlotResult(
            slot=slot,
            status="claimed_unverified",
            day=day,
            seconds=round(self.clock() - started, 2),
            reward=reward,
            next_day=next_day,
            next_reward=next_reward,
            verification="unverified",
            server_route=server_route,
            detail="Claim accepted. The reward has not been machine-verified.",
        )
        if not hasattr(self, "claimed_days_today"):
            self.claimed_days_today = {}
        self.claimed_days_today[slot] = (day, datetime.now(timezone.utc))
        # Acceptance must survive any later receipt, state, or navigation failure.
        try:
            update_claim_state(slot, day)
            receipt_screen = claim_screen
            global_locked, _ = self.detector.matches(receipt_screen, "global_reward_locked")
            read_rewards = None
            if not global_locked:
                read_rewards, receipt_screen = self.read_receipt_rewards(receipt_screen)
            if global_locked:
                result.received_reward = "+10 Mastery Points"
                result.verification = "global_locked"
                result.status = "claimed_global_locked"
                result.detail = (
                    "Claim accepted. The receipt explicitly reported Global reward locked "
                    f"and {result.received_reward}."
                )
            elif read_rewards:
                result.received_reward = "; ".join(read_rewards)
                result.verification = "ocr"
                locked_by_text = any(
                    reward.endswith("(global reward locked)") for reward in read_rewards
                )
                result.status = "claimed_global_locked" if locked_by_text else "claimed_verified"
                result.detail = f"Claim accepted. The reward popup read: {result.received_reward}."
            if result.verification != "unverified":
                update_claim_state(
                    slot, day, verification=result.verification, received_reward=result.received_reward
                )
            receipt_path = self.save_receipt_region(receipt_screen, slot)
            result.receipt_image = receipt_path.name
            if result.status == "claimed_global_locked":
                self.log(
                    f"Slot {slot}: GLOBAL REWARD LOCKED — received {result.received_reward} "
                    f"instead of expected {reward}; receipt saved as {receipt_path.name}"
                )
            elif read_rewards:
                self.log(
                    f"Slot {slot}: Day {day} claimed — received {result.received_reward} "
                    f"(expected {reward}); receipt saved as {receipt_path.name}"
                )
            else:
                self.log(
                    f"Slot {slot}: Day {day} panel accepted — expected {reward}; "
                    f"receipt saved as {receipt_path.name}; reward remains unverified"
                )
                result.detail = (
                    "Claim accepted. The receipt image was captured locally, but the "
                    "actual reward wasn't machine-verified."
                )
        except (Exception, KeyboardInterrupt) as error:
            result.seconds = round(self.clock() - started, 2)
            raise PostClaimNavigationFailed(result, error, "receipt or state") from error
        result.seconds = round(self.clock() - started, 2)
        return self.return_after_recorded_result(result)

    @staticmethod
    def roblox_running() -> bool:
        target_pid = os.environ.get("XYNCLAIM_ROBLOX_PID")
        if target_pid and target_pid.isdigit():
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {target_pid}", "/NH"],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            return "RobloxPlayerBeta.exe" in result.stdout
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RobloxPlayerBeta.exe", "/NH"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
        return "RobloxPlayerBeta.exe" in result.stdout

    def process_running(self, pid: int | None = None) -> bool:
        if self.roblox_handle is None:
            return False
        return ctypes.windll.kernel32.WaitForSingleObject(self.roblox_handle, 0) == 0x102

    def attach_roblox_process(self, geometry: RobloxGeometry) -> None:
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(geometry.hwnd, ctypes.byref(pid))
        process_id = int(pid.value)
        handle = ctypes.windll.kernel32.OpenProcess(
            0x00100000 | 0x1000 | 0x0200 | 0x0001, False, process_id
        )
        if not handle:
            raise RuntimeError("Couldn't hold a safe handle to the Roblox process")
        if self.roblox_handle:
            ctypes.windll.kernel32.CloseHandle(self.roblox_handle)
        self.geometry = geometry
        self.roblox_pid = process_id
        self.roblox_handle = handle

    def apply_run_environment(self) -> None:
        if self.roblox_pid is None or self.roblox_handle is None:
            raise RuntimeError("Roblox process is unavailable for run settings")
        if getattr(self, "low_cpu_priority", False):
            priority = ctypes.windll.kernel32.GetPriorityClass(self.roblox_handle)
            if not priority:
                raise RuntimeError("Couldn't read Roblox process priority")
            if not ctypes.windll.kernel32.SetPriorityClass(self.roblox_handle, 0x00004000):
                raise RuntimeError("Couldn't lower Roblox process priority")
            self._original_priority = int(priority)
            self.log("Roblox CPU priority lowered for this run")
        if getattr(self, "mute_roblox", False):
            self._audio_restore = mute_process_audio(self.roblox_pid)
            if self._audio_restore:
                self.log("Roblox audio muted for this run")
            else:
                self.log("Roblox had no active audio session to mute")

    def ensure_roblox_window(self) -> bool:
        self.check_stopped()
        ensure_default_desktop()
        deadline = self.clock() + 15.0
        while self.clock() < deadline:
            self.check_stopped()
            geometry = find_roblox_window()
            if geometry is not None:
                self.log("Roblox is already running — attaching to it")
                self.attach_roblox_process(geometry)
                if focus_roblox(geometry.hwnd):
                    return True
            if not self.roblox_running():
                break
            time.sleep(0.5)

        self.check_stopped()
        if find_roblox_window() is not None:
            raise FocusLost("A usable Roblox window could not be focused; leaving it open")
        target_pid = os.environ.get("XYNCLAIM_ROBLOX_PID")
        if self.roblox_running():
            self.check_stopped()
            if target_pid and target_pid.isdigit():
                self.log(f"Terminating target ghost Roblox process PID {target_pid} before launch")
                subprocess.run(
                    ["taskkill", "/F", "/PID", target_pid],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                )
            else:
                for stuck_pid in stuck_roblox_pids():
                    self.log(f"Closing stuck Roblox process PID {stuck_pid} (no window) before launch")
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(stuck_pid)],
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        check=False,
                    )
            time.sleep(1.0)

        self.log("Launching Dragon Generations directly")
        self.check_stopped()
        launch_roblox_on_default_desktop(GAME_URI)
        self.check_stopped()
        launch_roblox_on_default_desktop(GAME_URI)

        deadline = self.clock() + 75.0
        while self.clock() < deadline:
            self.check_stopped()
            geometry = find_roblox_window()
            if geometry is not None:
                self.attach_roblox_process(geometry)
                if focus_roblox(geometry.hwnd):
                    return False
            time.sleep(0.25)
        raise RuntimeError(
            "Roblox didn't open a game window. If Roblox is asking you to sign in, "
            "sign in there and run again"
        )

    def finish_launch(self, already_running: bool) -> None:
        if already_running:
            # Anywhere but the main menu (in the world, on the daily panel, in a
            # menu) goes back through M > Travel > Main Menu, the same route a slot
            # takes when no daily panel shows. Clicking through the intro-skip
            # instead would fire abilities at the character in the world.
            on_main_menu, _ = self.detector.matches(self.screenshot(), "main_menu")
            if not on_main_menu:
                self.log(
                    "Roblox is already open but not on the main menu; going there "
                    "through the DBOG menu before starting"
                )
                try:
                    self.prepare_menu()
                except TimeoutError:
                    # Intro or loading screen: the game menu can't open yet, so
                    # fall through to the normal wait for the main menu.
                    self.log("DBOG menu didn't open; waiting for the main menu instead")
                else:
                    self.click_transition(*MENU_TRAVEL_TAB, "travel_menu", 5.0, "game_menu")
                    self.click_until_gone(*TRAVEL_MAIN_MENU, "travel_menu", 8.0)
        try:
            self.wait_for_main_menu(MAIN_MENU_TIMEOUT)
        except TimeoutError as error:
            if already_running:
                raise
            raise RuntimeError(
                "DBOG's main menu never appeared. If Roblox is asking you to sign in, "
                "sign in there and run again"
            ) from error
        if not already_running:
            self.log("Main menu visible — waiting for DBOG services to become interactive")
            deadline = self.clock() + 25.0
            while self.clock() < deadline:
                self.check_stopped()
                time.sleep(0.25)
        if already_running:
            # Roblox swallows the first synthetic click after it is focused by hand.
            self.click(960, 300)
            time.sleep(0.5)
        self.log("Main menu detected")

    def close_roblox(self) -> None:
        """Ask Roblox to close, then make sure it did. The earlier version clicked
        the game's own "are you sure" dialog; that reference screenshot is gone, so
        confirmation is left to the process check and the taskkill fallback."""
        if not self.process_running():
            return
        self.log("Closing Roblox")
        if self.geometry is not None:
            current_pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(
                self.geometry.hwnd, ctypes.byref(current_pid)
            )
            if int(current_pid.value) == self.roblox_pid:
                ctypes.windll.user32.PostMessageW(
                    self.geometry.hwnd, 0x0010, 0, 0
                )  # WM_CLOSE

        deadline = self.clock() + 5.0
        while self.clock() < deadline and self.process_running():
            time.sleep(0.2)
        forced = False
        if self.process_running():
            forced = True
            if not ctypes.windll.kernel32.TerminateProcess(self.roblox_handle, 1):
                raise RuntimeError("Windows couldn't force-close Roblox")
            time.sleep(0.3)
        if self.process_running():
            raise RuntimeError("Roblox is still running after the close request")
        self.log("Roblox force-closed" if forced else "Roblox closed")

    def close_then_restore_audio(self) -> None:
        """Keep Roblox muted through shutdown, while still restoring every session."""
        if self.close_when_done:
            try:
                self.close_roblox()
            except (Exception, KeyboardInterrupt) as error:
                detail = f"Cleanup failed: {type(error).__name__}: {error}"
                self.errors.append(detail)
                self.log(detail)
        if getattr(self, "_audio_restore", None):
            try:
                restore_process_audio(self._audio_restore)
                self.log("Restored Roblox audio")
            except Exception as error:
                detail = f"Audio restore failed: {type(error).__name__}: {error}"
                self.errors.append(detail)
                self.log(detail)
            self._audio_restore = []

    def run(self) -> RunReport:
        ensure_default_desktop()
        started = self.clock()
        started_utc = datetime.now(timezone.utc)
        display_awake = False
        audio_com = None
        try:
            self.check_stopped()
            self.acquire_run_mutex()
            keep_display_awake()
            display_awake = True
            already_running = self.ensure_roblox_window()
            if self.geometry is None:
                raise RuntimeError("Roblox window geometry is unavailable")
            self.geometry = wait_for_stable_geometry(
                self.geometry.hwnd, cancel_check=self.check_stopped
            )
            self.attach_roblox_process(self.geometry)
            self.input.set_geometry(self.geometry)
            if not focus_roblox(self.geometry.hwnd):
                raise RuntimeError("Couldn't focus Roblox before window setup")
            self.geometry, self._window_restore = prepare_roblox_window(
                self.geometry,
                self.input,
                self.normal_input_guard,
                getattr(self, "run_display", "current"),
                rollback_guard=self.require_focus,
            )
            self.geometry = wait_for_stable_geometry(
                self.geometry.hwnd, cancel_check=self.check_stopped
            )
            self.attach_roblox_process(self.geometry)
            if (self.geometry.width, self.geometry.height) != (SCREEN_WIDTH, SCREEN_HEIGHT):
                raise RuntimeError(
                    f"Roblox client must be {SCREEN_WIDTH}x{SCREEN_HEIGHT}, got "
                    f"{self.geometry.width}x{self.geometry.height}"
                )
            if not focus_roblox(self.geometry.hwnd):
                raise RuntimeError("Couldn't restore Roblox focus after window setup")
            self.input.set_geometry(self.geometry)
            self.capture = ScreenCapture(self.geometry)
            if getattr(self, "mute_roblox", False):
                import comtypes

                comtypes.CoInitialize()
                audio_com = comtypes
            if getattr(self, "mute_roblox", False) or getattr(
                self, "low_cpu_priority", False
            ):
                self.apply_run_environment()
            self._focus_stop_armed = getattr(self, "stop_on_focus_loss", False)
            self.finish_launch(already_running)
            if getattr(self, "low_graphics", False) or load_graphics_note() is not None:
                # A leftover note means an earlier run died at bar 1: lower (a no-op)
                # so this run's cleanup raises it back to the player's level.
                self.lower_graphics_in_game()
            for index, slot in enumerate(self.slots):
                self.check_stopped()
                slot_started = self.clock()
                try:
                    result = self.process_slot(slot)
                except RunStopped:
                    raise
                except PostClaimNavigationFailed as error:
                    result = error.result
                    detail = str(error)
                    self.results.append(result)
                    if self.capture is not None:
                        try:
                            diagnostic = self.save_diagnostic(
                                self.screenshot(), f"slot_{slot}_post_result_navigation"
                            )
                            detail += f"; diagnostic: {diagnostic.name}"
                        except (Exception, KeyboardInterrupt):
                            pass
                    self.errors.append(f"Slot {slot}: {detail}")
                    self.log(f"Slot {slot}: RESULT PRESERVED — {detail}")
                    self.append_unattempted(
                        "Run stopped after post-result navigation failed"
                    )
                    break
                except SlotUnavailable as error:
                    detail = str(error)
                    self.log(f"Slot {slot}: not available — {detail}")
                    self.results.append(
                        SlotResult(
                            slot=slot,
                            status="slot_missing",
                            day=None,
                            seconds=round(self.clock() - slot_started, 2),
                            detail=detail,
                        )
                    )
                    continue
                except SlotHeld as error:
                    detail = str(error)
                    self.log(f"Slot {slot}: HELD — {detail}")
                    self.results.append(
                        SlotResult(
                            slot=slot,
                            status="held",
                            day=error.day,
                            seconds=round(self.clock() - slot_started, 2),
                            detail=detail,
                        )
                    )
                    try:
                        self.return_to_main_menu()
                    except (RunStopped, KeyboardInterrupt):
                        raise
                    except Exception as error:
                        detail = f"Slot {slot}: held-result navigation failed: {type(error).__name__}: {error}"
                        self.errors.append(detail)
                        self.log(detail)
                        self.append_unattempted("Navigation failed after a held slot")
                        break
                    continue
                except JoinCodeRejected as error:
                    detail = str(error)
                    self.log(f"Slot {slot}: SERVER CODE REJECTED — {detail}")
                    self.errors.append(f"Slot {slot}: {detail}")
                    self.results.append(
                        SlotResult(
                            slot=slot,
                            status="server_code_invalid",
                            day=None,
                            seconds=round(self.clock() - slot_started, 2),
                            detail=detail,
                        )
                    )
                    for pending_slot in self.slots[index + 1 :]:
                        self.results.append(
                            SlotResult(
                                slot=pending_slot,
                                status="not_attempted",
                                day=None,
                                seconds=0.0,
                                detail="The selected server access was rejected",
                            )
                        )
                    break
                except FocusLost as error:
                    detail = str(error)
                    self.log(f"Slot {slot}: STOPPED SAFELY — {detail}")
                    self.errors.append(f"Slot {slot}: {detail}")
                    self.results.append(
                        SlotResult(
                            slot=slot,
                            status="failed",
                            day=None,
                            seconds=round(self.clock() - slot_started, 2),
                            detail=detail,
                        )
                    )
                    self.append_unattempted("Run stopped after Roblox lost focus")
                    break
                except Exception as error:
                    detail = f"{type(error).__name__}: {error}"
                    if self.capture is not None:
                        try:
                            diagnostic = self.save_diagnostic(
                                self.screenshot(), f"slot_{slot}_failure"
                            )
                            detail += f"; diagnostic: {diagnostic.name}"
                        except Exception:
                            pass
                    self.log(f"Slot {slot}: FAILED — {detail}")
                    self.errors.append(f"Slot {slot}: {detail}")
                    self.results.append(
                        SlotResult(
                            slot=slot,
                            status="failed",
                            day=None,
                            seconds=round(self.clock() - slot_started, 2),
                            detail=detail,
                        )
                    )
                    for pending_slot in self.slots[index + 1 :]:
                        self.results.append(
                            SlotResult(
                                slot=pending_slot,
                                status="not_attempted",
                                day=None,
                                seconds=0.0,
                                detail="Run stopped after an unrecognized or unsafe game state",
                            )
                        )
                    break
                else:
                    self.results.append(result)
                    remaining = len(self.slots) - index - 1
                    self.log(f"Slot {slot}: complete ({remaining} remaining)")
                finally:
                    if self.results:
                        save_partial_report(self.make_report(started_utc, started))
        except (RunStopped, KeyboardInterrupt):
            self.stop_event.set()
            self.errors.append("Run stopped by user")
            self.append_unattempted("Run stopped by user")
            self.log("Run stopped")
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            self.errors.append(detail)
            self.append_unattempted("Run stopped before this slot could be attempted")
            self.log(f"RUN FAILED — {detail}")
        finally:
            # Cleanup still needs Roblox in front to tidy up, so it takes focus back.
            self._focus_stop_armed = False
            if getattr(self, "_original_priority", None) is not None and self.roblox_handle:
                try:
                    if not ctypes.windll.kernel32.SetPriorityClass(
                        self.roblox_handle, self._original_priority
                    ):
                        raise RuntimeError("Windows refused the priority restore")
                    self.log("Restored Roblox CPU priority")
                except Exception as error:
                    detail = f"CPU priority restore failed: {type(error).__name__}: {error}"
                    self.errors.append(detail)
                    self.log(detail)
                self._original_priority = None
            if getattr(self, "_shared_code_may_be_visible", False):
                try:
                    self.clear_visible_shared_code()
                except Exception as error:
                    detail = (
                        "Shared-code field cleanup could not be confirmed: "
                        f"{type(error).__name__}: {error}"
                    )
                    self.errors.append(detail)
                    self.log(detail)
            if getattr(self, "_graphics_restore_level", None) and self.process_running():
                # Before any close: Roblox saves graphics at shutdown, so it keeps the restored level.
                try:
                    self.require_focus()
                    self.restore_graphics_in_game()
                except Exception as error:
                    detail = (
                        f"Graphics restore failed ({type(error).__name__}: {error}); "
                        "the next run puts it back, or press F10 in Roblox"
                    )
                    self.errors.append(detail)
                    self.log(detail)
            # Close before restoring audio when requested. Otherwise shutdown can
            # emit a brief unmuted burst between restoration and process exit.
            try:
                self.close_then_restore_audio()
            finally:
                if audio_com is not None:
                    try:
                        audio_com.CoUninitialize()
                    except Exception as error:
                        detail = f"Audio COM cleanup failed: {type(error).__name__}: {error}"
                        self.errors.append(detail)
                        self.log(detail)
            if not self.close_when_done and self.process_running():
                # Default: the last slot ends on the main menu, ready to play.
                self.log("Leaving Roblox open")
            if self.capture is not None:
                try:
                    self.capture.close()
                except Exception as error:
                    detail = f"Capture cleanup failed: {type(error).__name__}: {error}"
                    self.errors.append(detail)
                    self.log(detail)
            if (
                self._window_restore is not None
                and not self.close_when_done
                and self.process_running()
                and self.geometry is not None
            ):
                try:
                    restore_roblox_window(
                        self._window_restore,
                        self.geometry,
                        self.input,
                        self.require_focus,
                    )
                    self.log("Restored the original Roblox window")
                except Exception as error:
                    detail = f"Window restore failed: {type(error).__name__}: {error}"
                    self.errors.append(detail)
                    self.log(detail)
            if display_awake:
                try:
                    release_display_awake()
                except Exception as error:
                    detail = f"Power cleanup failed: {type(error).__name__}: {error}"
                    self.errors.append(detail)
                    self.log(detail)
            if self.roblox_handle:
                ctypes.windll.kernel32.CloseHandle(self.roblox_handle)
                self.roblox_handle = None
            if self.run_mutex:
                ctypes.windll.kernel32.ReleaseMutex(self.run_mutex)
                ctypes.windll.kernel32.CloseHandle(self.run_mutex)
                self.run_mutex = None

        report = self.make_report(started_utc, started)
        try:
            save_report(report)
        except OSError as error:
            message = f"Report could not be saved: {type(error).__name__}: {error}"
            report.errors.append(message)
            self.log(message)
        return report


def _load_claim_state_unlocked() -> dict[str, dict[str, object]]:
    if not CLAIM_STATE_PATH.exists():
        return {}
    try:
        state = json.loads(CLAIM_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        backup = CLAIM_STATE_PATH.with_name(f"claim_state.corrupt_{stamp}.json")
        CLAIM_STATE_PATH.replace(backup)
        raise RuntimeError(
            f"Claim state was corrupt and was preserved as {backup.name}"
        ) from error
    if not isinstance(state, dict) or not all(
        isinstance(key, str) and isinstance(record, dict)
        for key, record in state.items()
    ):
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        backup = CLAIM_STATE_PATH.with_name(f"claim_state.corrupt_{stamp}.json")
        CLAIM_STATE_PATH.replace(backup)
        raise RuntimeError(
            f"Claim state had an invalid structure and was preserved as {backup.name}"
        )
    return state


def load_claim_state() -> dict[str, dict[str, object]]:
    with exclusive_file_lock(STATE_LOCK_PATH):
        return _load_claim_state_unlocked()


def _write_claim_state_unlocked(state: dict[str, dict[str, object]]) -> None:
    temporary = CLAIM_STATE_PATH.with_name(
        f".{CLAIM_STATE_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as state_file:
            json.dump(state, state_file, indent=2)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, CLAIM_STATE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def pending_claim_day(record: object) -> int | None:
    if not isinstance(record, dict):
        return None
    pending = record.get("pending_claim")
    if not isinstance(pending, dict):
        return None
    day = pending.get("day")
    return day if type(day) is int and 1 <= day <= 14 else None


def mark_pending_claim(slot: int, day: int) -> None:
    """Atomically preserve evidence immediately before the irreversible click."""
    if not 1 <= slot <= 12 or not 1 <= day <= 14:
        raise ValueError("Pending claim slot/day is out of range")
    with exclusive_file_lock(STATE_LOCK_PATH):
        state = _load_claim_state_unlocked()
        record = dict(state.get(str(slot), {}))
        record["pending_claim"] = {
            "day": day,
            "started_utc": datetime.now(timezone.utc).isoformat(),
        }
        state[str(slot)] = record
        _write_claim_state_unlocked(state)


def clear_pending_claim(slot: int) -> None:
    """Remove only a provably unsent intent, preserving the prior claim record."""
    with exclusive_file_lock(STATE_LOCK_PATH):
        state = _load_claim_state_unlocked()
        record = state.get(str(slot), {})
        if "pending_claim" in record:
            record.pop("pending_claim")
            _write_claim_state_unlocked(state)


def update_claim_state(
    slot: int,
    day: int,
    verification: str = "unverified",
    received_reward: str | None = None,
    claimed_utc: str | None = None,
) -> None:
    next_day = 1 if day == 14 else day + 1
    with exclusive_file_lock(STATE_LOCK_PATH):
        state = _load_claim_state_unlocked()
        state[str(slot)] = {
            "claimed_day": day,
            "expected_reward": DAILY_REWARDS[day],
            "reward": DAILY_REWARDS[day],
            "verification": verification,
            "received_reward": received_reward,
            "next_day": next_day,
            "next_reward": DAILY_REWARDS[next_day],
            "claimed_utc": claimed_utc or datetime.now(timezone.utc).isoformat(),
        }
        _write_claim_state_unlocked(state)


def render_text_report(report: RunReport) -> str:
    lines = [
        f"{APP_NAME} DBOG Daily Claimer run report",
        f"Version:      {report.app_version}",
        f"Server:       {report.server_profile} ({report.access_mode})",
        f"Slot count:   {report.configured_slot_count}",
        f"Started UTC:  {report.started_utc}",
        f"Finished UTC: {report.finished_utc}",
        f"Total time:   {report.total_seconds:.2f}s",
        "",
    ]
    for slot in report.slots:
        if slot.status == "claimed_unverified":
            summary = (
                f"CLAIMED (UNVERIFIED) Day {slot.day}: expected {slot.reward}; "
                f"next Day {slot.next_day}: {slot.next_reward}"
            )
        elif slot.status == "claimed_verified":
            summary = (
                f"CLAIMED Day {slot.day}: received {slot.received_reward}; "
                f"next Day {slot.next_day}: {slot.next_reward}"
            )
        elif slot.status == "claimed_global_locked":
            summary = (
                f"CLAIMED Day {slot.day}: GLOBAL REWARD LOCKED; "
                f"received {slot.received_reward}, expected {slot.reward}; "
                f"next Day {slot.next_day}: {slot.next_reward}"
            )
        elif slot.status == "no_daily":
            if slot.day:
                summary = (
                    f"NO DAILY; last recorded Day {slot.day}: {slot.reward}; "
                    f"next Day {slot.next_day}: {slot.next_reward}"
                )
            else:
                summary = "NO DAILY; streak day not recorded yet"
        elif slot.status == "inspected":
            summary = f"INSPECTED Day {slot.day}: {slot.reward} (not claimed)"
        elif slot.status == "held":
            summary = f"HELD — {slot.detail}"
        elif slot.status == "not_attempted":
            summary = f"NOT ATTEMPTED — {slot.detail}"
        elif slot.status == "slot_missing":
            summary = f"SLOT NOT AVAILABLE — {slot.detail}"
        elif slot.status == "server_code_invalid":
            summary = f"SERVER CODE REJECTED — {slot.detail}"
        else:
            summary = f"FAILED — {slot.detail}"
        if slot.server_route == "public_fallback":
            summary += " [PUBLIC FALLBACK]"
        lines.append(f"Slot {slot.slot:>2}: {summary} ({slot.seconds:.2f}s)")
    if report.errors:
        lines.extend(["", "Errors:", *[f"- {error}" for error in report.errors]])
    return "\n".join(lines) + "\n"


def partial_report_path(report: RunReport) -> Path:
    stamp = re.sub(r"[^a-zA-Z0-9_-]", "_", report.started_utc)
    return REPORT_DIR / f"partial_run_{stamp}.json"


def save_partial_report(report: RunReport) -> None:
    """Keep completed slots durable without adding a second run to history."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    payload["in_progress"] = True
    with exclusive_file_lock(REPORT_DIR / ".reports.lock"):
        atomic_write_text(partial_report_path(report), json.dumps(payload, indent=2) + "\n")


def save_report(report: RunReport) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    json_path = REPORT_DIR / f"run_{stamp}.json"
    text_path = REPORT_DIR / f"run_{stamp}.txt"

    payload = asdict(report)
    with exclusive_file_lock(REPORT_DIR / ".reports.lock"):
        atomic_write_text(json_path, json.dumps(payload, indent=2) + "\n")
        atomic_write_text(text_path, render_text_report(report))
        with exclusive_file_lock(HISTORY_LOCK_PATH):
            with HISTORY_PATH.open("a+b") as history:
                if history.tell():
                    history.seek(-1, os.SEEK_END)
                    if history.read(1) != b"\n":
                        history.write(b"\n")
                history.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
                history.flush()
                os.fsync(history.fileno())
            trim_history_file()
            partial_report_path(report).unlink(missing_ok=True)
        prune_report_files("run_*.json", MAX_RUN_REPORTS)
        prune_report_files("run_*.txt", MAX_RUN_REPORTS)
        prune_report_files("partial_run_*.json", MAX_RUN_REPORTS)
    return json_path, text_path


def parse_slots(value: str) -> list[int]:
    slots: set[int] = set()
    try:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start, end = int(start_text), int(end_text)
                if start > end:
                    raise argparse.ArgumentTypeError(
                        "Slot ranges must go from lower to higher"
                    )
                slots.update(range(start, end + 1))
            else:
                slots.add(int(part))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Slots must be numbers or ranges such as 1,3-5"
        ) from error
    invalid = sorted(slot for slot in slots if slot < 1 or slot > 12)
    if invalid or not slots:
        raise argparse.ArgumentTypeError("Slots must be between 1 and 12")
    return sorted(slots)


def console_log(message: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with LIVE_LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")
    print(line, flush=True)


def main() -> int:
    migrate_legacy_data()
    parser = argparse.ArgumentParser(
        prog="python -m xynclaim.engine",
        description="Claim Dragon Generations dailies by slot",
    )
    parser.add_argument("--run", action="store_true", help="Run without the GUI")
    parser.add_argument("--slots", type=parse_slots, default=list(range(1, 13)))
    parser.add_argument(
        "--slot-count",
        type=int,
        choices=range(1, 13),
        default=12,
        metavar="1-12",
        help="Number of character slots available on this account",
    )
    parser.add_argument(
        "--close-when-done",
        action="store_true",
        help="Close Roblox when the run ends (default: leave it on the main menu)",
    )
    parser.add_argument(
        "--public-fallback",
        action="store_true",
        help="Use a public server only after private access is explicitly rejected",
    )
    parser.add_argument(
        "--shared-code-stdin",
        action="store_true",
        help="Read a shared server code from one stdin line without exposing it in process arguments",
    )
    parser.add_argument(
        "--min-claim-day",
        type=int,
        choices=range(1, 15),
        default=1,
        metavar="1-14",
        help="Minimum visible daily streak day required to claim (default: 1)",
    )
    parser.add_argument(
        "--max-claim-day", type=int, choices=range(1, 15), default=14,
        help="Maximum effective streak day allowed to claim (default: 14)",
    )
    parser.add_argument(
        "--inspect",
        "--check-only",
        action="store_true",
        dest="inspect_only",
        help="Inspect every slot's daily streak day without claiming any rewards",
    )
    parser.add_argument(
        "--align",
        action="store_true",
        help="Use automatic sequence planning to align slots with their target slot (slot 1 by default)",
    )
    args = parser.parse_args()

    if args.run:
        unavailable = [slot for slot in args.slots if slot > args.slot_count]
        if unavailable:
            parser.error(
                f"selected slots exceed --slot-count {args.slot_count}: "
                + ", ".join(map(str, unavailable))
            )
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        server_code = ""
        access_mode = "owner"
        server_profile = "My private server"
        if args.shared_code_stdin:
            server_code = normalize_server_code(sys.stdin.readline())
            access_mode = "shared"
            server_profile = "Shared private server"
        run_display = "primary"
        mute_roblox = False
        low_graphics = False
        low_cpu_priority = False
        slot_mismatch_policies = None
        settings = None
        try:
            from xynclaim.settings import load_settings

            settings = load_settings(SETTINGS_PATH, args.slot_count)
            run_display = settings.run_monitor
            mute_roblox = settings.mute_roblox
            low_graphics = settings.low_graphics
            low_cpu_priority = settings.low_cpu_priority
            slot_mismatch_policies = {
                slot: settings.sequence_modes.get(slot, "always")
                for slot in args.slots
            }
        except Exception as error:
            parser.error(f"Cannot load run settings: {error}")

        slots_to_run = args.slots
        if args.align and settings is not None:
            from xynclaim.settings import plan_sequence_claims

            decision = plan_sequence_claims(settings, args.slots, load_claim_state())
            due_slots = list(decision.due_slots)
            if not due_slots:
                console_log(
                    f"Sequence planner: no selected slots are due today (held: {', '.join(map(str, decision.held_slots)) or 'none'})"
                )
                return 0
            console_log(
                f"Sequence planner: due = {', '.join(map(str, due_slots))}, held = {', '.join(map(str, decision.held_slots)) or 'none'}"
            )
            slots_to_run = due_slots

        report = DBOGRunner(
            slots_to_run,
            console_log,
            close_when_done=args.close_when_done,
            configured_slot_count=args.slot_count,
            access_mode=access_mode,
            server_code=server_code,
            server_profile=server_profile,
            allow_public_fallback=args.public_fallback,
            run_display=run_display,
            mute_roblox=mute_roblox,
            low_graphics=low_graphics,
            low_cpu_priority=low_cpu_priority,
            slot_mismatch_policies=slot_mismatch_policies,
            sequence_align_targets=settings.sequence_align_targets,
            min_claim_day=args.min_claim_day,
            max_claim_day=args.max_claim_day,
            inspect_only=args.inspect_only,
        ).run()
        print(json.dumps(asdict(report), indent=2))
        return 1 if report.errors else 0

    # Avoid importing this script a second time under its module name when the
    # bridge imports the engine from a direct `python -m xynclaim.engine` run.
    sys.modules.setdefault("xynclaim.engine", sys.modules[__name__])
    from xynclaim.bridge import launch_webview

    launch_webview()
    return 0


if __name__ == "__main__":
    sys.exit(main())
