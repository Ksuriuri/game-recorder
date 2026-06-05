"""Foreground-window region detection for borderless game capture."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
from dataclasses import dataclass

from game_recorder.capture.screen import CaptureRegion

logger = logging.getLogger(__name__)

_AUTO_MIN_AREA_RATIO = 0.45
_AUTO_MIN_WIDTH = 640
_AUTO_MIN_HEIGHT = 360
_FORCED_MIN_WIDTH = 128
_FORCED_MIN_HEIGHT = 128


def _user32() -> ctypes.WinDLL:
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsIconic.restype = wt.BOOL
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindow.restype = wt.BOOL
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetClientRect.restype = wt.BOOL
    user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    user32.ClientToScreen.restype = wt.BOOL
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    user32.AttachThreadInput.restype = wt.BOOL
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    user32.BringWindowToTop.argtypes = [wt.HWND]
    user32.BringWindowToTop.restype = wt.BOOL
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wt.BOOL
    return user32


@dataclass(frozen=True)
class CaptureTarget:
    """Resolved capture target for the current recording session."""

    region: CaptureRegion | None
    title: str
    source: str
    hwnd: int | None = None


def get_foreground_window_hwnd() -> int | None:
    """Best-effort: return the HWND of the current foreground window."""
    try:
        user32 = _user32()
        hwnd = user32.GetForegroundWindow()
        if hwnd and user32.IsWindow(hwnd):
            return int(hwnd)
    except Exception:
        pass
    return None


def get_foreground_window_title() -> str:
    """Best-effort: return the title of the current foreground window."""
    try:
        user32 = _user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value
    except Exception:
        pass
    return ""


def find_window_by_title(title: str) -> int | None:
    """Find the first visible top-level window whose title exactly matches."""
    if not title:
        return None
    user32 = _user32()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _callback(hwnd: wt.HWND, _lparam: wt.LPARAM) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == title:
            found.append(int(hwnd))
            return False
        return True

    enum_cb = _callback
    try:
        user32.EnumWindows(enum_cb, 0)
    except Exception:
        return None
    return found[0] if found else None


def _resolve_focus_target(*, hwnd: int | None = None, title: str = "") -> int | None:
    user32 = _user32()
    target = hwnd
    if target is not None and not user32.IsWindow(wt.HWND(target)):
        target = None
    if target is None and title:
        target = find_window_by_title(title)
    return target


_RECORDER_UI_TITLES = frozenset({"游戏录制状态"})


def is_recorder_ui_foreground() -> bool:
    """Return True when a recorder overlay / notice window owns foreground focus."""
    return get_foreground_window_title() in _RECORDER_UI_TITLES


def is_game_window_foreground(*, hwnd: int | None = None, title: str = "") -> bool:
    """Return True when the game window currently owns foreground focus."""
    target = _resolve_focus_target(hwnd=hwnd, title=title)
    if target is None:
        return False
    return _user32().GetForegroundWindow() == wt.HWND(target)


def restore_window_focus(*, hwnd: int | None = None, title: str = "") -> bool:
    """Bring a game window back to the foreground after recorder UI steals focus."""
    user32 = _user32()
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GetCurrentThreadId.restype = wt.DWORD

    target = _resolve_focus_target(hwnd=hwnd, title=title)
    if target is None:
        return False

    target_hwnd = wt.HWND(target)
    if user32.GetForegroundWindow() == target_hwnd:
        return True

    if user32.IsIconic(target_hwnd):
        user32.ShowWindow(target_hwnd, 9)  # SW_RESTORE

    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    target_tid = user32.GetWindowThreadProcessId(target_hwnd, None)
    cur_tid = kernel32.GetCurrentThreadId()

    attached_fg = attached_target = False
    try:
        if fg_tid:
            attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        if target_tid:
            attached_target = bool(user32.AttachThreadInput(cur_tid, target_tid, True))
        user32.SetForegroundWindow(target_hwnd)
        user32.BringWindowToTop(target_hwnd)
    finally:
        if attached_target and target_tid:
            user32.AttachThreadInput(cur_tid, target_tid, False)
        if attached_fg and fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, False)

    if user32.GetForegroundWindow() == target_hwnd:
        logger.info("已切回游戏窗口 %r (hwnd=%#x)", title or "?", target)
        return True
    logger.debug("切回游戏窗口未确认 %r (hwnd=%#x)", title or "?", target)
    return False


def resolve_capture_target(
    mode: str,
    output_width: int,
    output_height: int,
) -> CaptureTarget:
    """Resolve the requested capture target.

    ``auto`` captures a large foreground client area, which is what borderless
    games expose, and falls back to the full output when the foreground window
    looks like a launcher, terminal, or other small desktop window.
    """
    normalized = mode.lower().strip()
    fg_hwnd = get_foreground_window_hwnd()
    if normalized == "screen":
        return CaptureTarget(
            region=None,
            title=get_foreground_window_title(),
            source="screen",
            hwnd=fg_hwnd,
        )

    title = get_foreground_window_title()
    region, region_hwnd = _foreground_client_region(output_width, output_height)
    fg_hwnd = region_hwnd or fg_hwnd
    if region is None:
        logger.info("捕获目标：全屏（无可用前台客户区窗口）")
        return CaptureTarget(region=None, title=title, source="screen", hwnd=fg_hwnd)

    output_area = max(1, output_width * output_height)
    area_ratio = (region.width * region.height) / output_area
    force_foreground = normalized == "foreground"
    large_enough_for_auto = (
        area_ratio >= _AUTO_MIN_AREA_RATIO
        and region.width >= _AUTO_MIN_WIDTH
        and region.height >= _AUTO_MIN_HEIGHT
    )
    large_enough_for_forced = (
        region.width >= _FORCED_MIN_WIDTH and region.height >= _FORCED_MIN_HEIGHT
    )

    if force_foreground and large_enough_for_forced:
        logger.info("捕获目标：前台客户区 %s 标题=%r", region, title)
        return CaptureTarget(
            region=region, title=title, source="foreground", hwnd=fg_hwnd
        )

    if normalized == "auto" and large_enough_for_auto:
        logger.info(
            "捕获目标：自动前台客户区 %s（占输出 %.0f%%）标题=%r",
            region,
            area_ratio * 100,
            title,
        )
        return CaptureTarget(
            region=region, title=title, source="auto_foreground", hwnd=fg_hwnd
        )

    logger.info(
        "捕获目标：全屏（前台客户区 %dx%d 仅占输出 %.0f%%）",
        region.width,
        region.height,
        area_ratio * 100,
    )
    return CaptureTarget(region=None, title=title, source="screen", hwnd=fg_hwnd)


def _foreground_client_region(
    output_width: int,
    output_height: int,
) -> tuple[CaptureRegion | None, int | None]:
    try:
        user32 = _user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None, None
        hwnd_int = int(hwnd)
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None, hwnd_int

        rect = wt.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None, hwnd_int

        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None, hwnd_int

        point = wt.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
            return None, hwnd_int

        left = max(0, int(point.x))
        top = max(0, int(point.y))
        right = min(output_width, int(point.x) + width)
        bottom = min(output_height, int(point.y) + height)

        if right <= left or bottom <= top:
            return None, hwnd_int
        return CaptureRegion(left=left, top=top, right=right, bottom=bottom), hwnd_int
    except Exception as exc:
        logger.debug("前台捕获区域检测失败：%s", exc)
        return None, None
