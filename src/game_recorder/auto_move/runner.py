"""Background auto-move loop tied to a recording session."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from game_recorder.auto_move.input_inject import InputInjector, set_timer_resolution_1ms
from game_recorder.auto_move.policy_wander import WanderPolicy, apply_action
from game_recorder.auto_move.pose_live import LivePoseReader, default_auto_move_sources
from game_recorder.camera_sync import CameraSource
from game_recorder.capture.window_region import restore_window_focus

logger = logging.getLogger(__name__)

# Policy / pose decisions run slower than mouse inject. Mouse stream needs high Hz
# so games that sample look every frame do not see stair-steps.
_POLICY_HZ = 30.0
_DEFAULT_INJECT_HZ = 250.0
_PIXELS_PER_DEG = 6.0


class AutoMoveRunner:
    """Drive WASD + mouse look while a session is recording."""

    def __init__(
        self,
        *,
        output_dir: Path,
        session_dir: Path,
        sources: tuple[CameraSource, ...] | None = None,
        tick_hz: float = _DEFAULT_INJECT_HZ,
        policy: WanderPolicy | None = None,
        hwnd: int | None = None,
        title: str = "",
        pixels_per_deg: float = _PIXELS_PER_DEG,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._session_dir = Path(session_dir)
        self._sources = sources if sources is not None else default_auto_move_sources()
        # tick_hz = mouse inject rate (policy runs at _POLICY_HZ internally).
        self._tick_hz = max(60.0, float(tick_hz))
        self._policy = policy or WanderPolicy()
        self._pixels_per_deg = max(1.0, float(pixels_per_deg))
        self._hwnd = hwnd
        self._title = title or ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._injector = InputInjector()
        self._pose = LivePoseReader(
            output_dir=self._output_dir,
            session_dir=self._session_dir,
            sources=self._sources,
        )

    @property
    def policy(self) -> WanderPolicy:
        return self._policy

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._policy.reset()
        self._thread = threading.Thread(
            target=self._run,
            name="auto-move",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "自动移动已启动（注入 %.0f Hz / 策略 %.0f Hz，相机源=%s）",
            self._tick_hz,
            _POLICY_HZ,
            ",".join(s.key for s in self._sources) or "none",
        )

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        try:
            self._injector.release_all()
        except OSError as exc:
            logger.warning("释放自动移动按键失败：%s", exc)
        logger.info("自动移动已停止")

    def _run(self) -> None:
        inject_interval = 1.0 / self._tick_hz
        policy_interval = 1.0 / _POLICY_HZ
        set_timer_resolution_1ms(enabled=True)
        try:
            self._pose.wait_for_pose(timeout_s=2.0)
            if self._hwnd or self._title:
                restore_window_focus(hwnd=self._hwnd, title=self._title)

            last = time.perf_counter()
            next_policy_at = last
            focus_refresh_at = time.monotonic() + 2.0
            action = self._policy.step(None, dt=policy_interval)

            while not self._stop.is_set():
                now = time.perf_counter()
                dt = now - last
                last = now
                # Clamp absurd stalls (debugger / focus steal) so one frame
                # does not dump a huge mouse jump.
                dt = min(dt, 4.0 / self._tick_hz)

                mono = time.monotonic()
                if mono >= focus_refresh_at:
                    if self._hwnd or self._title:
                        restore_window_focus(hwnd=self._hwnd, title=self._title)
                    focus_refresh_at = mono + 5.0

                if now >= next_policy_at:
                    pose = self._pose.poll()
                    action = self._policy.step(pose, dt=policy_interval)
                    next_policy_at = now + policy_interval

                try:
                    apply_action(
                        self._injector,
                        action,
                        dt=dt,
                        pixels_per_deg=self._pixels_per_deg,
                    )
                except OSError as exc:
                    logger.warning("自动移动注入失败：%s", exc)
                    break

                elapsed = time.perf_counter() - now
                sleep_s = inject_interval - elapsed
                if sleep_s > 0:
                    self._stop.wait(sleep_s)
        finally:
            set_timer_resolution_1ms(enabled=False)
            try:
                self._injector.release_all()
            except OSError:
                pass
