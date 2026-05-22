"""Keyboard and mouse capture on Windows (ctypes).

Mouse + keyboard: **Raw Input** (``WM_INPUT`` + ``RIDEV_INPUTSINK``) when possible — it sees keys
in Task Manager, UWP, and games where ``GetAsyncKeyState`` misses letters (e.g. WASD).

Falls back to ``GetAsyncKeyState`` keyboard polling if Raw Input setup fails; mouse-button
VKs (0x01–0x06) are never logged as keys in that mode.
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

WM_INPUT = 0x00FF

RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RI_KEY_BREAK = 1
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001

RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
RI_MOUSE_LEFT_BUTTON_UP = 0x0002
RI_MOUSE_RIGHT_BUTTON_DOWN = 0x0004
RI_MOUSE_RIGHT_BUTTON_UP = 0x0008
RI_MOUSE_MIDDLE_BUTTON_DOWN = 0x0010
RI_MOUSE_MIDDLE_BUTTON_UP = 0x0020
RI_MOUSE_BUTTON_4_DOWN = 0x0040
RI_MOUSE_BUTTON_4_UP = 0x0080
RI_MOUSE_BUTTON_5_DOWN = 0x0100
RI_MOUSE_BUTTON_5_UP = 0x0200
RI_MOUSE_WHEEL = 0x0400
MOUSE_MOVE_ABSOLUTE = 0x0001

# MsgWaitForMultipleObjects
QS_INPUT = 0x0407

PM_REMOVE = 0x0001

# Do not treat these as keyboard (GetAsyncKeyState mirrors mouse buttons as VKs).
_SKIP_ASYNC_VK: frozenset[int] = frozenset((0x01, 0x02, 0x04, 0x05, 0x06))

_RAW_MOUSE_BUTTON_ACTIONS: tuple[tuple[int, str], ...] = (
    (RI_MOUSE_LEFT_BUTTON_DOWN, "left_down"),
    (RI_MOUSE_LEFT_BUTTON_UP, "left_up"),
    (RI_MOUSE_RIGHT_BUTTON_DOWN, "right_down"),
    (RI_MOUSE_RIGHT_BUTTON_UP, "right_up"),
    (RI_MOUSE_MIDDLE_BUTTON_DOWN, "middle_down"),
    (RI_MOUSE_MIDDLE_BUTTON_UP, "middle_up"),
    (RI_MOUSE_BUTTON_4_DOWN, "x_down"),
    (RI_MOUSE_BUTTON_4_UP, "x_up"),
    (RI_MOUSE_BUTTON_5_DOWN, "x2_down"),
    (RI_MOUSE_BUTTON_5_UP, "x2_up"),
)

_RAW_INPUT_DEVICES = (
    (0x01, 0x02),  # mouse
    (0x01, 0x06),  # keyboard
)


class _RAWMOUSE_BUTTONS_STRUCT(ctypes.Structure):
    _fields_ = [
        ("usButtonFlags", wt.USHORT),
        ("usButtonData", wt.USHORT),
    ]


class _RAWMOUSE_BUTTONS_UNION(ctypes.Union):
    _fields_ = [
        ("ulButtons", wt.DWORD),
        ("buttons", _RAWMOUSE_BUTTONS_STRUCT),
    ]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("button_union",)
    _fields_ = [
        ("usFlags", wt.USHORT),
        ("button_union", _RAWMOUSE_BUTTONS_UNION),
        ("ulRawButtons", wt.DWORD),
        ("lLastX", wt.LONG),
        ("lLastY", wt.LONG),
        ("ulExtraInformation", wt.DWORD),
    ]

class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wt.DWORD),
        ("dwSize", wt.DWORD),
        ("hDevice", wt.HANDLE),
        ("wParam", ctypes.c_size_t),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", wt.USHORT),
        ("Flags", wt.USHORT),
        ("Reserved", wt.USHORT),
        ("VKey", wt.USHORT),
        ("Message", wt.UINT),
        ("ExtraInformation", ctypes.c_size_t),
    ]


class RAWINPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD)]

    _fields_ = [("header", RAWINPUTHEADER), ("data", _U)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wt.USHORT),
        ("usUsage", wt.USHORT),
        ("dwFlags", wt.DWORD),
        ("hwndTarget", wt.HWND),
    ]


# Win64: WPARAM/LPARAM/LRESULT are pointer-sized.
_LRESULT = ctypes.c_ssize_t
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wt.HWND, ctypes.c_uint, _WPARAM, _LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HICON),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]

user32 = ctypes.windll.user32  # type: ignore[attr-defined]
kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wt.SHORT

user32.MsgWaitForMultipleObjects.argtypes = [
    wt.DWORD,
    ctypes.c_void_p,
    wt.BOOL,
    wt.DWORD,
    wt.DWORD,
]
user32.MsgWaitForMultipleObjects.restype = wt.DWORD

user32.GetRawInputData.argtypes = [
    wt.HANDLE,
    wt.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(wt.UINT),
    wt.UINT,
]
user32.GetRawInputData.restype = wt.UINT

user32.RegisterRawInputDevices.argtypes = [
    ctypes.POINTER(RAWINPUTDEVICE),
    wt.UINT,
    wt.UINT,
]
user32.RegisterRawInputDevices.restype = wt.BOOL

user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, _WPARAM, _LPARAM]
user32.DefWindowProcW.restype = _LRESULT

user32.CreateWindowExW.argtypes = [
    wt.DWORD,
    wt.LPCWSTR,
    wt.LPCWSTR,
    wt.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wt.HWND,
    wt.HMENU,
    wt.HINSTANCE,
    ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wt.HWND

user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM

user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
user32.UnregisterClassW.restype = wt.BOOL

HWND_MESSAGE = wt.HWND(-3)  # HWND_MESSAGE — parent for message-only windows

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
    """Captures keyboard and mouse with Raw Input when possible."""

    def __init__(
        self,
        t0_ns: int,
        fps: int,
        on_event: EventCallback,
        mouse_throttle_ms: float = 5.0,
        keyboard_poll_hz: float = 200.0,
    ) -> None:
        self._t0_ns = t0_ns
        self._fps = fps
        self._on_event = on_event
        self._keyboard_poll_hz = max(30.0, float(keyboard_poll_hz))
        self._key_poll_interval_ns = int(1_000_000_000 / self._keyboard_poll_hz)
        self._last_key_poll_ns: int = 0
        self._async_key_prev: list[bool] = [False] * 256
        self._mouse_throttle_ns = int(mouse_throttle_ms * 1_000_000)
        self._last_mouse_move_ns: int = 0
        self._event_count = 0
        self._key_events = 0
        self._mouse_events = 0

        self._use_raw_input = False
        self._raw_hwnd: wt.HWND | None = None
        self._raw_class_name: str | None = None
        self._raw_hinstance: wt.HINSTANCE | None = None
        self._wnd_proc_ref: ctypes._CFuncPtr | None = None

    def run(self, stop_event: threading.Event) -> None:
        self._use_raw_input = self._setup_raw_input()
        if self._use_raw_input:
            logger.info("Input capture started (keyboard + mouse Raw Input)")
        else:
            logger.warning(
                "Raw Input unavailable — using GetAsyncKeyState keyboard polling @ %.0f Hz "
                "(mouse events disabled; try restarting as admin)",
                self._keyboard_poll_hz,
            )
            for vk in range(1, 256):
                self._async_key_prev[vk] = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            self._last_key_poll_ns = time.perf_counter_ns()

        msg = wt.MSG()
        try:
            while not stop_event.is_set():
                if not self._use_raw_input:
                    self._poll_keyboard_async()
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    user32.MsgWaitForMultipleObjects(0, None, 0, 10, QS_INPUT)
        finally:
            self._teardown_raw_input()
            logger.info(
                "Input capture stopped (%d events: %d key, %d mouse)",
                self._event_count,
                self._key_events,
                self._mouse_events,
            )

    # ── Raw input ──────────────────────────────────────────────────────────────

    def _setup_raw_input(self) -> bool:
        hinst = kernel32.GetModuleHandleW(None)
        if not hinst:
            return False

        class_name = f"GameRecorderRawInput_{kernel32.GetCurrentProcessId()}"
        self._raw_hinstance = hinst
        self._raw_class_name = class_name

        @WNDPROC
        def _wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                self._handle_wm_input(lparam)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wnd_proc_ref = _wnd_proc

        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(_wnd_proc, ctypes.c_void_p)
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = class_name

        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = kernel32.GetLastError()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                logger.debug("RegisterClassW failed: GetLastError=%s", err)
                return False

        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "GameRecorderInputSink",
            0,
            0,
            0,
            0,
            0,
            HWND_MESSAGE,
            None,
            hinst,
            None,
        )
        if not hwnd:
            logger.debug("CreateWindowExW failed: GetLastError=%s", kernel32.GetLastError())
            user32.UnregisterClassW(class_name, hinst)
            self._raw_class_name = None
            return False

        self._raw_hwnd = hwnd

        devices = (RAWINPUTDEVICE * len(_RAW_INPUT_DEVICES))()
        for idx, (usage_page, usage) in enumerate(_RAW_INPUT_DEVICES):
            devices[idx].usUsagePage = usage_page
            devices[idx].usUsage = usage
            devices[idx].dwFlags = RIDEV_INPUTSINK
            devices[idx].hwndTarget = hwnd

        if not user32.RegisterRawInputDevices(
            ctypes.cast(devices, ctypes.POINTER(RAWINPUTDEVICE)),
            len(devices),
            ctypes.sizeof(RAWINPUTDEVICE),
        ):
            logger.debug(
                "RegisterRawInputDevices failed: GetLastError=%s",
                kernel32.GetLastError(),
            )
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(class_name, hinst)
            self._raw_hwnd = None
            self._raw_class_name = None
            return False

        return True

    def _teardown_raw_input(self) -> None:
        if self._raw_hwnd and self._raw_hinstance:
            devices = (RAWINPUTDEVICE * len(_RAW_INPUT_DEVICES))()
            for idx, (usage_page, usage) in enumerate(_RAW_INPUT_DEVICES):
                devices[idx].usUsagePage = usage_page
                devices[idx].usUsage = usage
                devices[idx].dwFlags = RIDEV_REMOVE
                devices[idx].hwndTarget = None
            user32.RegisterRawInputDevices(
                ctypes.cast(devices, ctypes.POINTER(RAWINPUTDEVICE)),
                len(devices),
                ctypes.sizeof(RAWINPUTDEVICE),
            )
            user32.DestroyWindow(self._raw_hwnd)
            self._raw_hwnd = None
        if self._raw_class_name and self._raw_hinstance:
            user32.UnregisterClassW(self._raw_class_name, self._raw_hinstance)
            self._raw_class_name = None
        self._raw_hinstance = None
        self._wnd_proc_ref = None

    def _handle_wm_input(self, lparam: int) -> None:
        cb = wt.UINT(0)
        hdr_sz = ctypes.sizeof(RAWINPUTHEADER)
        hip = wt.HANDLE(ctypes.cast(lparam, ctypes.c_void_p).value)
        r = user32.GetRawInputData(hip, RID_INPUT, None, ctypes.byref(cb), hdr_sz)
        if r == -1 or cb.value == 0:
            return
        buf = (ctypes.c_byte * cb.value)()
        cb2 = wt.UINT(cb.value)
        if (
            user32.GetRawInputData(
                hip, RID_INPUT, ctypes.cast(buf, ctypes.c_void_p), ctypes.byref(cb2), hdr_sz
            )
            == -1
        ):
            return
        raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
        if raw.header.dwType == RIM_TYPEKEYBOARD:
            self._handle_raw_keyboard(raw.data.keyboard)
        elif raw.header.dwType == RIM_TYPEMOUSE:
            self._handle_raw_mouse(raw.data.mouse)

    def _handle_raw_keyboard(self, kb: RAWKEYBOARD) -> None:
        vk = int(kb.VKey)
        if vk == 0:
            return
        up = bool(kb.Flags & RI_KEY_BREAK)
        now_ns = time.perf_counter_ns()
        frame = self._frame_index(now_ns)
        self._emit(
            {
                "frame": int(frame),
                "type": "key",
                "action": "up" if up else "down",
                "vk": vk,
                "key": _vk_to_name(vk),
            }
        )

    def _handle_raw_mouse(self, mouse: RAWMOUSE) -> None:
        now_ns = time.perf_counter_ns()
        frame = self._frame_index(now_ns)
        button_flags = int(mouse.buttons.usButtonFlags)
        dx = int(mouse.lLastX)
        dy = int(mouse.lLastY)

        if dx or dy:
            if (now_ns - self._last_mouse_move_ns) >= self._mouse_throttle_ns:
                self._last_mouse_move_ns = now_ns
                event: dict = {
                    "frame": int(frame),
                    "type": "mouse",
                    "action": "move",
                }
                if mouse.usFlags & MOUSE_MOVE_ABSOLUTE:
                    event["x"] = dx
                    event["y"] = dy
                    event["absolute"] = True
                else:
                    event["dx"] = dx
                    event["dy"] = dy
                self._emit(event)

        for flag, action in _RAW_MOUSE_BUTTON_ACTIONS:
            if button_flags & flag:
                self._emit(
                    {
                        "frame": int(frame),
                        "type": "mouse",
                        "action": action,
                    }
                )

        if button_flags & RI_MOUSE_WHEEL:
            delta = ctypes.c_short(mouse.buttons.usButtonData).value
            self._emit(
                {
                    "frame": int(frame),
                    "type": "mouse",
                    "action": "scroll",
                    "scroll_delta": delta,
                }
            )

    # ── Keyboard fallback (async) ────────────────────────────────────────────

    def _poll_keyboard_async(self) -> None:
        now_ns = time.perf_counter_ns()
        if now_ns - self._last_key_poll_ns < self._key_poll_interval_ns:
            return
        self._last_key_poll_ns = now_ns
        frame = self._frame_index(now_ns)
        for vk in range(1, 256):
            if vk in _SKIP_ASYNC_VK:
                continue
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if down == self._async_key_prev[vk]:
                continue
            self._async_key_prev[vk] = down
            self._emit(
                {
                    "frame": int(frame),
                    "type": "key",
                    "action": "down" if down else "up",
                    "vk": vk,
                    "key": _vk_to_name(vk),
                }
            )

    # ── Shared ───────────────────────────────────────────────────────────────

    def _frame_index(self, now_ns: int) -> int:
        delta_ns = now_ns - self._t0_ns
        if delta_ns < 0:
            return 0
        return (delta_ns * self._fps) // 1_000_000_000

    def _emit(self, event: dict) -> None:
        self._event_count += 1
        et = event.get("type")
        if et == "key":
            self._key_events += 1
        elif et == "mouse":
            self._mouse_events += 1
        self._on_event(event)
