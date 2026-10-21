"""A configurable global start/stop hotkey on its own Windows message thread."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import queue
import threading
from typing import Callable

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_UPDATE_HOTKEY = 0x8001
WM_QUIT = 0x0012
MODIFIERS = {"Ctrl": MOD_CONTROL, "Alt": MOD_ALT, "Shift": MOD_SHIFT}
FUNCTION_KEYS = {f"F{number}": 0x6F + number for number in range(1, 13)}


def parse_hotkey(key: str) -> tuple[str, int, int]:
    """Return the display name, Win32 modifiers and virtual key (empty means off)."""
    error = "Hotkey must be empty, F1-F12, or Ctrl/Alt/Shift plus A-Z, 0-9 or F1-F12"
    if not isinstance(key, str) or not key.isascii():
        raise ValueError(error)
    if not key.strip():
        return "", 0, 0
    parts = [part.strip().upper() for part in key.split("+")]
    modifiers = parts[:-1]
    names = {name.upper(): name for name in MODIFIERS}
    if any(part not in names for part in modifiers) or len(set(modifiers)) != len(modifiers):
        raise ValueError(error + "; modifiers must not be repeated")
    final = parts[-1]
    if final in FUNCTION_KEYS:
        virtual_key = FUNCTION_KEYS[final]
    elif modifiers and len(final) == 1 and final in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        virtual_key = ord(final)
    else:
        raise ValueError(error)
    ordered = [name for name in MODIFIERS if name.upper() in modifiers]
    flags = MOD_NOREPEAT | sum(MODIFIERS[name] for name in ordered)
    return "+".join([*ordered, final]), flags, virtual_key


def normalize_hotkey(key: str) -> str:
    return parse_hotkey(key)[0]


class HotkeyListener:
    def __init__(self, on_press: Callable[[], None]) -> None:
        self.label = ""
        self._hotkey_id = 0
        self._on_press = on_press
        self._ready = threading.Event()
        self._change_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._stopped = False
        self._requests: queue.Queue = queue.Queue()
        self._thread_id = 0
        self._thread = threading.Thread(target=self._listen, name="XynClaimHotkey", daemon=True)
        self._thread.start()
        self._ready.wait()

    def change(self, key: str) -> bool:
        binding = parse_hotkey(key)
        with self._change_lock:
            completed = threading.Event()
            result: list[bool] = []
            with self._request_lock:
                if self._stopped:
                    raise RuntimeError("The global hotkey listener is unavailable")
                self._requests.put((binding, completed, result))
                if not ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_UPDATE_HOTKEY, 0, 0):
                    self._requests.get_nowait()
                    raise RuntimeError("Couldn't update the global hotkey listener")
            completed.wait()
            return result[0]

    def close(self) -> None:
        with self._change_lock:
            if self._thread.is_alive():
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
                self._thread.join(2)

    def _replace(self, binding: tuple[str, int, int]) -> bool:
        label, modifiers, virtual_key = binding
        if label == self.label:
            return True
        user32 = ctypes.windll.user32
        # Reserve the replacement first, so a conflict never drops the working key.
        new_id = 2 if self._hotkey_id == 1 else 1
        if label and not user32.RegisterHotKey(None, new_id, modifiers, virtual_key):
            return False
        if self._hotkey_id and not user32.UnregisterHotKey(None, self._hotkey_id):
            if label:
                user32.UnregisterHotKey(None, new_id)
            return False
        self._hotkey_id = new_id if label else 0
        self.label = label
        return True

    def _listen(self) -> None:
        user32 = ctypes.windll.user32
        message = wintypes.MSG()
        try:
            self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            # PostThreadMessage needs this thread's message queue to exist first.
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self._ready.set()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_UPDATE_HOTKEY:
                    binding, completed, result = self._requests.get_nowait()
                    try:
                        result.append(self._replace(binding))
                    finally:
                        if not result:
                            result.append(False)
                        completed.set()
                elif message.message == WM_HOTKEY and self._hotkey_id and message.wParam == self._hotkey_id:
                    try:
                        self._on_press()
                    except Exception:
                        pass  # A failed toggle must not kill the listener; the UI shows run errors.
        finally:
            if self._hotkey_id:
                user32.UnregisterHotKey(None, self._hotkey_id)
            self.label = ""
            with self._request_lock:
                self._stopped = True
                self._ready.set()
                while not self._requests.empty():
                    _, completed, result = self._requests.get_nowait()
                    result.append(False)
                    completed.set()
