"""Random wander policy: hold W, turn on stuck / on a timer.

Outputs continuous look *rates* (deg/s). The runner integrates them at a high
inject Hz so the game sees a smooth mouse stream instead of coarse jumps.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum

from game_recorder.auto_move.input_inject import VK_A, VK_D, VK_S, VK_W, InputInjector
from game_recorder.auto_move.pose_live import UnifiedPose


class WanderPhase(str, Enum):
    WALK = "walk"
    TURN = "turn"
    BACKUP = "backup"


@dataclass
class WanderAction:
    keys: frozenset[int]
    # Continuous look command (degrees per second). Runner integrates to pixels.
    yaw_deg_s: float = 0.0
    pitch_deg_s: float = 0.0
    phase: WanderPhase = WanderPhase.WALK


@dataclass
class WanderPolicy:
    """Closed-loop wander using horizontal camera displacement as a stuck sensor."""

    stuck_speed_mps: float = 0.15
    stuck_s: float = 1.5
    turn_duration_s: float = 1.25
    backup_duration_s: float = 0.4
    turn_deg_s: float = 55.0
    # Smooth look wander (deg/s).
    look_yaw_max_deg_s: float = 8.0
    look_pitch_max_deg_s: float = 2.0
    look_smooth_hz: float = 1.4
    look_retarget_min_s: float = 1.2
    look_retarget_max_s: float = 3.0
    repath_min_s: float = 4.0
    repath_max_s: float = 10.0
    # Soften turn edges so rate never steps hard.
    turn_ease_s: float = 0.35
    # How fast commanded look rate tracks the policy target (higher = snappier).
    rate_track_hz: float = 6.0
    rng: random.Random = field(default_factory=random.Random)

    _phase: WanderPhase = field(default=WanderPhase.WALK, init=False)
    _phase_until: float = field(default=0.0, init=False)
    _phase_started: float = field(default=0.0, init=False)
    _turn_sign: float = field(default=1.0, init=False)
    _next_repath_at: float = field(default=0.0, init=False)
    _last_pose: UnifiedPose | None = field(default=None, init=False)
    _last_pose_mono: float = field(default=0.0, init=False)
    _stuck_since: float | None = field(default=None, init=False)
    _yaw_rate_deg_s: float = field(default=0.0, init=False)
    _pitch_rate_deg_s: float = field(default=0.0, init=False)
    _yaw_target_deg_s: float = field(default=0.0, init=False)
    _pitch_target_deg_s: float = field(default=0.0, init=False)
    _next_look_retarget_at: float = field(default=0.0, init=False)
    # Smoothed command actually handed to the injector (extra low-pass).
    _cmd_yaw_deg_s: float = field(default=0.0, init=False)
    _cmd_pitch_deg_s: float = field(default=0.0, init=False)

    def reset(self) -> None:
        now = time.monotonic()
        self._phase = WanderPhase.WALK
        self._phase_until = 0.0
        self._phase_started = now
        self._turn_sign = 1.0
        self._next_repath_at = now + self._sample_repath()
        self._last_pose = None
        self._last_pose_mono = 0.0
        self._stuck_since = None
        self._yaw_rate_deg_s = 0.0
        self._pitch_rate_deg_s = 0.0
        self._yaw_target_deg_s = 0.0
        self._pitch_target_deg_s = 0.0
        self._cmd_yaw_deg_s = 0.0
        self._cmd_pitch_deg_s = 0.0
        self._next_look_retarget_at = now
        self._retarget_look(now)

    def step(
        self,
        pose: UnifiedPose | None,
        *,
        dt: float,
        now: float | None = None,
    ) -> WanderAction:
        clock = time.monotonic() if now is None else float(now)
        dt = max(1e-4, float(dt))

        if self._phase in (WanderPhase.TURN, WanderPhase.BACKUP):
            if clock < self._phase_until:
                return self._finish_action(self._action_for_phase(dt, clock), dt)
            self._phase = WanderPhase.WALK
            self._phase_started = clock
            self._stuck_since = None
            self._next_repath_at = clock + self._sample_repath()
            self._yaw_rate_deg_s *= 0.35
            self._pitch_rate_deg_s *= 0.35

        if pose is not None:
            self._observe_pose(pose, clock)

        if self._stuck_since is not None and (clock - self._stuck_since) >= self.stuck_s:
            self._begin_escape(clock)
            return self._finish_action(self._action_for_phase(dt, clock), dt)

        if clock >= self._next_repath_at:
            self._begin_turn(clock, prefer_backup=False)
            return self._finish_action(self._action_for_phase(dt, clock), dt)

        yaw, pitch = self._update_look_rates(dt, clock)
        return self._finish_action(
            WanderAction(
                keys=frozenset({VK_W}),
                yaw_deg_s=yaw,
                pitch_deg_s=pitch,
                phase=WanderPhase.WALK,
            ),
            dt,
        )

    def _finish_action(self, action: WanderAction, dt: float) -> WanderAction:
        """Low-pass the commanded rates so key/phase changes never jerk the mouse."""
        alpha = 1.0 - math.exp(-max(0.5, self.rate_track_hz) * dt)
        self._cmd_yaw_deg_s += alpha * (action.yaw_deg_s - self._cmd_yaw_deg_s)
        self._cmd_pitch_deg_s += alpha * (action.pitch_deg_s - self._cmd_pitch_deg_s)
        # Kill tiny residual chatter.
        if abs(self._cmd_yaw_deg_s) < 0.05:
            self._cmd_yaw_deg_s = 0.0
        if abs(self._cmd_pitch_deg_s) < 0.05:
            self._cmd_pitch_deg_s = 0.0
        return WanderAction(
            keys=action.keys,
            yaw_deg_s=self._cmd_yaw_deg_s,
            pitch_deg_s=self._cmd_pitch_deg_s,
            phase=action.phase,
        )

    def _update_look_rates(self, dt: float, clock: float) -> tuple[float, float]:
        if clock >= self._next_look_retarget_at:
            self._retarget_look(clock)
        alpha = 1.0 - math.exp(-max(0.05, self.look_smooth_hz) * dt)
        self._yaw_rate_deg_s += alpha * (self._yaw_target_deg_s - self._yaw_rate_deg_s)
        self._pitch_rate_deg_s += alpha * (
            self._pitch_target_deg_s - self._pitch_rate_deg_s
        )
        return self._yaw_rate_deg_s, self._pitch_rate_deg_s

    def _retarget_look(self, clock: float) -> None:
        yaw_max = max(0.0, self.look_yaw_max_deg_s)
        pitch_max = max(0.0, self.look_pitch_max_deg_s)
        self._yaw_target_deg_s = self.rng.uniform(-yaw_max, yaw_max)
        self._pitch_target_deg_s = self.rng.uniform(-pitch_max, pitch_max)
        lo = min(self.look_retarget_min_s, self.look_retarget_max_s)
        hi = max(self.look_retarget_min_s, self.look_retarget_max_s)
        self._next_look_retarget_at = clock + self.rng.uniform(lo, hi)

    def _observe_pose(self, pose: UnifiedPose, now: float) -> None:
        prev = self._last_pose
        prev_t = self._last_pose_mono
        self._last_pose = pose
        self._last_pose_mono = now
        if prev is None or prev_t <= 0:
            self._stuck_since = None
            return
        elapsed = max(1e-3, now - prev_t)
        speed = prev.horizontal_distance_to(pose) / elapsed
        if speed < self.stuck_speed_mps:
            if self._stuck_since is None:
                self._stuck_since = now
        else:
            self._stuck_since = None

    def _begin_escape(self, now: float) -> None:
        if self.rng.random() < 0.35:
            self._phase = WanderPhase.BACKUP
            self._phase_started = now
            self._phase_until = now + self.backup_duration_s
        else:
            self._begin_turn(now, prefer_backup=False)

    def _begin_turn(self, now: float, *, prefer_backup: bool) -> None:
        if prefer_backup:
            self._phase = WanderPhase.BACKUP
            self._phase_started = now
            self._phase_until = now + self.backup_duration_s
            return
        self._phase = WanderPhase.TURN
        self._phase_started = now
        self._phase_until = now + self.turn_duration_s
        self._turn_sign = -1.0 if self.rng.random() < 0.5 else 1.0

    def _turn_ease(self, clock: float) -> float:
        ease = max(0.05, float(self.turn_ease_s))
        elapsed = max(0.0, clock - self._phase_started)
        remaining = max(0.0, self._phase_until - clock)
        fade_in = min(1.0, elapsed / ease)
        fade_out = min(1.0, remaining / ease)

        def _smooth(t: float) -> float:
            t = max(0.0, min(1.0, t))
            return t * t * (3.0 - 2.0 * t)

        return _smooth(fade_in) * _smooth(fade_out)

    def _action_for_phase(self, dt: float, clock: float) -> WanderAction:
        if self._phase == WanderPhase.BACKUP:
            return WanderAction(
                keys=frozenset({VK_S}),
                yaw_deg_s=0.0,
                pitch_deg_s=0.0,
                phase=WanderPhase.BACKUP,
            )
        ease = self._turn_ease(clock)
        yaw = self._turn_sign * self.turn_deg_s * ease
        _, pitch = self._update_look_rates(dt, clock)
        side = VK_A if self._turn_sign < 0 else VK_D
        return WanderAction(
            keys=frozenset({VK_W, side}),
            yaw_deg_s=yaw,
            pitch_deg_s=pitch * 0.35,
            phase=WanderPhase.TURN,
        )

    def _sample_repath(self) -> float:
        lo = min(self.repath_min_s, self.repath_max_s)
        hi = max(self.repath_min_s, self.repath_max_s)
        return self.rng.uniform(lo, hi)


def apply_action(
    injector: InputInjector,
    action: WanderAction,
    *,
    dt: float,
    pixels_per_deg: float = 6.0,
) -> None:
    """Apply keys and integrate look rates into a mouse delta for this frame."""
    injector.set_keys(action.keys)
    dx = action.yaw_deg_s * pixels_per_deg * dt
    dy = action.pitch_deg_s * pixels_per_deg * dt
    if dx or dy:
        injector.move_mouse(dx, dy)
