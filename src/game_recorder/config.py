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
    # Quality scale (~0–51): lower = better quality / larger files.
    # Mapped to NVENC -cq, AMF -qp_*, QSV -global_quality, or libx264 -crf.
    video_quality: int = 23
    video_preset: str = "p4"  # NVENC preset only (p1=fastest … p7=best quality)
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

    # Sliding-window width (seconds) for frame-drop auto-stop and tail trim. 0 disables both
    # (drops are still logged and written to meta.json).
    frame_drop_stop_after_s: float = 10.0
    # Max dropped frames allowed within the window before auto-stop (inclusive). 5 = up to 5 OK.
    frame_drop_max_tolerated: int = 5

    # Discard the whole session on stop when shorter than this (seconds). 0 = off.
    min_recording_duration_s: float = 10.0

    # In-game camera plugins: publish source-specific active_session.json files,
    # then select and align the one source that produced samples.
    gta_camera_sync: bool = True
    rdr2_camera_sync: bool = True
    wukong_camera_sync: bool = True
    cp2077_camera_sync: bool = True

    # OS-level WASD + mouse wander (hybrid: SendInput drive + camera pose feedback).
    # On by default; disable with ``--no-auto-move``.
    auto_move: bool = True
    auto_move_tick_hz: float = 250.0
    auto_move_stuck_speed_mps: float = 0.15
    auto_move_stuck_s: float = 1.5
    auto_move_turn_deg_s: float = 55.0

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.auto_move:
            # Constant WASD hold would trip idle/stuck; smooth scripted look can
            # still look "violent" to the shake detector — disable both in auto mode.
            self.idle_timeout_s = 0.0
            self.violent_duration_s = 0.0


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


# Preferred H.264 encoder order: GPU hardware first, then software.
_H264_ENCODER_PREFERENCE: tuple[str, ...] = (
    "h264_nvenc",  # NVIDIA NVENC
    "h264_amf",  # AMD AMF
    "h264_qsv",  # Intel Quick Sync
    "libx264",  # CPU fallback
)


@functools.lru_cache(maxsize=8)
def listed_h264_encoders(ffmpeg_path: str) -> frozenset[str]:
    """Return H.264 encoder names advertised by ``ffmpeg -encoders``."""
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return frozenset()
    out = result.stdout or ""
    return frozenset(name for name in _H264_ENCODER_PREFERENCE if name in out)


def detect_nvenc(ffmpeg_path: str) -> bool:
    """Check whether h264_nvenc is available in the installed ffmpeg."""
    return "h264_nvenc" in listed_h264_encoders(ffmpeg_path)


@functools.lru_cache(maxsize=32)
def hw_encoder_runtime_usable(ffmpeg_path: str, encoder: str) -> bool:
    """True if FFmpeg can actually open *encoder* on this machine.

    ``-encoders`` alone is not enough: the build may list NVENC/AMF/QSV while the
    installed driver / GPU rejects opening the encoder (e.g. NVENC API mismatch).

    Probe uses ``yuv420p`` at 256x256 — tiny lavfi sizes without an explicit
    pixel format often fail with ``Invalid argument`` even when the GPU works.
    """
    if encoder == "libx264":
        return True
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
                "color=c=black:s=256x256:d=0.1",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                encoder,
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


def nvenc_runtime_usable(ffmpeg_path: str) -> bool:
    """True if FFmpeg can actually open h264_nvenc (driver NVENC API matches the build)."""
    return hw_encoder_runtime_usable(ffmpeg_path, "h264_nvenc")


@functools.lru_cache(maxsize=8)
def select_h264_encoder(ffmpeg_path: str) -> str:
    """Pick the best usable H.264 encoder: NVENC → AMF → QSV → libx264."""
    listed = listed_h264_encoders(ffmpeg_path)
    for name in _H264_ENCODER_PREFERENCE:
        if name == "libx264":
            return "libx264"
        if name not in listed:
            continue
        if hw_encoder_runtime_usable(ffmpeg_path, name):
            return name
    return "libx264"
