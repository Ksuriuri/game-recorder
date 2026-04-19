"""Recording configuration with defaults tuned for game capture."""

from __future__ import annotations

import functools
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
    # Keyboard: poll rate (Hz). WH_KEYBOARD_LL misses many games; GetAsyncKeyState does not.
    keyboard_poll_hz: float = 200.0

    # Session management
    # Auto-segmentation: every N seconds, finalize current mp4 + jsonl and start
    # a new one within the same session directory.  0 disables segmentation
    # (single continuous file for the whole session — recommended, since
    # rotation currently introduces a sub-second video/audio gap).
    segment_seconds: int = 0

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
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return "h264_nvenc" in result.stdout
    except Exception:
        return False


@functools.lru_cache(maxsize=8)
def nvenc_runtime_usable(ffmpeg_path: str) -> bool:
    """True if FFmpeg can actually open h264_nvenc (driver NVENC API matches the build).

    ``-encoders`` alone is not enough: newer FFmpeg may require a newer driver
    (e.g. API 13.0 / driver 570+) than the one installed.
    """
    try:
        result = subprocess.run(
            [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=0.04",
                "-c:v",
                "h264_nvenc",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False
