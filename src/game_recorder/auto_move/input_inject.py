"""Windows SendInput helpers for WASD keys and relative mouse look."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
# Prevent Windows from coalescing injected moves into one jump (critical for smooth look).
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000

MAPVK_VK_TO_VSC = 0

VK_W = 0x57
VK_A = 0x41
VK_S = 0x53
VK_D = 0x44

WASD_VKS: frozenset[int] = frozenset((VK_W, VK_A, VK_S, VK_D))

ULONG_PTR = ctypes.c_size_t

# Cap each SendInput mouse packet; leftover is sent in subsequent packets same call.
_MAX_MOUSE_STEP = 4


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    )


class _INPUT(ctypes.Structure):
    _fields_ = (
        ("type", wt.DWORD),
        ("union", _INPUTUNION),
    )


def _user32() -> ctypes.WinDLL:
    return ctypes.WinDLL("user32", use_last_error=True)


def _send_inputs(inputs: list[_INPUT]) -> None:
    if not inputs:
        return
    user32 = _user32()
    n = len(inputs)
    arr = (_INPUT * n)(*inputs)
    sent = int(user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT)))
    if sent != n:
        err = ctypes.get_last_error()
        raise OSError(err, f"SendInput sent {sent}/{n} events (GetLastError={err})")


def _vk_to_scan(vk: int) -> int:
    user32 = _user32()
    user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
    user32.MapVirtualKeyW.restype = wt.UINT
    return int(user32.MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC)) & 0xFF


def _key_input(vk: int, *, up: bool) -> _INPUT:
    scan = _vk_to_scan(vk)
    flags = KEYEVENTF_SCANCODE
    if up:
        flags |= KEYEVENTF_KEYUP
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = _KEYBDINPUT(
        wVk=0,
        wScan=scan,
        dwFlags=flags,
        time=0,
        dwExtraInfo=ULONG_PTR(0),
    )
    return inp


def _mouse_move_input(dx: int, dy: int) -> _INPUT:
    inp = _INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi = _MOUSEINPUT(
        dx=int(dx),
        dy=int(dy),
        mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_MOVE_NOCOALESCE,
        time=0,
        dwExtraInfo=ULONG_PTR(0),
    )
    return inp


def set_timer_resolution_1ms(*, enabled: bool) -> None:
    """Raise Windows timer resolution so high-Hz sleep loops stay accurate."""
    try:
        winmm = ctypes.WinDLL("winmm")
        if enabled:
            winmm.timeBeginPeriod(1)
        else:
            winmm.timeEndPeriod(1)
    except OSError as exc:
        logger.debug("timeBeginPeriod unavailable: %s", exc)


@dataclass
class InputInjector:
    """Track held WASD keys and inject relative mouse deltas via SendInput."""

    _held: set[int] = field(default_factory=set)
    # Sub-pixel residuals so slow/smooth look is not lost to integer rounding.
    _frac_x: float = 0.0
    _frac_y: float = 0.0

    @property
    def held_keys(self) -> frozenset[int]:
        return frozenset(self._held)

    def set_keys(self, keys: frozenset[int] | set[int] | None) -> None:
        """Press/release WASD so the held set matches ``keys`` (None = release all)."""
        wanted = set(keys or ()) & set(WASD_VKS)
        to_up = self._held - wanted
        to_down = wanted - self._held
        batch: list[_INPUT] = []
        for vk in sorted(to_up):
            batch.append(_key_input(vk, up=True))
        for vk in sorted(to_down):
            batch.append(_key_input(vk, up=False))
        if batch:
            _send_inputs(batch)
            self._held = wanted

    def move_mouse(self, dx: float, dy: float) -> None:
        self._frac_x += float(dx)
        self._frac_y += float(dy)
        ix = int(self._frac_x)  # trunc toward zero
        iy = int(self._frac_y)
        self._frac_x -= ix
        self._frac_y -= iy
        if ix == 0 and iy == 0:
            return
        # One SendInput call with several small non-coalesced packets.
        packets: list[_INPUT] = []
        while ix != 0 or iy != 0:
            step_x = max(-_MAX_MOUSE_STEP, min(_MAX_MOUSE_STEP, ix))
            step_y = max(-_MAX_MOUSE_STEP, min(_MAX_MOUSE_STEP, iy))
            packets.append(_mouse_move_input(step_x, step_y))
            ix -= step_x
            iy -= step_y
            if len(packets) >= 32:
                _send_inputs(packets)
                packets = []
        if packets:
            _send_inputs(packets)

    def release_all(self) -> None:
        self.set_keys(None)
        self._frac_x = 0.0
        self._frac_y = 0.0

    def hold_w(self) -> None:
        self.set_keys({VK_W})
