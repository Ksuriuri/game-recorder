"""FFmpeg subprocess encoder: raw BGR frames via stdin pipe + WASAPI audio capture.

Video and audio are muxed in the same FFmpeg process so A/V sync is handled
internally by FFmpeg — no manual timestamp alignment needed.
"""

from __future__ import annotations

import functools
import logging
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

from game_recorder.config import (
    Config,
    find_ffmpeg,
    listed_h264_encoders,
    select_h264_encoder,
)
from game_recorder.encoder import python_loopback as _pyloop

logger = logging.getLogger(__name__)

_ENCODER_LABEL = {
    "h264_nvenc": "NVIDIA NVENC",
    "h264_amf": "AMD AMF",
    "h264_qsv": "Intel QSV",
    "libx264": "libx264（软件）",
}

# Python loopback (soundcard) format pumped into FFmpeg over TCP.
# 48 kHz / stereo / s16le matches the standard Windows shared-mode mixer format,
# so soundcard does no resampling and FFmpeg sees the original mix.
_PYLOOP_SAMPLERATE = 48_000
_PYLOOP_CHANNELS = 2
# Worker should publish "connected" then "streaming" within this budget.
_PYLOOP_STARTUP_TIMEOUT_S = 12.0

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
    """True if this FFmpeg build has the ``wasapi`` *demuxer* (rare in upstream static builds)."""
    # ``-devices`` is unreliable: recent win64 static builds (e.g. BtbN master/7.1) ship without
    # a wasapi demuxer even in "gpl" variants, so the list never mentions it.
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-h", "demuxer=wasapi"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if "Unknown format" in out or "Unknown demuxer" in out:
            return False
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
            "WASAPI 环回探测失败（rc=%d）：%s",
            result.returncode,
            (result.stderr or "").strip().splitlines()[-1:] or "<无 stderr>",
        )
        return False
    except Exception as e:
        logger.warning("WASAPI 环回探测异常：%s", e)
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
            "仅找到类似麦克风的 DirectShow 设备，跳过音频。请在 Windows 中"
            "启用 Stereo Mix（录制选项卡 → 显示禁用的设备）或通过 --audio-device 指定。"
        )
        return None
    if r >= 50:
        logger.warning(
            "正在使用 %r — 若视频无桌面/游戏声音，请将 Windows 播放路由"
            "至 VoiceMeeter 或启用 Stereo Mix，并将其准确名称传给 --audio-device。",
            best,
        )
    elif r > 10:
        logger.info(
            "已自动选择 DirectShow 音频 %r — 若无声，请启用 Stereo Mix 或设置 --audio-device。",
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
        self._encoder = select_h264_encoder(self._ffmpeg_path)
        listed = listed_h264_encoders(self._ffmpeg_path)
        if self._encoder == "libx264":
            skipped = [
                name
                for name in ("h264_nvenc", "h264_amf", "h264_qsv")
                if name in listed
            ]
            if skipped:
                logger.warning(
                    "FFmpeg 已编译 %s，但本机 GPU/驱动无法打开；回退到 libx264。"
                    "非 N 卡机器若卡顿，可试 --fps 20 --quality 28 --x264-threads 1。",
                    "/".join(skipped),
                )
        else:
            logger.info(
                "视频编码器：%s（%s）",
                self._encoder,
                _ENCODER_LABEL.get(self._encoder, self._encoder),
            )
        self._frame_size = 0
        self._ffmpeg_stderr = bytearray()
        self._stdin_broken_logged = False
        self._intentional_stop = False
        self._failed = False
        self._audio_source: str | None = None

        # Python soundcard-loopback pump (only set when that path is selected).
        self._pyloop_thread: threading.Thread | None = None
        self._pyloop_stop: threading.Event | None = None
        self._pyloop_queue: queue.Queue | None = None  # type: ignore[type-arg]

    @property
    def encoder_name(self) -> str:
        return self._encoder

    @property
    def failed(self) -> bool:
        """True when FFmpeg exited unexpectedly while recording."""
        return self._failed

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

    def is_alive(self) -> bool:
        """False when the child process has exited (excluding intentional stop())."""
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def start(self, width: int, height: int, output_path: Path) -> None:
        """Launch the FFmpeg subprocess."""
        self._intentional_stop = False
        self._failed = False
        self._stdin_broken_logged = False
        self._frame_size = width * height * 3  # BGR24

        cfg = self.config
        use_wasapi = False
        use_pyloop = False
        pyloop_port = 0
        dshow_device: str | None = None

        if cfg.audio_device:
            # Explicit override always wins, even if it's silent — user asked for it.
            dshow_device = cfg.audio_device
        else:
            # Selection priority for zero-config game-audio capture:
            #   1) FFmpeg native WASAPI demuxer  (single process; only some builds ship it)
            #   2) Python soundcard loopback     (zero-config on every Win10+; default on
            #      BtbN/upstream static FFmpeg, which has no wasapi demuxer)
            #   3) DirectShow auto-pick          (Stereo Mix / VB-CABLE / VoiceMeeter)
            if _ffmpeg_has_wasapi_demuxer(
                self._ffmpeg_path
            ) and _wasapi_loopback_usable(self._ffmpeg_path):
                use_wasapi = True
                logger.info("音频：FFmpeg WASAPI 环回（默认 Windows 播放设备）。")
            elif _pyloop.loopback_usable():
                use_pyloop = True
                pyloop_port = _pyloop.free_tcp_port()
                logger.info(
                    "音频：Python WASAPI 环回（soundcard，默认扬声器 → TCP 127.0.0.1:%d → FFmpeg）。",
                    pyloop_port,
                )
            else:
                logger.info(
                    "音频：无可用 WASAPI 环回 "
                    "（FFmpeg wasapi 解复用器与 soundcard 均不可用）— 回退至 DirectShow。"
                )
                dshow_device = _find_loopback_device(self._ffmpeg_path)

        has_audio = use_wasapi or use_pyloop or dshow_device is not None
        if use_wasapi:
            self._audio_source = "wasapi:default"
        elif use_pyloop:
            self._audio_source = "soundcard:default"
        elif dshow_device is not None:
            self._audio_source = f"dshow:{dshow_device}"
            if not cfg.audio_device and "voicemeeter" in dshow_device.lower():
                logger.error(
                    "自动选择的 DirectShow %r 通常无声：仅当 Windows 默认播放为 VoiceMeeter "
                    "*输入*（或应用已路由至该处）时才有信号。"
                    "修复：安装 `soundcard` Python 包（已是项目依赖）以使用 Python WASAPI 环回，"
                    "或通过 --audio-device 指定 Stereo Mix（在 设置 → 系统 → 声音 → 输入设备 中启用），"
                    "或运行 game-recorder --list-audio-devices 复制准确设备名。",
                    dshow_device,
                )
        else:
            self._audio_source = None
            logger.warning(
                "未找到可用音频捕获设备 — 录制将无声。"
                "请确认 `soundcard` 包导入正常（Python WASAPI 环回），"
                "或启用 Stereo Mix / 安装 VB-CABLE。"
            )

        cmd: list[str] = [self._ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning"]

        # Audio BEFORE rawvideo pipe: avoids FFmpeg waiting on stdin probe while the audio
        # demuxer initialises, and matches common working pipe+dshow examples.
        if use_wasapi:
            cmd += ["-f", "wasapi", "-loopback", "1", "-i", "default"]
        elif use_pyloop:
            cmd += _pyloop.tcp_ffmpeg_input_args(
                pyloop_port, _PYLOOP_SAMPLERATE, _PYLOOP_CHANNELS
            )
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
        cmd += _video_encoder_args(self._encoder, cfg)

        cmd += ["-pix_fmt", "yuv420p"]

        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", cfg.audio_bitrate]
            # Input order: 0 = audio (dshow or wasapi), 1 = rawvideo from pipe
            cmd += ["-map", "1:v", "-map", "0:a"]
            # Output muxer option: before any -i, FFmpeg misparses it as an input option (dshow err).
            cmd.append("-shortest")

        cmd.append(str(output_path))

        logger.info(
            "正在启动 FFmpeg：编码器=%s 音频=%s",
            self._encoder,
            self._audio_source or "<无>",
        )
        logger.debug("FFmpeg 命令：%s", " ".join(cmd))

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_below_normal_priority(),
        )
        self._start_stderr_drain()

        if use_pyloop:
            self._start_pyloop_pump(pyloop_port)

    def _start_pyloop_pump(self, port: int) -> None:
        """Spawn the soundcard→TCP worker; downgrade to silent on failure.

        Failure modes that get us here:
          * FFmpeg exited before binding the TCP port (encoder rejected video args, etc.)
          * `soundcard` cannot open the default speaker's loopback (driver / exclusive mode)
        Either way we kill the FFmpeg process and let the caller observe an empty audio
        track — but we MUST NOT leave the Popen running with `tcp://?listen` blocking
        for 15 s, that would make every recording start visibly hang.
        """
        thread, q, stop = _pyloop.start_tcp_pump(port, _PYLOOP_SAMPLERATE, _PYLOOP_CHANNELS)
        self._pyloop_thread = thread
        self._pyloop_queue = q
        self._pyloop_stop = stop

        try:
            connect_msg = q.get(timeout=_PYLOOP_STARTUP_TIMEOUT_S)
        except queue.Empty:
            connect_msg = TimeoutError("环回 worker 未报告连接结果")

        if isinstance(connect_msg, BaseException):
            logger.error(
                "Python 环回无法连接 FFmpeg（%s）。中止此 FFmpeg 进程，继续无音频录制。",
                connect_msg,
            )
            self._abort_pyloop_and_ffmpeg()
            return

        try:
            stream_msg = q.get(timeout=_PYLOOP_STARTUP_TIMEOUT_S)
        except queue.Empty:
            stream_msg = TimeoutError("环回 worker 已连接但未打开录音器")

        if isinstance(stream_msg, BaseException):
            logger.error(
                "Python 环回已连接 FFmpeg 但无法打开扬声器（%s）。中止并继续无音频录制。",
                stream_msg,
            )
            self._abort_pyloop_and_ffmpeg()
            return

        logger.info("Python 环回正在向 FFmpeg 推流（s16le %d Hz x%d）。",
                    _PYLOOP_SAMPLERATE, _PYLOOP_CHANNELS)

    def _abort_pyloop_and_ffmpeg(self) -> None:
        """Stop the loopback worker and kill FFmpeg so the caller can resort to silent retry."""
        if self._pyloop_stop is not None:
            self._pyloop_stop.set()
        if self._pyloop_thread is not None:
            self._pyloop_thread.join(timeout=2.0)
        self._pyloop_thread = None
        self._pyloop_stop = None
        self._pyloop_queue = None
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass
        # Mark this encoder instance as silent so callers / meta.json reflect reality.
        self._audio_source = None

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

    def _mark_unexpected_exit(self, detail: str) -> None:
        if self._intentional_stop or self._failed:
            return
        self._failed = True
        logger.error("FFmpeg 编码进程异常退出：%s", detail)
        self._dump_stderr()

    def write_frame(self, frame_bytes: bytes) -> None:
        """Write one raw BGR24 frame to the FFmpeg pipe."""
        if self._intentional_stop:
            return
        if self._proc is None or self._proc.stdin is None:
            if not self._failed:
                self._mark_unexpected_exit("进程或 stdin 不可用")
            return
        if self._proc.poll() is not None:
            self._mark_unexpected_exit(f"returncode={self._proc.returncode}")
            return
        if len(frame_bytes) != self._frame_size:
            logger.warning(
                "丢弃帧：字节大小 %d != 预期 %d（宽×高×3）",
                len(frame_bytes),
                self._frame_size,
            )
            return
        try:
            mv = memoryview(frame_bytes)
            while len(mv) > 0:
                n = self._proc.stdin.write(mv[:_STDIN_CHUNK])
                mv = mv[n:]
        except (BrokenPipeError, OSError) as e:
            if not self._stdin_broken_logged:
                self._stdin_broken_logged = True
            self._mark_unexpected_exit(f"stdin 写入失败：{e}")

    def stop(self) -> None:
        """Gracefully close the pipe and wait for FFmpeg to finish."""
        if self._proc is None:
            return
        self._intentional_stop = True
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            # With -shortest, FFmpeg should EOF the audio side once video EOFs,
            # which makes our soundcard worker's sendall raise BrokenPipe and exit.
            # Belt-and-braces: also signal stop explicitly.
            if self._pyloop_stop is not None:
                self._pyloop_stop.set()
            self._proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logger.warning("FFmpeg 未及时退出，正在强制终止")
            self._proc.kill()
        finally:
            if self._proc.returncode and self._proc.returncode != 0:
                self._dump_stderr()
            if self._pyloop_thread is not None:
                self._pyloop_thread.join(timeout=3.0)
                if self._pyloop_thread.is_alive():
                    logger.warning("Python 环回 worker 未及时退出。")
            self._pyloop_thread = None
            self._pyloop_stop = None
            self._pyloop_queue = None
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
            logger.error("FFmpeg stderr：\n%s", err_text)


def _video_encoder_args(encoder: str, cfg: Config) -> list[str]:
    """FFmpeg ``-c:v …`` args for the selected H.264 encoder."""
    q = str(cfg.video_quality)
    if encoder == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            cfg.video_preset,
            "-rc",
            "vbr",
            "-cq",
            q,
        ]
    if encoder == "h264_amf":
        # CQP keeps quality roughly aligned with NVENC CQ / x264 CRF.
        return [
            "-c:v",
            "h264_amf",
            "-usage",
            "transcoding",
            "-quality",
            "balanced",
            "-rc",
            "cqp",
            "-qp_i",
            q,
            "-qp_p",
            q,
        ]
    if encoder == "h264_qsv":
        return [
            "-c:v",
            "h264_qsv",
            "-preset",
            "veryfast",
            "-global_quality",
            q,
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        q,
        "-threads",
        str(max(1, cfg.x264_threads)),
    ]


def _below_normal_priority() -> int:
    """Return process creation flags for below-normal priority on Windows."""
    if sys.platform == "win32":
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        return BELOW_NORMAL_PRIORITY_CLASS
    return 0
