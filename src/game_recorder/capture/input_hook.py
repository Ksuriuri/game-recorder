"""Keyboard and mouse capture via Win32 low-level hooks (ctypes).

Uses SetWindowsHookExW with WH_KEYBOARD_LL / WH_MOUSE_LL to capture
global input events with high-precision timestamps relative to a shared
T0 epoch, enabling frame-accurate alignment with the video stream.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# ── Win32 constants ──────────────────────────────────────────────────────────

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C

_MOUSE_ACTION_MAP: dict[int, str] = {
    WM_LBUTTONDOWN: "left_down",
    WM_LBUTTONUP: "left_up",
    WM_RBUTTONDOWN: "right_down",
    WM_RBUTTONUP: "right_up",
    WM_MBUTTONDOWN: "middle_down",
    WM_MBUTTONUP: "middle_up",
    WM_MOUSEWHEEL: "scroll",
    WM_XBUTTONDOWN: "x_down",
    WM_XBUTTONUP: "x_up",
    WM_MOUSEMOVE: "move",
}

# ── Win32 structures ─────────────────────────────────────────────────────────


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p
)

user32 = ctypes.windll.user32  # type: ignore[attr-defined]
kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

EventCallback = Callable[[dict], None]

# ── Virtual-key to readable name ─────────────────────────────────────────────

_VK_NAMES: dict[int, str] = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x10: "Shift",
    0x11: "Ctrl", 0x12: "Alt", 0x14: "CapsLock", 0x1B: "Esc",
    0x20: "Space", 0x21: "PageUp", 0x22: "PageDown", 0x23: "End",
    0x24: "Home", 0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2D: "Insert", 0x2E: "Delete",
    0x5B: "LWin", 0x5C: "RWin",
    0xA0: "LShift", 0xA1: "RShift", 0xA2: "LCtrl", 0xA3: "RCtrl",
    0xA4: "LAlt", 0xA5: "RAlt",
}

for _i in range(10):  # 0-9
    _VK_NAMES[0x30 + _i] = str(_i)
for _i in range(26):  # A-Z
    _VK_NAMES[0x41 + _i] = chr(0x41 + _i)
for _i in range(12):  # F1-F12
    _VK_NAMES[0x70 + _i] = f"F{_i + 1}"


def _vk_to_name(vk: int) -> str:
    return _VK_NAMES.get(vk, f"0x{vk:02X}")


# ── InputCapture ─────────────────────────────────────────────────────────────


class InputCapture:
    """Captures keyboard and mouse events using Win32 low-level hooks.

    Each emitted event carries a ``frame`` index computed from the shared
    T0 epoch and the target FPS, so events are pre-aligned to video frames
    for downstream per-frame bucketing.

    Parameters
    ----------
    t0_ns:
        ``time.perf_counter_ns()`` epoch — all event timestamps are relative
        to this value so they align with the video stream.
    fps:
        Target video frame rate.  Used to bucket every event into a frame
        index via ``frame = int((now_ns - t0_ns) * fps / 1e9)``.
    on_event:
        ``(event_dict) -> None`` called for every captured event.
    mouse_throttle_ms:
        Minimum interval between mouse-move events (default 5 ms = 200 Hz).
    """

    def __init__(
        self,
        t0_ns: int,
        fps: int,
        on_event: EventCallback,
        mouse_throttle_ms: float = 5.0,
    ) -> None:
        self._t0_ns = t0_ns
        self._fps = fps
        self._on_event = on_event
        self._mouse_throttle_ns = int(mouse_throttle_ms * 1_000_000)
        self._last_mouse_move_ns: int = 0
        self._kb_hook = None
        self._mouse_hook = None
        self._event_count = 0

        # Must prevent GC of the HOOKPROC pointers
        self._kb_proc = HOOKPROC(self._keyboard_ll_proc)
        self._mouse_proc = HOOKPROC(self._mouse_ll_proc)

    def run(self, stop_event: threading.Event) -> None:
        """Install hooks and run a message pump.  Blocking — run in a thread."""
        self._kb_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._kb_proc, kernel32.GetModuleHandleW(None), 0
        )
        self._mouse_hook = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._mouse_proc, kernel32.GetModuleHandleW(None), 0
        )
        if not self._kb_hook or not self._mouse_hook:
            logger.error("Failed to install input hooks")
            return

        logger.info("Input hooks installed (keyboard + mouse)")

        msg = wt.MSG()
        try:
            while not stop_event.is_set():
                # PeekMessage with a short timeout to stay responsive to stop_event
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.001)
        finally:
            if self._kb_hook:
                user32.UnhookWindowsHookEx(self._kb_hook)
            if self._mouse_hook:
                user32.UnhookWindowsHookEx(self._mouse_hook)
            logger.info("Input hooks removed (%d events captured)", self._event_count)

    # ── Hook procedures ──────────────────────────────────────────────────

    def _frame_index(self, now_ns: int) -> int:
        """Convert a perf-counter timestamp to a 0-based video-frame index."""
        delta_ns = now_ns - self._t0_ns
        if delta_ns < 0:
            return 0
        return (delta_ns * self._fps) // 1_000_000_000

    def _keyboard_ll_proc(
        self, nCode: int, wParam: int, lParam: int  # noqa: N803
    ) -> int:
        if nCode >= 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            frame = self._frame_index(time.perf_counter_ns())
            action = "down" if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN) else "up"
            event = {
                "frame": int(frame),
                "type": "key",
                "action": action,
                "vk": kb.vkCode,
                "key": _vk_to_name(kb.vkCode),
            }
            self._emit(event)
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _mouse_ll_proc(
        self, nCode: int, wParam: int, lParam: int  # noqa: N803
    ) -> int:
        if nCode >= 0:
            now_ns = time.perf_counter_ns()
            ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents

            # Throttle mouse-move to avoid flooding
            if wParam == WM_MOUSEMOVE:
                if (now_ns - self._last_mouse_move_ns) < self._mouse_throttle_ns:
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                self._last_mouse_move_ns = now_ns

            frame = self._frame_index(now_ns)
            action = _MOUSE_ACTION_MAP.get(wParam, f"unknown_{wParam:#x}")
            event: dict = {
                "frame": int(frame),
                "type": "mouse",
                "action": action,
                "x": ms.pt.x,
                "y": ms.pt.y,
            }
            if wParam == WM_MOUSEWHEEL:
                # High word of mouseData is the wheel delta
                delta = ctypes.c_short(ms.mouseData >> 16).value
                event["scroll_delta"] = delta
            self._emit(event)
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _emit(self, event: dict) -> None:
        self._event_count += 1
        self._on_event(event)
