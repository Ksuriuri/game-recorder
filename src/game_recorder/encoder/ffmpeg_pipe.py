"""FFmpeg subprocess encoder: raw BGR frames via stdin pipe + WASAPI audio capture.

Video and audio are muxed in the same FFmpeg process so A/V sync is handled
internally by FFmpeg — no manual timestamp alignment needed.
"""

from __future__ import annotations

import functools
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path

from game_recorder.config import Config, detect_nvenc, find_ffmpeg, nvenc_runtime_usable

logger = logging.getLogger(__name__)

# Windows pipe writes: very large single writes can fail with OSError EINVAL; chunking avoids that.
_STDIN_CHUNK = 256 * 1024


# FFmpeg 8+ lists devices as: ... "Device Name" (audio)
_DSHOW_AUDIO_LINE = re.compile(r'"([^"]+)"\s*\(audio\)')


def _list_dshow_devices(ffmpeg: str) -> list[str]:
    """Return names of DirectShow audio capture devices."""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        lines = result.stderr.splitlines()
        devices: list[str] = []
        # FFmpeg 8+: no "DirectShow audio devices" header; match quoted names with (audio)
        for line in lines:
            m = _DSHOW_AUDIO_LINE.search(line)
            if m:
                devices.append(m.group(1))
        if devices:
            return devices

        # Older FFmpeg: section between "DirectShow audio devices" and next section
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


def _is_likely_microphone_only(name: str) -> bool:
    """Heuristic: physical inputs are wrong default for *system* (game) audio."""
    n = name.lower()
    if any(k in n for k in ("stereo mix", "what u hear", "wave out mix", "loopback")):
        return False
    return any(
        x in n
        for x in (
            "microphone",
            "mic",
            "headset",
            "麦克风",
            "array",
            "阵列",
        )
    )


@functools.lru_cache(maxsize=4)
def _ffmpeg_has_wasapi_demuxer(ffmpeg: str) -> bool:
    """True if this FFmpeg build registers the WASAPI input device (full builds, not essentials)."""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-devices"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return "wasapi" in out.lower()
    except Exception:
        return False


@functools.lru_cache(maxsize=4)
def _wasapi_loopback_usable(ffmpeg: str) -> bool:
    """True if `wasapi -loopback 1 -i default` can actually be opened on this PC.

    Having the demuxer registered is necessary but not sufficient: the default
    playback endpoint may be missing, in exclusive mode, or rejected by the
    audio service.  This probe runs a 0.2-second null capture so we can
    silently fall back to DirectShow in that case.
    """
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "wasapi",
                "-loopback",
                "1",
                "-i",
                "default",
                "-t",
                "0.2",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return True
        logger.warning(
            "WASAPI loopback probe failed (rc=%d): %s",
            result.returncode,
            (result.stderr or "").strip().splitlines()[-1:] or "<no stderr>",
        )
        return False
    except Exception as e:
        logger.warning("WASAPI loopback probe raised: %s", e)
        return False


def _find_loopback_device(ffmpeg: str) -> str | None:
    """Pick a DirectShow capture device likely to carry desktop/game audio."""
    devices = _list_dshow_devices(ffmpeg)
    if not devices:
        return None

    def rank(name: str) -> int:
        """Lower = better for *system* audio. Mics last; VoiceMeeter last among non-mics."""
        if _is_likely_microphone_only(name):
            return 300
        n = name.lower()
        if any(k in n for k in ("stereo mix", "what u hear", "wave out mix")):
            return 0
        if "loopback" in n and "voicemeeter" not in n:
            return 1
        if "virtual cable" in n or "vb-audio cable" in n:
            return 2
        if any(k in n for k in ("cable output", "wave out")):
            return 3
        if "voicemeeter" in n or "vb-audio" in n:
            return 50
        return 10

    best = min(devices, key=rank)
    r = rank(best)
    if r >= 300:
        logger.warning(
            "Only microphone-like DirectShow devices found; skipping audio. In Windows, "
            "enable Stereo Mix (Recording tab → show disabled devices) or pass --audio-device."
        )
        return None
    if r >= 50:
        logger.warning(
            "Using %r — if the video has no desktop/game sound, route Windows playback "
            "through VoiceMeeter or enable Stereo Mix and pass its exact name to --audio-device.",
            best,
        )
    elif r > 10:
        logger.info(
            "Auto-selected DirectShow audio %r — if silent, enable Stereo Mix or set --audio-device.",
            best,
        )
    return best


class FFmpegEncoder:
    """Manages an FFmpeg child process that receives raw video frames via pipe
    and optionally captures system audio through WASAPI/DirectShow.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._proc: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._ffmpeg_path = find_ffmpeg()
        listed = detect_nvenc(self._ffmpeg_path)
        self._has_nvenc = listed and nvenc_runtime_usable(self._ffmpeg_path)
        if listed and not self._has_nvenc:
            logger.warning(
                "NVENC is built into FFmpeg but the GPU driver rejected it "
                "(e.g. need NVIDIA driver 570+ for this FFmpeg). Using libx264."
            )
        self._encoder = "h264_nvenc" if self._has_nvenc else "libx264"
        self._frame_size = 0
        self._ffmpeg_stderr = bytearray()
        self._stdin_broken_logged = False
        self._audio_source: str | None = None

    @property
    def encoder_name(self) -> str:
        return self._encoder

    @property
    def audio_source(self) -> str | None:
        """Human-readable description of the audio input actually used.

        ``None`` means the recording has no audio track (no usable device
        was found on this machine).  Examples:
            - ``"wasapi:default"``   (zero-config, full FFmpeg build)
            - ``"dshow:Stereo Mix (Realtek)"``
        Set by :meth:`start`; valid for the lifetime of the process.
        """
        return self._audio_source

    def start(self, width: int, height: int, output_path: Path) -> None:
        """Launch the FFmpeg subprocess."""
        self._frame_size = width * height * 3  # BGR24

        cfg = self.config
        use_wasapi = False
        dshow_device: str | None = None

        if cfg.audio_device:
            dshow_device = cfg.audio_device
        else:
            # Prefer WASAPI loopback: zero-config, no Stereo Mix needed,
            # works on any Windows 10/11 machine — but only if the demuxer
            # is compiled in AND the default endpoint is actually openable.
            if _ffmpeg_has_wasapi_demuxer(
                self._ffmpeg_path
            ) and _wasapi_loopback_usable(self._ffmpeg_path):
                use_wasapi = True
                logger.info("Using WASAPI loopback (default Windows playback).")
            else:
                if not _ffmpeg_has_wasapi_demuxer(self._ffmpeg_path):
                    logger.info(
                        "FFmpeg build has no WASAPI indev; falling back to DirectShow. "
                        "For zero-config audio on arbitrary Windows PCs, install a full "
                        "FFmpeg build (e.g. BtbN gpl)."
                    )
                dshow_device = _find_loopback_device(self._ffmpeg_path)

        has_audio = use_wasapi or dshow_device is not None
        if use_wasapi:
            self._audio_source = "wasapi:default"
        elif dshow_device is not None:
            self._audio_source = f"dshow:{dshow_device}"
        else:
            self._audio_source = None
            logger.warning(
                "No usable audio capture device found — recording will be SILENT. "
                "Install a full FFmpeg build (WASAPI loopback) or enable Stereo Mix."
            )

        cmd: list[str] = [self._ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning"]
        # Must be global: applies to muxer when any input ends (see -shortest in ffmpeg-all).
        if has_audio:
            cmd.append("-shortest")

        # Audio BEFORE rawvideo pipe: avoids FFmpeg waiting on stdin probe while dshow runs,
        # and matches common working pipe+dshow examples.
        if use_wasapi:
            cmd += ["-f", "wasapi", "-loopback", "1", "-i", "default"]
        elif dshow_device is not None:
            cmd += [
                "-thread_queue_size",
                "4096",
                "-f",
                "dshow",
                "-i",
                f"audio={dshow_device}",
            ]

        cmd += [
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(cfg.fps),
            "-i",
            "pipe:0",
        ]

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
            # Input order: 0 = audio (dshow or wasapi), 1 = rawvideo from pipe
            cmd += ["-map", "1:v", "-map", "0:a"]

        cmd.append(str(output_path))

        logger.info(
            "Starting FFmpeg: encoder=%s audio=%s",
            self._encoder,
            self._audio_source or "<none>",
        )
        logger.debug("FFmpeg cmd: %s", " ".join(cmd))

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_below_normal_priority(),
        )
        self._start_stderr_drain()

    def _start_stderr_drain(self) -> None:
        """Read FFmpeg stderr in a thread so the pipe never fills and blocks the child."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return

        def drain() -> None:
            try:
                while True:
                    chunk = proc.stderr.read(65536)
                    if not chunk:
                        break
                    self._ffmpeg_stderr.extend(chunk)
            except Exception:
                pass

        threading.Thread(target=drain, name="ffmpeg-stderr", daemon=True).start()

    def write_frame(self, frame_bytes: bytes) -> None:
        """Write one raw BGR24 frame to the FFmpeg pipe."""
        if self._proc is None or self._proc.stdin is None:
            return
        if len(frame_bytes) != self._frame_size:
            logger.warning(
                "Frame byte size %d != expected %d (WxHx3); encoder may desync",
                len(frame_bytes),
                self._frame_size,
            )
        try:
            mv = memoryview(frame_bytes)
            while len(mv) > 0:
                n = self._proc.stdin.write(mv[:_STDIN_CHUNK])
                mv = mv[n:]
        except (BrokenPipeError, OSError) as e:
            if not self._stdin_broken_logged:
                self._stdin_broken_logged = True
                logger.error("FFmpeg stdin write failed: %s", e)
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
            self._ffmpeg_stderr.clear()

    def _dump_stderr(self) -> None:
        err_text = ""
        if self._ffmpeg_stderr:
            err_text = self._ffmpeg_stderr.decode(errors="replace")
        elif self._proc and self._proc.stderr:
            try:
                err_text = self._proc.stderr.read().decode(errors="replace")
            except Exception:
                pass
        if err_text.strip():
            logger.error("FFmpeg stderr:\n%s", err_text)


def _below_normal_priority() -> int:
    """Return process creation flags for below-normal priority on Windows."""
    if sys.platform == "win32":
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        return BELOW_NORMAL_PRIORITY_CLASS
    return 0
