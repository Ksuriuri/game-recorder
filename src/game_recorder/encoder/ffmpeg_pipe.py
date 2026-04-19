"""FFmpeg subprocess encoder: raw BGR frames via stdin pipe + WASAPI audio capture.

Video and audio are muxed in the same FFmpeg process so A/V sync is handled
internally by FFmpeg — no manual timestamp alignment needed.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from game_recorder.config import Config, detect_nvenc, find_ffmpeg

logger = logging.getLogger(__name__)


def _list_dshow_devices(ffmpeg: str) -> list[str]:
    """Return names of DirectShow audio capture devices."""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Device names appear in stderr between quotes after "audio"
        lines = result.stderr.splitlines()
        devices: list[str] = []
        is_audio_section = False
        for line in lines:
            if "DirectShow audio devices" in line:
                is_audio_section = True
                continue
            if is_audio_section and '"' in line:
                name = line.split('"')[1]
                devices.append(name)
            if is_audio_section and ("DirectShow video" in line or line.strip() == ""):
                if devices:
                    break
        return devices
    except Exception:
        return []


def _find_loopback_device(ffmpeg: str) -> str | None:
    """Auto-detect a WASAPI loopback / stereo-mix device."""
    devices = _list_dshow_devices(ffmpeg)
    # Prefer common loopback device names
    preferred = ["stereo mix", "what u hear", "loopback", "wave out"]
    for dev in devices:
        for keyword in preferred:
            if keyword in dev.lower():
                return dev
    return devices[0] if devices else None


class FFmpegEncoder:
    """Manages an FFmpeg child process that receives raw video frames via pipe
    and optionally captures system audio through WASAPI/DirectShow.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._proc: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._ffmpeg_path = find_ffmpeg()
        self._has_nvenc = detect_nvenc(self._ffmpeg_path)
        self._encoder = "h264_nvenc" if self._has_nvenc else "libx264"
        self._frame_size = 0

    @property
    def encoder_name(self) -> str:
        return self._encoder

    def start(self, width: int, height: int, output_path: Path) -> None:
        """Launch the FFmpeg subprocess."""
        self._frame_size = width * height * 3  # BGR24

        cfg = self.config
        audio_device = cfg.audio_device or _find_loopback_device(self._ffmpeg_path)

        cmd: list[str] = [self._ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning"]

        # --- Video input: raw frames from pipe ---
        cmd += [
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(cfg.fps),
            "-i", "pipe:0",
        ]

        # --- Audio input: WASAPI loopback via DirectShow ---
        has_audio = audio_device is not None
        if has_audio:
            cmd += ["-f", "dshow", "-i", f"audio={audio_device}"]

        # --- Encoder settings ---
        if self._encoder == "h264_nvenc":
            cmd += [
                "-c:v", "h264_nvenc",
                "-preset", cfg.video_preset,
                "-rc", "vbr",
                "-cq", str(cfg.video_quality),
            ]
        else:
            cmd += [
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", str(cfg.video_quality),
            ]

        cmd += ["-pix_fmt", "yuv420p"]

        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", cfg.audio_bitrate]
            cmd += ["-map", "0:v", "-map", "1:a"]

        cmd.append(str(output_path))

        logger.info("Starting FFmpeg: encoder=%s audio=%s", self._encoder, audio_device)
        logger.debug("FFmpeg cmd: %s", " ".join(cmd))

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_below_normal_priority(),
        )

    def write_frame(self, frame_bytes: bytes) -> None:
        """Write one raw BGR24 frame to the FFmpeg pipe."""
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(frame_bytes)
        except BrokenPipeError:
            logger.error("FFmpeg pipe broken — encoder may have crashed")
            self._dump_stderr()

    def stop(self) -> None:
        """Gracefully close the pipe and wait for FFmpeg to finish."""
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logger.warning("FFmpeg did not exit in time, killing")
            self._proc.kill()
        finally:
            if self._proc.returncode and self._proc.returncode != 0:
                self._dump_stderr()
            self._proc = None

    def _dump_stderr(self) -> None:
        if self._proc and self._proc.stderr:
            try:
                err = self._proc.stderr.read().decode(errors="replace")
                if err.strip():
                    logger.error("FFmpeg stderr:\n%s", err)
            except Exception:
                pass


def _below_normal_priority() -> int:
    """Return process creation flags for below-normal priority on Windows."""
    if sys.platform == "win32":
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        return BELOW_NORMAL_PRIORITY_CLASS
    return 0
