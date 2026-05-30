"""Simple terminal progress helpers (stdlib only)."""

from __future__ import annotations

import math
import sys
import time
from typing import TextIO


def format_duration(seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS."""
    if not math.isfinite(seconds) or seconds < 0:
        return "--:--"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def progress_bar(current: int, total: int | None, *, width: int = 20) -> str:
    if total and total > 0:
        ratio = min(1.0, max(0.0, current / total))
        filled = int(width * ratio)
        pct = ratio * 100.0
        return f"[{'#' * filled}{'-' * (width - filled)}] {pct:5.1f}%"
    return f"[{'?' * width}] -----"


class ProgressWriter:
    """Single-line \\r progress output."""

    def __init__(self, stream: TextIO | None = None, *, min_interval: float = 0.2) -> None:
        self.stream = stream or sys.stderr
        self.min_interval = min_interval
        self._last_len = 0
        self._last_update = 0.0

    def update(self, line: str, *, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and now - self._last_update < self.min_interval:
            return
        self._last_update = now
        pad = max(0, self._last_len - len(line))
        self.stream.write("\r" + line + " " * pad)
        self.stream.flush()
        self._last_len = len(line)

    def finish(self, line: str = "") -> None:
        if line:
            pad = max(0, self._last_len - len(line))
            self.stream.write("\r" + line + " " * pad + "\n")
        else:
            self.stream.write("\n")
        self.stream.flush()
        self._last_len = 0


def eta_from_rate(done: int, total: int, elapsed_s: float) -> str:
    if total <= 0 or done <= 0 or elapsed_s <= 0:
        return "--:--"
    remaining = (total - done) * (elapsed_s / done)
    return format_duration(remaining)
