"""Windows: capture default playback (WASAPI loopback) with soundcard, stream s16le to FFmpeg.

FFmpeg’s static win64 builds often lack the ``wasapi`` demuxer; this path keeps system/game
audio automatic for normal shared-mode output on Windows 10+.
"""

from __future__ import annotations

import logging
import queue
import socket
import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from subprocess import Popen  # type: ignore[type-arg]

if sys.platform == "win32":
    import numpy as np
else:  # pragma: no cover
    np = None  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

# Maximum wait (µs) for a client to *connect* to the FFmpeg TCP listener; after that the stream
# is already established and this does not limit record duration.
_FFMEPG_TCP_LISTEN_US = 15_000_000
_RECORD_FRAMES = 1024


def loopback_usable() -> bool:
    """True if we can open the default *speaker* loopback stream (WASAPI)."""
    if sys.platform != "win32":
        return False
    try:
        import soundcard as sc  # noqa: PLC0415
    except ImportError:
        return False
    try:
        spk = sc.default_speaker()
        sc.get_microphone(id=str(spk.id), include_loopback=True)  # noqa: F841
    except (IndexError, OSError, RuntimeError) as e:
        logger.debug("soundcard loopback not available: %s", e)
        return False
    return True


def _f32_nch_to_s16le_interleaved(frames: object) -> bytes:
    if np is None:
        raise RuntimeError("numpy required for python loopback")
    a = np.asarray(frames, dtype=np.float32)
    if a.ndim == 1:
        a = a[:, np.newaxis]
    a = np.clip(a * 32767.0, -32768.0, 32767.0).astype(np.int16)
    if a.size == 0:
        return b""
    return a.tobytes()


def _resolve_loopback(sc: object) -> object:
    """Return a *Microphone* (loopback) for the current default *speaker*."""
    spk = sc.default_speaker()
    for attempt in (str(spk.id), str(spk.name), spk.name):
        try:
            return sc.get_microphone(attempt, include_loopback=True)
        except (IndexError, OSError, RuntimeError):
            continue
    mics = [m for m in sc.all_microphones(include_loopback=True) if getattr(m, "isloopback", False)]
    for m in mics:
        mname = str(getattr(m, "name", ""))
        if str(spk.id) in mname or str(spk.name) in mname or spk.name in mname:
            return m
    if mics:
        return mics[0]
    raise IndexError("no loopback device for default speaker")


def _audio_worker(
    port: int,
    samplerate: int,
    num_channels: int,
    out_q: queue.Queue[Exception | type[None] | str],
) -> None:
    """Connect to FFmpeg TCP, open loopback, stream until *stop* is set."""
    s: socket.socket | None = None
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=30.0)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, socket.timeout) as e:
        out_q.put(e)
        return
    out_q.put(None)  # TCP is up; ffmpeg can then open rawvideo and wait for BGR
    err: Exception | None = None
    try:
        import soundcard as sc  # noqa: PLC0415

        mic = _resolve_loopback(sc)
        nch = min(int(num_channels), int(getattr(mic, "channels", 2)))
        if nch < 1:
            nch = 2
        with mic.recorder(samplerate=samplerate, channels=nch) as rec:
            out_q.put("streaming")
            # Pump until parent closes the socket (FFmpeg stop) or stop is implicit via BrokenPipe
            while True:
                data = rec.record(numframes=_RECORD_FRAMES)
                s.sendall(_f32_nch_to_s16le_interleaved(data))
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
        pass
    except Exception as e:
        err = e
    finally:
        if s is not None:
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        if err is not None:
            try:
                out_q.put(err)
            except Exception:
                pass


def start_tcp_pump(
    port: int,
    samplerate: int,
    channels: int,
) -> tuple[threading.Thread, queue.Queue[Exception | type[None] | str], threading.Event]:
    """Start daemon thread. Queue protocol: *None* = connected; *\"streaming\"* = mic open; *Exception* = error."""
    stop = threading.Event()
    out_q: queue.Queue[Exception | type[None] | str] = queue.Queue(
        maxsize=8
    )  # "streaming" and errors

    t = threading.Thread(
        target=_audio_worker,
        name="python-loopback-audio",
        args=(port, samplerate, channels, out_q),
        daemon=True,
    )
    t.start()
    return t, out_q, stop


def tcp_ffmpeg_input_args(port: int, samplerate: int, channels: int) -> list[str]:
    tmo = _FFMEPG_TCP_LISTEN_US
    url = f"tcp://127.0.0.1:{port}?listen&listen_timeout={tmo}"
    return [
        "-thread_queue_size",
        "4096",
        "-f",
        "s16le",
        "-ar",
        str(samplerate),
        "-ac",
        str(channels),
        "-i",
        url,
    ]


def free_tcp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p: int = s.getsockname()[1]
    s.close()
    return p
