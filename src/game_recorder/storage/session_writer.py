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
    resolution: list[int] = field(default_factory=lambda: [0, 0])
    encoder: str = ""
    # Audio input actually used; ``None`` means the recording is silent.
    # Format: ``"wasapi:default"`` or ``"dshow:<device name>"``.
    audio_source: str | None = None
    foreground_window: str = ""
    total_frames: int = 0
    total_input_events: int = 0
    segment_seconds: int = 0
    segments: list[SegmentMeta] = field(default_factory=list)

    def save(self, path: Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
