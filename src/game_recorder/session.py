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
import ctypes.wintypes as wt
import logging
import shutil
import statistics
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from game_recorder.capture.input_hook import InputCapture
from game_recorder.capture.screen import ScreenCapture
from game_recorder.capture.window_region import (
    CaptureTarget,
    get_foreground_window_title,
    resolve_capture_target,
)
from game_recorder.config import Config, find_ffmpeg
from game_recorder.encoder.ffmpeg_pipe import FFmpegEncoder
from game_recorder.storage.action_writer import ActionWriter
from game_recorder.storage.idle_trim import apply_idle_tail_trim, idle_tail_trim_frames
from game_recorder.hotkeys import HOTKEY_VKS
from game_recorder.storage.library_index import add_session, effective_duration_s
from game_recorder.storage.session_writer import SegmentMeta, SessionMeta

logger = logging.getLogger(__name__)

# Virtual keys for movement keys (character walk).
_WASD_VKS: frozenset[int] = frozenset((0x57, 0x41, 0x53, 0x44))  # W A S D
AutoStopReason = Literal["idle", "stuck", "forbidden_key", "violent"]
AutoStopCallback = Callable[[AutoStopReason], None]

# Per-second thresholds for violent-input detection (sustained ``violent_duration_s``).
_VIOLENT_WINDOW_S = 1.0
_WASD_DOWN_PER_S = 5  # new WASD presses per second (ignore hold / key-repeat)
_MOUSE_REVERSAL_PER_S = 10  # dx/dy sign flips per second (shake); smooth look rarely hits this


class _ViolenceMonitor:
    """Detect sustained high-frequency WASD tapping or mouse shaking."""

    def __init__(
        self,
        duration_s: float,
        initial_wasd_held: frozenset[int] | None = None,
    ) -> None:
        self._duration_s = duration_s
        self._lock = threading.Lock()
        self._window_start = time.monotonic()
        self._wasd_presses = 0
        self._mouse_moves = 0
        self._mouse_reversals = 0
        self._wasd_held: set[int] = set(initial_wasd_held or ())
        self._last_dx = 0
        self._last_dy = 0
        self._last_abs_x: int | None = None
        self._last_abs_y: int | None = None
        self._violent_since: float | None = None

    def on_wasd_event(self, event: dict, now: float) -> bool:
        with self._lock:
            vk = event.get("vk")
            if not isinstance(vk, int):
                return self._advance(now)
            action = event.get("action")
            if action == "down":
                if vk in self._wasd_held:
                    return self._advance(now)
                self._wasd_held.add(vk)
                self._wasd_presses += 1
            elif action == "up":
                self._wasd_held.discard(vk)
            return self._advance(now)

    def on_mouse_move(self, event: dict, now: float) -> bool:
        with self._lock:
            if event.get("absolute"):
                x = int(event.get("x", 0))
                y = int(event.get("y", 0))
                if self._last_abs_x is not None and self._last_abs_y is not None:
                    dx = x - self._last_abs_x
                    dy = y - self._last_abs_y
                else:
                    dx = dy = 0
                self._last_abs_x = x
                self._last_abs_y = y
            else:
                dx = int(event.get("dx", 0))
                dy = int(event.get("dy", 0))
            if not dx and not dy:
                return self._advance(now)
            self._mouse_moves += 1
            if self._last_dx and dx and ((self._last_dx > 0) != (dx > 0)):
                self._mouse_reversals += 1
            if self._last_dy and dy and ((self._last_dy > 0) != (dy > 0)):
                self._mouse_reversals += 1
            self._last_dx = dx
            self._last_dy = dy
            return self._advance(now)

    def tick(self, now: float) -> bool:
        with self._lock:
            return self._advance(now)

    def _advance(self, now: float) -> bool:
        """Advance time windows; return True when violent input sustained long enough."""
        triggered = False
        while now - self._window_start >= _VIOLENT_WINDOW_S:
            window_end = self._window_start + _VIOLENT_WINDOW_S
            if self._evaluate_window(window_end):
                triggered = True
            self._window_start = window_end
            self._reset_window()
        return triggered

    def _evaluate_window(self, window_end: float) -> bool:
        hot = (
            self._wasd_presses >= _WASD_DOWN_PER_S
            or self._mouse_reversals >= _MOUSE_REVERSAL_PER_S
        )
        if hot:
            if self._violent_since is None:
                self._violent_since = window_end - _VIOLENT_WINDOW_S
            if window_end - self._violent_since >= self._duration_s:
                return True
        else:
            self._violent_since = None
        return False

    def _reset_window(self) -> None:
        self._wasd_presses = 0
        self._mouse_moves = 0
        self._mouse_reversals = 0
        self._last_dx = 0
        self._last_dy = 0


class Session:
    """A single recording session, possibly spanning multiple segments."""

    def __init__(
        self,
        config: Config,
        on_auto_stop: AutoStopCallback | None = None,
    ) -> None:
        self.config = config
        self._on_auto_stop = on_auto_stop
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
        self._capture_target: CaptureTarget | None = None

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
        self._segment_event_count: int = 0

        # Cache the encoder/audio identity so we can write meta.json after
        # the per-segment encoder instance is gone.  Audio routing is the
        # same for every segment (same Config), so we only need one snapshot.
        self._encoder_name: str = ""
        self._audio_source: str | None = None  # events written to current segment

        # Per captured frame: wall_frame_index − dxcam idx (median → meta event_video_sync_offset).
        self._sync_wall_minus_idx: list[int] = []

        # Auto-stop rules (WASD-only movement)
        self._last_movement_at: float = 0.0
        self._last_input_change_at: float = 0.0
        self._wasd_state: frozenset[int] = frozenset()
        self._auto_stop_fired = False
        self._auto_stop_reason: AutoStopReason | None = None
        self._idle_thread: threading.Thread | None = None
        self._violence: _ViolenceMonitor | None = None
        self._stop_finalized = False
        self._stop_kept = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    @property
    def capture_target(self) -> CaptureTarget | None:
        return self._capture_target

    # ── Public lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start all capture components."""
        logger.info("正在启动会话 %s → %s", self._session_id, self._session_dir)

        # Shared clock epoch
        self._t0_ns = time.perf_counter_ns()
        self._t0_epoch_ms = int(time.time() * 1000)
        self._last_movement_at = time.monotonic()
        self._last_input_change_at = time.monotonic()
        self._wasd_state = self._read_wasd_state()
        self._auto_stop_fired = False
        self._auto_stop_reason = None
        self._stop_finalized = False
        self._stop_kept = False
        violent_s = float(self.config.violent_duration_s)
        self._violence = (
            _ViolenceMonitor(violent_s, initial_wasd_held=self._wasd_state)
            if violent_s > 0 and self._on_auto_stop
            else None
        )

        # Probe output size and resolve the capture target before launching the first encoder.
        import dxcam as _dxcam

        probe = _dxcam.create(output_color="BGR")
        output_width, output_height = probe.width, probe.height
        del probe

        self._capture_target = resolve_capture_target(
            self.config.capture_mode,
            output_width,
            output_height,
        )
        if self._capture_target.region is None:
            self._width, self._height = output_width, output_height
        else:
            self._width = self._capture_target.region.width
            self._height = self._capture_target.region.height
        logger.info(
            "检测到输出 %dx%d；录制 %dx%d（%s）",
            output_width,
            output_height,
            self._width,
            self._height,
            self._capture_target.source,
        )

        # Segment sizing
        seg_s = max(0, int(self.config.segment_seconds))
        self._frames_per_segment = self.config.fps * seg_s if seg_s > 0 else 0
        if self._frames_per_segment > 0:
            logger.info(
                "自动分段：每 %d 秒 = %d 帧",
                seg_s,
                self._frames_per_segment,
            )
        else:
            logger.info("自动分段已关闭 — 单文件连续录制")

        # Open first segment
        with self._segment_lock:
            self._open_segment_locked(start_frame=0)

        # Screen capture thread
        self._screen = ScreenCapture(
            fps=self.config.fps,
            on_frame=self._on_frame,
            region=self._capture_target.region if self._capture_target else None,
        )
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

        idle_s = float(self.config.idle_timeout_s)
        watch_idle = idle_s > 0 and self._on_auto_stop is not None
        watch_violent = self._violence is not None
        if watch_idle or watch_violent:
            self._idle_thread = threading.Thread(
                target=self._auto_stop_watch_loop,
                args=(idle_s,),
                name="auto-stop-watch",
                daemon=True,
            )
            self._idle_thread.start()
            if watch_idle:
                logger.info("空闲检测已启用：%g 秒未移动人物角色（无 WASD）将自动停止", idle_s)
                logger.info(
                    "僵滞检测已启用：%g 秒 WASD 按键状态未变化且无鼠标移动将自动停止",
                    idle_s,
                )
        if self._on_auto_stop is not None:
            logger.info("非 WASD 按键检测已启用：按下其他键将自动停止")
            if watch_violent:
                logger.info(
                    "剧烈操作检测已启用：WASD 或鼠标高频晃动连续 %g 秒将自动停止",
                    violent_s,
                )

        fg = self._capture_target.title if self._capture_target else get_foreground_window_title()
        encoder_name = self._encoder.encoder_name if self._encoder else "?"
        logger.info(
            "会话已启动 — 编码器=%s，fps=%d，前台窗口=%r",
            encoder_name,
            self.config.fps,
            fg,
        )

    def stop(self) -> bool:
        """Signal threads to stop, finalize the active segment, write metadata.

        Returns True if the session was kept, False if it was discarded as too short.
        """
        if self._stop_finalized:
            return self._stop_kept

        logger.info("正在停止会话 %s …", self._session_id)
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
        wall_duration_s = (time.perf_counter_ns() - self._t0_ns) / 1e9
        video_duration_s = self._frame_count / max(1, self.config.fps)

        min_s = float(self.config.min_recording_duration_s)
        effective_s = self._effective_recording_duration_s(wall_duration_s)
        if min_s > 0 and effective_s < min_s:
            self._discard_session(wall_duration_s, effective_s, min_s)
            self._stop_finalized = True
            self._stop_kept = False
            return False

        idle_tail_trimmed = 0
        if self._auto_stop_reason in ("idle", "stuck"):
            trim_duration_s = float(self.config.idle_timeout_s)
        elif self._auto_stop_reason == "violent":
            trim_duration_s = float(self.config.violent_duration_s)
        else:
            trim_duration_s = 0.0
        if trim_duration_s > 0 and self._segments_meta:
            trim_n = idle_tail_trim_frames(trim_duration_s, self.config.fps)
            if trim_n > 0:
                self._segments_meta, idle_tail_trimmed = apply_idle_tail_trim(
                    self._session_dir,
                    self._segments_meta,
                    trim_frames=trim_n,
                    fps=self.config.fps,
                    session_timestamp=self._session_timestamp,
                    ffmpeg_path=find_ffmpeg(),
                )
                if idle_tail_trimmed > 0:
                    self._frame_count -= idle_tail_trimmed
                    video_duration_s = self._frame_count / max(1, self.config.fps)

        if min_s > 0 and video_duration_s < min_s:
            self._discard_session(
                wall_duration_s,
                video_duration_s,
                min_s,
                after_tail_trim=idle_tail_trimmed > 0,
            )
            self._stop_finalized = True
            self._stop_kept = False
            return False

        duration_s = video_duration_s
        total_events = sum(s.event_count for s in self._segments_meta)

        sync_off = 0
        if self._sync_wall_minus_idx:
            sync_off = int(round(statistics.median(self._sync_wall_minus_idx)))
            logger.info(
                "event_video_sync_offset=%d（wall−idx 中位数，n=%d 帧）",
                sync_off,
                len(self._sync_wall_minus_idx),
            )

        meta = SessionMeta(
            session_id=self._session_id,
            session_timestamp=self._session_timestamp,
            start_epoch_ms=self._t0_epoch_ms,
            duration_s=round(duration_s, 2),
            fps=self.config.fps,
            event_video_sync_offset=sync_off,
            resolution=[self._width, self._height],
            encoder=self._encoder_name,
            audio_source=self._audio_source,
            foreground_window=(
                self._capture_target.title
                if self._capture_target
                else get_foreground_window_title()
            ),
            capture_source=self._capture_target.source if self._capture_target else "screen",
            capture_region=(
                list(self._capture_target.region.as_dxcam_region())
                if self._capture_target and self._capture_target.region
                else None
            ),
            total_frames=self._frame_count,
            total_input_events=total_events,
            segment_seconds=int(self.config.segment_seconds),
            segments=self._segments_meta,
            auto_stop_reason=self._auto_stop_reason,
            idle_timeout_s=float(self.config.idle_timeout_s),
            violent_duration_s=float(self.config.violent_duration_s),
            idle_tail_trim_frames=idle_tail_trimmed,
        )
        meta.save(self._meta_path)
        try:
            add_session(self.config.output_dir, meta)
        except Exception as exc:
            logger.warning("更新库索引失败：%s", exc)

        logger.info(
            "会话 %s 已保存：%.1f 秒，%d 帧，%d 个输入事件，%d 个分段",
            self._session_id,
            duration_s,
            self._frame_count,
            total_events,
            len(self._segments_meta),
        )
        self._stop_finalized = True
        self._stop_kept = True
        return True

    def _effective_recording_duration_s(self, wall_duration_s: float) -> float:
        """Wall duration minus auto-stop tail (idle wait or violent-input window)."""
        return effective_duration_s(
            wall_duration_s,
            auto_stop_reason=self._auto_stop_reason,
            idle_timeout_s=self.config.idle_timeout_s,
            violent_duration_s=self.config.violent_duration_s,
        )

    def _auto_stop_tail_deduct_s(self) -> float:
        if self._auto_stop_reason in ("idle", "stuck"):
            return float(self.config.idle_timeout_s)
        if self._auto_stop_reason == "violent":
            return float(self.config.violent_duration_s)
        return 0.0

    def _discard_session(
        self,
        wall_duration_s: float,
        effective_duration_s: float,
        min_s: float,
        *,
        after_tail_trim: bool = False,
    ) -> None:
        """Remove session directory and skip library index (recording too short)."""
        tail_deduct = self._auto_stop_tail_deduct_s()
        if after_tail_trim:
            logger.info(
                "会话 %s 裁剪后视频时长 %.1f 秒 < %.1f 秒，丢弃录制数据",
                self._session_id,
                effective_duration_s,
                min_s,
            )
        elif effective_duration_s < wall_duration_s - 0.05 and tail_deduct > 0:
            logger.info(
                "会话 %s 有效时长 %.1f 秒 < %.1f 秒"
                "（总时长 %.1f 秒，已扣除自动停止尾部 %.1f 秒），丢弃录制数据",
                self._session_id,
                effective_duration_s,
                min_s,
                wall_duration_s,
                tail_deduct,
            )
        else:
            logger.info(
                "会话 %s 时长 %.1f 秒 < %.1f 秒，丢弃录制数据",
                self._session_id,
                effective_duration_s,
                min_s,
            )
        session_dir = self._session_dir
        try:
            shutil.rmtree(session_dir)
        except OSError as exc:
            logger.warning("删除过短会话目录失败 %s：%s", session_dir, exc)

    # ── Frame & event callbacks ──────────────────────────────────────────

    def _on_frame(self, frame_bytes: bytes, idx: int, width: int, height: int) -> None:
        """Called by the screen-capture thread for every captured frame."""
        with self._segment_lock:
            wall = int(
                (time.perf_counter_ns() - self._t0_ns)
                * int(max(1, self.config.fps))
                // 1_000_000_000
            )
            if len(self._sync_wall_minus_idx) < 100_000:
                self._sync_wall_minus_idx.append(wall - idx)

            self._frame_count = idx + 1

            if (width, height) != (self._width, self._height):
                logger.warning(
                    "捕获分辨率从 %dx%d 变为 %dx%d；正在轮换分段",
                    self._width,
                    self._height,
                    width,
                    height,
                )
                self._close_segment_locked(actual_end_frame=idx)
                self._width = width
                self._height = height
                self._open_segment_locked(start_frame=idx)
                self._drain_pending_events_locked()

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
        now = time.monotonic()
        if self._violence is not None:
            triggered = False
            if self._is_wasd_event(event):
                triggered = self._violence.on_wasd_event(event, now)
            elif event.get("type") == "mouse" and event.get("action") == "move":
                triggered = self._violence.on_mouse_move(event, now)
            else:
                triggered = self._violence.tick(now)
            if triggered:
                self._trigger_auto_stop("violent")

        if self._is_wasd_event(event):
            self._last_movement_at = now
            prev_state = self._wasd_state
            self._wasd_state = self._apply_wasd_event(prev_state, event)
            if self._wasd_state != prev_state:
                self._last_input_change_at = now
        elif event.get("type") == "mouse" and event.get("action") == "move":
            self._last_input_change_at = now

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
                    "丢弃已关闭分段的滞后事件（frame=%d，当前=[%d,%s)）",
                    frame,
                    self._segment_start_frame,
                    self._segment_planned_end,
                )
                return

            if self._action_writer is not None:
                self._action_writer.write(event)
                self._segment_event_count += 1

        if self._is_forbidden_input_event(event):
            self._trigger_auto_stop("forbidden_key")

    @staticmethod
    def _is_wasd_event(event: dict) -> bool:
        if event.get("type") != "key":
            return False
        vk = event.get("vk")
        return isinstance(vk, int) and vk in _WASD_VKS

    def _is_forbidden_input_event(self, event: dict) -> bool:
        if event.get("seed"):
            return False
        if event.get("type") == "mouse":
            return event.get("action") != "move"
        if event.get("type") != "key" or event.get("action") != "down":
            return False
        vk = event.get("vk")
        if not isinstance(vk, int):
            return False
        if vk in _WASD_VKS:
            return False
        if vk in HOTKEY_VKS:
            return False
        return True

    @staticmethod
    def _apply_wasd_event(state: frozenset[int], event: dict) -> frozenset[int]:
        vk = event.get("vk")
        if not isinstance(vk, int):
            return state
        keys = set(state)
        if event.get("action") == "down":
            keys.add(vk)
        elif event.get("action") == "up":
            keys.discard(vk)
        return frozenset(keys)

    @staticmethod
    def _read_wasd_state() -> frozenset[int]:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = wt.SHORT
        return frozenset(
            vk for vk in _WASD_VKS if bool(user32.GetAsyncKeyState(vk) & 0x8000)
        )

    @staticmethod
    def _wasd_held() -> bool:
        return bool(Session._read_wasd_state())

    def _auto_stop_watch_loop(self, idle_s: float) -> None:
        while not self._stop_event.wait(0.5):
            now = time.monotonic()
            if self._violence is not None and self._violence.tick(now):
                self._trigger_auto_stop("violent")
                return
            if idle_s <= 0:
                continue
            if self._wasd_held():
                if now - self._last_input_change_at >= idle_s:
                    self._trigger_auto_stop("stuck")
                    return
                self._last_movement_at = now
                continue
            if now - self._last_movement_at < idle_s:
                continue
            self._trigger_auto_stop("idle")
            return

    def _trigger_auto_stop(self, reason: AutoStopReason) -> None:
        if self._auto_stop_fired or self._on_auto_stop is None:
            return
        self._auto_stop_fired = True
        self._auto_stop_reason = reason
        self._stop_event.set()
        if reason == "idle":
            logger.info("检测到 %g 秒未移动人物角色（无 WASD），触发自动停止", self.config.idle_timeout_s)
        elif reason == "stuck":
            logger.info(
                "检测到 %g 秒 WASD 按键状态未变化且无鼠标移动，触发自动停止",
                self.config.idle_timeout_s,
            )
        elif reason == "violent":
            logger.info(
                "检测到连续 %g 秒高频 WASD / 鼠标晃动，触发自动停止",
                self.config.violent_duration_s,
            )
        else:
            logger.info("检测到非人物移动操作（按键或鼠标点击/滚轮），触发自动停止")
        self._on_auto_stop(reason)

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

        # Snapshot identity from the first segment (same for all subsequent ones).
        if not self._encoder_name:
            self._encoder_name = self._encoder.encoder_name
            self._audio_source = self._encoder.audio_source

        logger.info(
            "已打开分段 #%d：帧 [%d, %s) → %s",
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
                logger.warning("停止编码器时出错：%s", e)
            self._encoder = None

        if self._action_writer is not None:
            try:
                self._action_writer.close()
            except Exception as e:
                logger.warning("关闭操作日志写入器时出错：%s", e)
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
                        logger.warning("重命名失败 %s → %s：%s", src.name, dst.name, e)
            final_video = new_video
            final_actions = new_actions
        else:
            final_video = self._segment_video_path or Path()
            final_actions = self._segment_actions_path or Path()

        frame_count = actual_end_frame - self._segment_start_frame
        if frame_count <= 0:
            for path in (final_video, final_actions):
                try:
                    if path.exists():
                        path.unlink()
                except OSError as e:
                    logger.warning("删除空分段文件失败 %s：%s", path.name, e)
            logger.info(
                "已丢弃空分段 #%d：帧 [%d, %d)",
                self._segment_index,
                self._segment_start_frame,
                actual_end_frame,
            )
            self._segment_video_path = None
            self._segment_actions_path = None
            self._segment_planned_end = None
            return

        self._segments_meta.append(
            SegmentMeta(
                index=self._segment_index,
                start_frame=self._segment_start_frame,
                end_frame=actual_end_frame,
                frame_count=frame_count,
                event_count=self._segment_event_count,
                video=final_video.name,
                actions=final_actions.name,
            )
        )

        logger.info(
            "已关闭分段 #%d：帧 [%d, %d) → %s（%d 个事件）",
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
