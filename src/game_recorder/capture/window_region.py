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
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetClientRect.restype = wt.BOOL
    user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    user32.ClientToScreen.restype = wt.BOOL
    return user32


@dataclass(frozen=True)
class CaptureTarget:
    """Resolved capture target for the current recording session."""

    region: CaptureRegion | None
    title: str
    source: str


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
    if normalized == "screen":
        return CaptureTarget(region=None, title=get_foreground_window_title(), source="screen")

    title = get_foreground_window_title()
    region = _foreground_client_region(output_width, output_height)
    if region is None:
        logger.info("捕获目标：全屏（无可用前台客户区窗口）")
        return CaptureTarget(region=None, title=title, source="screen")

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
        return CaptureTarget(region=region, title=title, source="foreground")

    if normalized == "auto" and large_enough_for_auto:
        logger.info(
            "捕获目标：自动前台客户区 %s（占输出 %.0f%%）标题=%r",
            region,
            area_ratio * 100,
            title,
        )
        return CaptureTarget(region=region, title=title, source="auto_foreground")

    logger.info(
        "捕获目标：全屏（前台客户区 %dx%d 仅占输出 %.0f%%）",
        region.width,
        region.height,
        area_ratio * 100,
    )
    return CaptureTarget(region=None, title=title, source="screen")


def _foreground_client_region(output_width: int, output_height: int) -> CaptureRegion | None:
    try:
        user32 = _user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None

        rect = wt.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None

        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None

        point = wt.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
            return None

        left = max(0, int(point.x))
        top = max(0, int(point.y))
        right = min(output_width, int(point.x) + width)
        bottom = min(output_height, int(point.y) + height)

        if right <= left or bottom <= top:
            return None
        return CaptureRegion(left=left, top=top, right=right, bottom=bottom)
    except Exception as exc:
        logger.debug("前台捕获区域检测失败：%s", exc)
        return None
