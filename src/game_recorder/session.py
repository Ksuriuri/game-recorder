"""Session lifecycle: coordinates screen capture, FFmpeg encoder, and input hooks.

All components share a single T0 epoch (perf_counter_ns) so their timestamps
are directly comparable.  A session may be split into multiple *segments*
(every ``config.segment_seconds`` seconds → ``fps * segment_seconds`` frames):
each segment produces its own ``mp4`` + ``jsonl`` pair under the session
directory, named ``{session_timestamp}_{start_frame}_{end_frame}``.

The orchestration order is:

  1. Create session directory & shared T0 clock
  2. Open the first segment (encoder + action writer)
  3. Start screen-capture thread (drives segment rotation when boundary hit)
  4. Start input-hook thread (events routed to the matching segment)
  5. Wait for stop signal
  6. Tear down: finalize current segment, rename if its actual end differs
     from the planned end, then write meta.json
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
from game_recorder.storage.session_writer import SegmentMeta, SessionMeta

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
    """A single recording session, possibly spanning multiple segments."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._stop_event = threading.Event()

        # Identifiers
        self._session_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_id = f"session_{self._session_timestamp}"
        self._session_dir = config.output_dir / self._session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)

        self._meta_path = self._session_dir / "meta.json"

        # Components (created on start)
        self._screen: ScreenCapture | None = None
        self._input: InputCapture | None = None

        self._screen_thread: threading.Thread | None = None
        self._input_thread: threading.Thread | None = None

        # Clock
        self._t0_ns: int = 0
        self._t0_epoch_ms: int = 0

        # Resolution (probed at start)
        self._width: int = 0
        self._height: int = 0

        # ── Segment state (guarded by _segment_lock) ─────────────────────
        self._segment_lock = threading.Lock()
        self._frames_per_segment: int = 0  # 0 → no segmentation
        self._segment_index: int = 0
        self._segment_start_frame: int = 0
        # Planned end = segment_start_frame + frames_per_segment (None when unbounded)
        self._segment_planned_end: int | None = None

        self._encoder: FFmpegEncoder | None = None
        self._action_writer: ActionWriter | None = None
        self._segment_video_path: Path | None = None
        self._segment_actions_path: Path | None = None

        # Buffer for events whose frame already crossed the boundary while
        # the screen-capture thread has not yet rotated to the next segment.
        self._pending_events: list[dict] = []

        # Per-segment & total stats
        self._frame_count = 0  # global cumulative frame count (across segments)
        self._segments_meta: list[SegmentMeta] = []
        self._segment_event_count: int = 0  # events written to current segment

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    # ── Public lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start all capture components."""
        logger.info("Starting session %s → %s", self._session_id, self._session_dir)

        # Shared clock epoch
        self._t0_ns = time.perf_counter_ns()
        self._t0_epoch_ms = int(time.time() * 1000)

        # Probe screen resolution before launching the first encoder
        import dxcam as _dxcam

        probe = _dxcam.create(output_color="BGR")
        self._width, self._height = probe.width, probe.height
        del probe
        logger.info("Detected screen resolution: %dx%d", self._width, self._height)

        # Segment sizing
        seg_s = max(0, int(self.config.segment_seconds))
        self._frames_per_segment = self.config.fps * seg_s if seg_s > 0 else 0
        if self._frames_per_segment > 0:
            logger.info(
                "Auto-segment: every %d s = %d frames",
                seg_s,
                self._frames_per_segment,
            )
        else:
            logger.info("Auto-segment disabled — single continuous file")

        # Open first segment
        with self._segment_lock:
            self._open_segment_locked(start_frame=0)

        # Screen capture thread
        self._screen = ScreenCapture(fps=self.config.fps, on_frame=self._on_frame)
        self._screen_thread = threading.Thread(
            target=self._screen.run,
            args=(self._stop_event,),
            name="screen-capture",
            daemon=True,
        )
        self._screen_thread.start()

        # Input hook thread
        self._input = InputCapture(
            t0_ns=self._t0_ns,
            fps=self.config.fps,
            on_event=self._on_action_event,
            mouse_throttle_ms=self.config.mouse_poll_interval_ms,
            keyboard_poll_hz=self.config.keyboard_poll_hz,
        )
        self._input_thread = threading.Thread(
            target=self._input.run,
            args=(self._stop_event,),
            name="input-hooks",
            daemon=True,
        )
        self._input_thread.start()

        fg = _get_foreground_window_title()
        encoder_name = self._encoder.encoder_name if self._encoder else "?"
        logger.info(
            "Session started — encoder=%s, fps=%d, foreground=%r",
            encoder_name,
            self.config.fps,
            fg,
        )

    def stop(self) -> None:
        """Signal threads to stop, finalize the active segment, write metadata."""
        logger.info("Stopping session %s …", self._session_id)
        self._stop_event.set()

        # Wait for capture threads to drain
        if self._screen_thread and self._screen_thread.is_alive():
            self._screen_thread.join(timeout=5)
        if self._input_thread and self._input_thread.is_alive():
            self._input_thread.join(timeout=5)

        # Finalize the in-progress segment
        with self._segment_lock:
            self._close_segment_locked(actual_end_frame=self._frame_count)

        # Compute duration
        duration_s = (time.perf_counter_ns() - self._t0_ns) / 1e9

        total_events = sum(s.event_count for s in self._segments_meta)

        meta = SessionMeta(
            session_id=self._session_id,
            session_timestamp=self._session_timestamp,
            start_epoch_ms=self._t0_epoch_ms,
            duration_s=round(duration_s, 2),
            fps=self.config.fps,
            resolution=[self._width, self._height],
            encoder=self._encoder.encoder_name if self._encoder else "",
            foreground_window=_get_foreground_window_title(),
            total_frames=self._frame_count,
            total_input_events=total_events,
            segment_seconds=int(self.config.segment_seconds),
            segments=self._segments_meta,
        )
        meta.save(self._meta_path)

        logger.info(
            "Session %s saved: %.1fs, %d frames, %d input events, %d segment(s)",
            self._session_id,
            duration_s,
            self._frame_count,
            total_events,
            len(self._segments_meta),
        )

    # ── Frame & event callbacks ──────────────────────────────────────────

    def _on_frame(self, frame_bytes: bytes, idx: int) -> None:
        """Called by the screen-capture thread for every captured frame."""
        with self._segment_lock:
            self._frame_count = idx + 1

            # Rotate when this frame is the start of the next segment
            if (
                self._segment_planned_end is not None
                and idx >= self._segment_planned_end
            ):
                self._close_segment_locked(actual_end_frame=idx)
                self._open_segment_locked(start_frame=idx)
                self._drain_pending_events_locked()

            if self._encoder is not None:
                self._encoder.write_frame(frame_bytes)

    def _on_action_event(self, event: dict) -> None:
        """Called by the input-hook thread for every keyboard / mouse event."""
        frame = event.get("frame", 0)
        with self._segment_lock:
            # Event for a future segment that hasn't rotated yet → buffer it
            if (
                self._segment_planned_end is not None
                and frame >= self._segment_planned_end
            ):
                self._pending_events.append(event)
                return

            # Event for an already-finalized segment → drop with warning
            if frame < self._segment_start_frame:
                logger.debug(
                    "Dropping late event for closed segment (frame=%d, current=[%d,%s))",
                    frame,
                    self._segment_start_frame,
                    self._segment_planned_end,
                )
                return

            if self._action_writer is not None:
                self._action_writer.write(event)
                self._segment_event_count += 1

    # ── Segment management (must be called with _segment_lock held) ─────

    def _segment_paths(self, start_frame: int, end_frame: int) -> tuple[Path, Path]:
        """Build (video, actions) paths for a segment with the given frame range."""
        base = f"{self._session_timestamp}_{start_frame}_{end_frame}"
        return (
            self._session_dir / f"{base}.mp4",
            self._session_dir / f"{base}.jsonl",
        )

    def _open_segment_locked(self, start_frame: int) -> None:
        """Allocate filenames and start a new encoder + action writer."""
        if self._frames_per_segment > 0:
            planned_end = start_frame + self._frames_per_segment
        else:
            # Unbounded: use a placeholder; the file will be renamed on close.
            planned_end = -1

        video_path, actions_path = self._segment_paths(start_frame, planned_end)

        self._segment_start_frame = start_frame
        self._segment_planned_end = (
            planned_end if self._frames_per_segment > 0 else None
        )
        self._segment_video_path = video_path
        self._segment_actions_path = actions_path
        self._segment_event_count = 0

        self._action_writer = ActionWriter(actions_path)
        self._encoder = FFmpegEncoder(self.config)
        self._encoder.start(self._width, self._height, video_path)

        logger.info(
            "Opened segment #%d: frames [%d, %s) → %s",
            self._segment_index,
            start_frame,
            "∞" if self._segment_planned_end is None else str(self._segment_planned_end),
            video_path.name,
        )

    def _close_segment_locked(self, actual_end_frame: int) -> None:
        """Finalize current encoder + action writer; rename files if needed."""
        if self._encoder is None and self._action_writer is None:
            return

        # Tear down encoder (blocks until ffmpeg finalizes the moov atom)
        if self._encoder is not None:
            try:
                self._encoder.stop()
            except Exception as e:
                logger.warning("Error stopping encoder: %s", e)
            self._encoder = None

        if self._action_writer is not None:
            try:
                self._action_writer.close()
            except Exception as e:
                logger.warning("Error closing action writer: %s", e)
            self._action_writer = None

        # Rename when the actual end frame differs from the placeholder /
        # planned end baked into the filename.
        if (
            self._segment_video_path is not None
            and self._segment_actions_path is not None
        ):
            new_video, new_actions = self._segment_paths(
                self._segment_start_frame, actual_end_frame
            )
            for src, dst in (
                (self._segment_video_path, new_video),
                (self._segment_actions_path, new_actions),
            ):
                if src == dst:
                    continue
                if not src.exists():
                    continue
                for attempt in range(6):
                    try:
                        src.rename(dst)
                        break
                    except OSError as e:
                        if attempt < 5:
                            time.sleep(0.15)
                            continue
                        logger.warning("Failed to rename %s → %s: %s", src.name, dst.name, e)
            final_video = new_video
            final_actions = new_actions
        else:
            final_video = self._segment_video_path or Path()
            final_actions = self._segment_actions_path or Path()

        self._segments_meta.append(
            SegmentMeta(
                index=self._segment_index,
                start_frame=self._segment_start_frame,
                end_frame=actual_end_frame,
                frame_count=actual_end_frame - self._segment_start_frame,
                event_count=self._segment_event_count,
                video=final_video.name,
                actions=final_actions.name,
            )
        )

        logger.info(
            "Closed segment #%d: frames [%d, %d) → %s (%d events)",
            self._segment_index,
            self._segment_start_frame,
            actual_end_frame,
            final_video.name,
            self._segment_event_count,
        )

        self._segment_index += 1
        self._segment_video_path = None
        self._segment_actions_path = None
        self._segment_planned_end = None

    def _drain_pending_events_locked(self) -> None:
        """Flush buffered events into whichever segment now owns their frame."""
        if not self._pending_events:
            return
        carry: list[dict] = []
        for ev in self._pending_events:
            f = ev.get("frame", 0)
            if (
                self._segment_planned_end is not None
                and f >= self._segment_planned_end
            ):
                # Still in the future of the new segment too → keep buffering
                carry.append(ev)
            elif f < self._segment_start_frame:
                # Should not happen, but be defensive
                continue
            else:
                if self._action_writer is not None:
                    self._action_writer.write(ev)
                    self._segment_event_count += 1
        self._pending_events = carry
