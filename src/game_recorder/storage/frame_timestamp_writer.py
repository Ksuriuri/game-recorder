"""Per-encoded-frame capture timestamp sidecar writer."""

from __future__ import annotations

import json
import threading
from pathlib import Path

FRAME_TIMESTAMPS_FILENAME = "frame_timestamps.jsonl"
FRAME_TIMESTAMPS_SCHEMA = "video_frame_timestamps_v1"
FRAME_TIMESTAMPS_CLOCK = "perf_counter_ns_mapped_to_unix_ms"


class FrameTimestampWriter:
    """Write one timestamp record for every frame sent to the video encoder."""

    def __init__(self, path: Path, buffer_records: int = 64) -> None:
        self._path = Path(path)
        self._buffer_records = max(1, int(buffer_records))
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._file = open(self._path, "w", encoding="utf-8", buffering=8192)
        self._total_written = 0
        self._duplicate_written = 0

    @property
    def total_written(self) -> int:
        return self._total_written

    @property
    def duplicate_written(self) -> int:
        return self._duplicate_written

    def write(
        self,
        *,
        frame: int,
        capture_perf_ns: int,
        capture_unix_ms: float,
        source_frame: int,
        duplicate: bool,
        duplicate_of: int | None = None,
    ) -> None:
        """Append an encoded-frame timestamp record."""
        record: dict[str, int | float | bool] = {
            "frame": int(frame),
            "t_capture_unix_ms": round(float(capture_unix_ms), 3),
            "t_capture_perf_ns": int(capture_perf_ns),
            "source_frame": int(source_frame),
            "duplicate": bool(duplicate),
        }
        if duplicate:
            if duplicate_of is None:
                raise ValueError("duplicate frame requires duplicate_of")
            record["duplicate_of"] = int(duplicate_of)

        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._buffer.append(line)
            self._total_written += 1
            if duplicate:
                self._duplicate_written += 1
            if len(self._buffer) >= self._buffer_records:
                self._flush_buffer_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_buffer_locked()
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._flush_buffer_locked()
            self._file.flush()
            self._file.close()

    def _flush_buffer_locked(self) -> None:
        if not self._buffer:
            return
        self._file.write("\n".join(self._buffer) + "\n")
        self._file.flush()
        self._buffer.clear()


def trim_frame_timestamps(path: Path, *, max_frame_exclusive: int) -> tuple[int, int]:
    """Trim tail records and return ``(kept_frames, kept_duplicates)``."""
    kept_lines: list[str] = []
    duplicate_count = 0
    with open(path, encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if int(record["frame"]) >= max_frame_exclusive:
                continue
            kept_lines.append(line)
            if bool(record.get("duplicate", False)):
                duplicate_count += 1

    tmp = path.with_name(f"{path.stem}.trim{path.suffix}")
    try:
        with open(tmp, "w", encoding="utf-8") as file:
            if kept_lines:
                file.write("\n".join(kept_lines) + "\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return len(kept_lines), duplicate_count
