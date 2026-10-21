"""Windows identity and icons for the source and packaged app."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import sys


def set_app_identity() -> None:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Xyn.XynClaim")


def icon_path() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return root / "packaging" / "xynclaim.ico"


def set_window_icons(hwnd: int) -> list[int]:
    if not hwnd or not icon_path().is_file():
        return []
    user32 = ctypes.windll.user32
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    icons = []
    for kind, metric in ((1, 11), (0, 49)):  # ICON_BIG / ICON_SMALL, SM_CXICON / SM_CXSMICON
        size = user32.GetSystemMetrics(metric)
        icon = user32.LoadImageW(None, str(icon_path()), 1, size, size, 0x10)
        if icon:
            user32.SendMessageW(hwnd, 0x80, kind, icon)
            icons.append(icon)
    # WM_SETICON borrows these handles; keep them alive until the window closes.
    return icons


def round_corners(hwnd: int) -> None:
    """Ask Windows 11 to round the frameless window; older Windows ignores the attribute."""
    if not hwnd:
        return
    corner_preference = ctypes.c_int(2)  # DWMWCP_ROUND
    # DWMWA_WINDOW_CORNER_PREFERENCE = 33
    ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(corner_preference),
                                               ctypes.sizeof(corner_preference))


def release_icons(icons: list[int]) -> None:
    user32 = ctypes.windll.user32
    user32.DestroyIcon.argtypes = [wintypes.HANDLE]
    for icon in icons:
        user32.DestroyIcon(icon)
