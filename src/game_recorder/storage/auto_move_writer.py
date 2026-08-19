"""Auto-move policy label sidecar writer."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

AUTO_MOVE_FILENAME = "auto_move.jsonl"
AUTO_MOVE_SCHEMA = "auto_move_actions_v1"


class AutoMoveWriter:
    """Write one record per *change* in the policy's chosen action.

    The policy re-decides at 30 Hz but holds each action for seconds, so
    deduplicating on the label collapses a 30-minute session to a few hundred
    records — the injected keystrokes and mouse deltas themselves are already in
    the segment input log, and this only adds the labels that cannot be
    recovered from it (which paradigm, and where each turn starts).

    ``frame`` uses the same clock mapping as the input log, so records join
    directly against ``frame_timestamps.jsonl`` and the segment jsonl. Indices
    are absolute and never trimmed, so consumers should ignore records at or
    past ``meta.json``'s ``total_frames`` after a tail trim.
    """

    def __init__(
        self,
        path: Path,
        *,
        t0_perf_ns: int,
        t0_epoch_ms: int,
        fps: int,
        buffer_records: int = 32,
    ) -> None:
        self._path = Path(path)
        self._t0_perf_ns = int(t0_perf_ns)
        self._t0_epoch_ms = int(t0_epoch_ms)
        self._fps = max(1, int(fps))
        self._buffer_records = max(1, int(buffer_records))
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._file = open(self._path, "w", encoding="utf-8", buffering=8192)
        self._total_written = 0
        self._last_label: tuple[object, ...] | None = None

    @property
    def total_written(self) -> int:
        return self._total_written

    def write(
        self,
        *,
        action_id: int | None,
        translation: str | None,
        rotation: str | None,
        paradigm: str | None,
        turn_index: int | None,
        perf_ns: int | None = None,
    ) -> bool:
        """Append a record if the label changed. Returns True when written."""
        label = (action_id, translation, rotation, paradigm, turn_index)
        with self._lock:
            # Called ~30x per second but only a few hundred times per session
            # produce a record, so the repeat path must stay a tuple compare —
            # everything else happens only on a real change.
            if label == self._last_label:
                return False
            self._last_label = label
            now_ns = time.perf_counter_ns() if perf_ns is None else int(perf_ns)
            elapsed_ns = max(0, now_ns - self._t0_perf_ns)
            record = {
                "frame": int(elapsed_ns * self._fps // 1_000_000_000),
                "t_unix_ms": round(self._t0_epoch_ms + elapsed_ns / 1_000_000, 3),
                "action_id": action_id,
                "translation": translation,
                "rotation": rotation,
                "paradigm": paradigm,
                "turn_index": turn_index,
            }
            self._buffer.append(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            )
            self._total_written += 1
            if len(self._buffer) >= self._buffer_records:
                self._flush_buffer_locked()
        return True

    def flush(self) -> None:
        with self._lock:
            self._flush_buffer_locked()
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file.closed:
                return
            self._flush_buffer_locked()
            self._file.flush()
            self._file.close()

    def _flush_buffer_locked(self) -> None:
        if not self._buffer:
            return
        self._file.write("\n".join(self._buffer) + "\n")
        self._buffer.clear()
