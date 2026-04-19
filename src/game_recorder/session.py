"""Session lifecycle: coordinates screen capture, FFmpeg encoder, and input hooks.

All components share a single T0 epoch (perf_counter_ns) so their timestamps
are directly comparable.  The orchestration order is:

  1. Create session directory & writers
  2. Record T0 (shared clock epoch)
  3. Start FFmpeg encoder subprocess
  4. Start screen-capture thread (feeds frames to FFmpeg pipe)
  5. Start input-hook thread (writes to JSONL)
  6. Wait for stop signal
  7. Tear down in reverse order, write meta.json
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from game_recorder.capture.input_hook import InputCapture
from game_recorder.capture.screen import ScreenCapture
from game_recorder.config import Config
from game_recorder.encoder.ffmpeg_pipe import FFmpegEncoder
from game_recorder.storage.action_writer import ActionWriter
from game_recorder.storage.session_writer import SessionMeta

logger = logging.getLogger(__name__)


def _get_foreground_window_title() -> str:
    """Best-effort: return the title of the current foreground window."""
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value
    except Exception:
        pass
    return ""


class Session:
    """A single recording session."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._stop_event = threading.Event()

        # Identifiers
        self._session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._session_dir = config.output_dir / self._session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)

        # Paths
        self._video_path = self._session_dir / "video.mp4"
        self._actions_path = self._session_dir / "actions.jsonl"
        self._meta_path = self._session_dir / "meta.json"

        # Components (created on start)
        self._encoder: FFmpegEncoder | None = None
        self._screen: ScreenCapture | None = None
        self._input: InputCapture | None = None
        self._action_writer: ActionWriter | None = None

        self._screen_thread: threading.Thread | None = None
        self._input_thread: threading.Thread | None = None

        # Clock
        self._t0_ns: int = 0
        self._t0_epoch_ms: int = 0

        # Stats
        self._frame_count = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    def start(self) -> None:
        """Start all capture components."""
        logger.info("Starting session %s → %s", self._session_id, self._session_dir)

        # ── Shared clock epoch ───────────────────────────────────────────
        self._t0_ns = time.perf_counter_ns()
        self._t0_epoch_ms = int(time.time() * 1000)

        # ── Action writer ────────────────────────────────────────────────
        self._action_writer = ActionWriter(self._actions_path)

        # ── Screen capture (probe resolution before starting encoder) ───
        import dxcam as _dxcam

        probe = _dxcam.create(output_color="BGR")
        width, height = probe.width, probe.height
        del probe
        logger.info("Detected screen resolution: %dx%d", width, height)

        # ── FFmpeg encoder ───────────────────────────────────────────────
        self._encoder = FFmpegEncoder(self.config)
        self._encoder.start(width, height, self._video_path)

        # ── Screen capture thread ────────────────────────────────────────
        self._frame_count = 0

        def _on_frame(frame_bytes: bytes, idx: int) -> None:
            self._frame_count = idx + 1
            if self._encoder:
                self._encoder.write_frame(frame_bytes)

        self._screen = ScreenCapture(fps=self.config.fps, on_frame=_on_frame)
        self._screen_thread = threading.Thread(
            target=self._screen.run,
            args=(self._stop_event,),
            name="screen-capture",
            daemon=True,
        )
        self._screen_thread.start()

        # ── Input hook thread ────────────────────────────────────────────
        self._input = InputCapture(
            t0_ns=self._t0_ns,
            on_event=self._action_writer.write,
            mouse_throttle_ms=self.config.mouse_poll_interval_ms,
        )
        self._input_thread = threading.Thread(
            target=self._input.run,
            args=(self._stop_event,),
            name="input-hooks",
            daemon=True,
        )
        self._input_thread.start()

        fg = _get_foreground_window_title()
        logger.info(
            "Session started — encoder=%s, fps=%d, foreground=%r",
            self._encoder.encoder_name,
            self.config.fps,
            fg,
        )

    def stop(self) -> None:
        """Signal all threads to stop, wait for teardown, write metadata."""
        logger.info("Stopping session %s …", self._session_id)
        self._stop_event.set()

        # Wait for capture threads
        if self._screen_thread and self._screen_thread.is_alive():
            self._screen_thread.join(timeout=5)
        if self._input_thread and self._input_thread.is_alive():
            self._input_thread.join(timeout=5)

        # Close encoder (flushes pipe)
        if self._encoder:
            self._encoder.stop()

        # Close action writer (flushes buffer)
        total_events = 0
        if self._action_writer:
            self._action_writer.close()
            total_events = self._action_writer.total_written

        # Compute duration
        duration_s = (time.perf_counter_ns() - self._t0_ns) / 1e9

        # Detect resolution from screen capture
        w = self._screen.width if self._screen else 0
        h = self._screen.height if self._screen else 0

        # Write meta.json
        meta = SessionMeta(
            session_id=self._session_id,
            start_epoch_ms=self._t0_epoch_ms,
            duration_s=round(duration_s, 2),
            fps=self.config.fps,
            resolution=[w, h],
            encoder=self._encoder.encoder_name if self._encoder else "",
            foreground_window=_get_foreground_window_title(),
            total_frames=self._frame_count,
            total_input_events=total_events,
        )
        meta.save(self._meta_path)

        logger.info(
            "Session %s saved: %.1fs, %d frames, %d input events",
            self._session_id,
            duration_s,
            self._frame_count,
            total_events,
        )
