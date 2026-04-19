"""Buffered JSONL writer for input-event streams.

Events are buffered in memory and flushed to disk in batches to minimise
I/O syscalls during gameplay.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class ActionWriter:
    """Thread-safe, buffered JSONL writer.

    Parameters
    ----------
    path:
        Output ``.jsonl`` file path.
    buffer_size:
        Number of events to accumulate before flushing to disk.
    """

    def __init__(self, path: Path, buffer_size: int = 100) -> None:
        self._path = Path(path)
        self._buffer_size = buffer_size
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._file = open(self._path, "w", encoding="utf-8", buffering=8192)
        self._total_written = 0

    @property
    def total_written(self) -> int:
        return self._total_written

    def write(self, event: dict) -> None:
        """Append an event dict.  Thread-safe."""
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._buffer.append(line)
            if len(self._buffer) >= self._buffer_size:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        self.flush()
        self._file.close()
        logger.info("Action log closed: %d events → %s", self._total_written, self._path)

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        chunk = "\n".join(self._buffer) + "\n"
        self._file.write(chunk)
        self._total_written += len(self._buffer)
        self._buffer.clear()

    def __enter__(self) -> ActionWriter:
        return self

    def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        self.close()
