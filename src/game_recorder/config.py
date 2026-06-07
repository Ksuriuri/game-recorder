"""Recording configuration with defaults tuned for game capture."""

from __future__ import annotations

import functools
import os
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
    x264_threads: int = 2  # software fallback should not steal every CPU core from games

    # Audio
    audio_device: str | None = None  # None = auto-detect WASAPI loopback
    audio_bitrate: str = "128k"

    # Input capture
    mouse_poll_interval_ms: float = 1000.0 / 30.0  # throttle mouse-move events (30 Hz)
    # Keyboard: poll rate (Hz). WH_KEYBOARD_LL misses many games; GetAsyncKeyState does not.
    keyboard_poll_hz: float = 200.0

    # Video capture target
    # auto: capture a large foreground client window (borderless games), else full screen.
    # foreground: force the foreground client window when possible.
    # screen: always capture the full primary output.
    capture_mode: str = "auto"

    # Optional prefix (letters, digits, hyphen) for session folder and segment filenames.
    recording_id: str | None = None

    # Session management
    # Auto-segmentation: every N seconds, finalize current mp4 + jsonl and start
    # a new one within the same session directory.  0 disables segmentation
    # (single continuous file for the whole session — recommended, since
    # rotation currently introduces a sub-second video/audio gap).
    segment_seconds: int = 0

    # Stop when no WASD activity, or WASD state unchanged with no mouse move, for this many seconds. 0 = off.
    idle_timeout_s: float = 10.0

    # Stop when high-frequency WASD or mouse shaking lasts this many seconds. 0 = off.
    violent_duration_s: float = 1.0

    # Trim this many seconds from the tail when auto-stopping due to window focus loss.
    focus_lost_trim_s: float = 1.0

    # Discard the whole session on stop when shorter than this (seconds). 0 = off.
    min_recording_duration_s: float = 10.0

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)


def find_ffmpeg() -> str:
    """Resolve ffmpeg: ``GAME_RECORDER_FFMPEG``, then project ``ffmpeg/``, then PATH.

    BtbN gpl in ``ffmpeg/bin`` (``install.bat``) includes the WASAPI indev used for
    system/game audio. Minimal builds fall back to DirectShow and often need routing.
    """
    override = os.environ.get("GAME_RECORDER_FFMPEG", "").strip()
    if override:
        p = Path(override)
        if p.is_file():
            return str(p.resolve())
        print(
            f"错误：已设置 GAME_RECORDER_FFMPEG 但不是有效文件：{override!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    root = Path(__file__).resolve().parent.parent.parent
    for rel in (("ffmpeg", "bin", "ffmpeg.exe"), ("ffmpeg", "ffmpeg.exe")):
        candidate = root.joinpath(*rel)
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("ffmpeg")
    if found:
        return found

    print(
        "错误：未找到 ffmpeg。请运行 install.bat，或将 ffmpeg.exe 放入 ffmpeg\\bin 或 ffmpeg\\，"
        "或将完整构建加入 PATH。",
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
