"""Session metadata writer — produces the meta.json sidecar file."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class SegmentMeta:
    """Per-segment metadata entry."""

    index: int = 0
    start_frame: int = 0
    end_frame: int = 0  # exclusive
    frame_count: int = 0
    event_count: int = 0
    video: str = ""
    actions: str = ""


@dataclass
class SessionMeta:
    session_id: str = ""
    session_timestamp: str = ""
    start_epoch_ms: int = 0
    duration_s: float = 0.0
    fps: int = 30
    # jsonl ``frame`` ≈ video capture idx + this (median of wall−idx per captured frame); see Session.
    event_video_sync_offset: int = 0
    resolution: list[int] = field(default_factory=lambda: [0, 0])
    encoder: str = ""
    # Audio input actually used; ``None`` means the recording is silent.
    # Format: ``"wasapi:default"`` or ``"dshow:<device name>"``.
    audio_source: str | None = None
    foreground_window: str = ""
    capture_source: str = "screen"
    capture_region: list[int] | None = None
    total_frames: int = 0
    total_input_events: int = 0
    segment_seconds: int = 0
    segments: list[SegmentMeta] = field(default_factory=list)
    # Set when recording ends via auto-stop (``"idle"`` | ``"forbidden_key"`` | ``"violent"``).
    auto_stop_reason: str | None = None
    # Snapshot of ``Config.idle_timeout_s`` for library effective-duration math.
    idle_timeout_s: float = 0.0
    # Snapshot of ``Config.violent_duration_s`` for library effective-duration math.
    violent_duration_s: float = 0.0
    # Frames removed from the last segment mp4/jsonl after idle/violent auto-stop (0 = not trimmed).
    idle_tail_trim_frames: int = 0

    def save(self, path: Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
