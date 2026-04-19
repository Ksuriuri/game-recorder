"""Per-frame buffered JSONL writer for input-event streams.

Events arrive from the input-hook thread already tagged with a ``frame``
index that aligns with the video stream.  This writer groups consecutive
events sharing the same frame into a single JSONL record:

    {"frame": 5, "events": [{"type":"key", ...}, {"type":"mouse", ...}]}

Only frames containing at least one input event are written, so the file
is sparse by design.

Records are buffered in memory and flushed to disk in batches to minimise
I/O syscalls during gameplay.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class ActionWriter:
    """Thread-safe writer that buckets input events by video-frame index.

    Parameters
    ----------
    path:
        Output ``.jsonl`` file path.
    buffer_frames:
        Number of completed frame records to accumulate before flushing.
    """

    def __init__(self, path: Path, buffer_frames: int = 64) -> None:
        self._path = Path(path)
        self._buffer_frames = buffer_frames
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._file = open(self._path, "w", encoding="utf-8", buffering=8192)

        # Active frame bucket
        self._current_frame: int | None = None
        self._current_events: list[dict] = []

        # Stats
        self._total_events = 0
        self._total_frames_written = 0

    @property
    def total_written(self) -> int:
        """Total number of raw input events received (across all frames)."""
        return self._total_events

    @property
    def total_frames_written(self) -> int:
        """Number of frame records emitted (frames with >=1 event)."""
        return self._total_frames_written

    def write(self, event: dict) -> None:
        """Append an event dict.  ``event['frame']`` is the bucket key.

        Thread-safe.  Events are expected to arrive in non-decreasing frame
        order (which holds for a single-threaded Win32 hook message pump).
        Out-of-order events for an already-flushed frame are emitted as a
        new record for that frame to avoid silent data loss.
        """
        frame = event.pop("frame")
        with self._lock:
            self._total_events += 1
            if self._current_frame is None:
                self._current_frame = frame
                self._current_events.append(event)
                return

            if frame == self._current_frame:
                self._current_events.append(event)
                return

            # Frame transition — emit the previous bucket, open a new one.
            self._emit_current_locked()
            self._current_frame = frame
            self._current_events.append(event)

            if len(self._buffer) >= self._buffer_frames:
                self._flush_buffer_locked()

    def flush(self) -> None:
        """Force-write all pending records (does NOT close the active frame)."""
        with self._lock:
            self._flush_buffer_locked()
            self._file.flush()

    def close(self) -> None:
        """Emit any in-progress frame, flush to disk, and close the file."""
        with self._lock:
            self._emit_current_locked()
            self._flush_buffer_locked()
            self._file.flush()
            self._file.close()
        logger.info(
            "Action log closed: %d events across %d frames → %s",
            self._total_events,
            self._total_frames_written,
            self._path,
        )

    def _emit_current_locked(self) -> None:
        if self._current_frame is None or not self._current_events:
            return
        record = {"frame": self._current_frame, "events": self._current_events}
        self._buffer.append(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        )
        self._total_frames_written += 1
        self._current_events = []
        self._current_frame = None

    def _flush_buffer_locked(self) -> None:
        if not self._buffer:
            return
        self._file.write("\n".join(self._buffer) + "\n")
        self._buffer.clear()

    def __enter__(self) -> ActionWriter:
        return self

    def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        self.close()
