"""Session metadata writer — produces the meta.json sidecar file."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class SessionMeta:
    session_id: str = ""
    start_epoch_ms: int = 0
    duration_s: float = 0.0
    fps: int = 30
    resolution: list[int] = field(default_factory=lambda: [0, 0])
    encoder: str = ""
    foreground_window: str = ""
    total_frames: int = 0
    total_input_events: int = 0

    def save(self, path: Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
