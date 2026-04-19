"""Recording configuration with defaults tuned for game capture."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    fps: int = 30
    output_dir: Path = field(default_factory=lambda: Path("recordings"))

    # Video encoding
    video_quality: int = 23  # CQ value: lower = better quality, higher = smaller file
    video_preset: str = "p4"  # NVENC preset (p1=fastest … p7=best quality)

    # Audio
    audio_device: str | None = None  # None = auto-detect WASAPI loopback
    audio_bitrate: str = "128k"

    # Input capture
    mouse_poll_interval_ms: float = 5.0  # throttle mouse-move events (200 Hz)

    # Session management
    max_segment_seconds: int = 0  # 0 = no auto-segmentation

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)


def find_ffmpeg() -> str:
    """Locate the ffmpeg binary: bundled copy first, then PATH."""
    bundled = Path(__file__).resolve().parent.parent.parent / "ffmpeg" / "ffmpeg.exe"
    if bundled.is_file():
        return str(bundled)

    found = shutil.which("ffmpeg")
    if found:
        return found

    print(
        "ERROR: ffmpeg not found. Place ffmpeg.exe in the ffmpeg/ directory "
        "or add it to PATH.",
        file=sys.stderr,
    )
    sys.exit(1)


def detect_nvenc(ffmpeg_path: str) -> bool:
    """Check whether h264_nvenc is available in the installed ffmpeg."""
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "h264_nvenc" in result.stdout
    except Exception:
        return False
