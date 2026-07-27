"""Session-local occupancy maps for under-visited places and look directions.

Used to reweight discrete auto-move actions toward spatial / look novelty while
keeping CSV inverse-frequency priors.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from game_recorder.auto_move.action_space import (
    ROTATIONS,
    TRANSLATIONS,
    world_vel_for_translation,
)
from game_recorder.auto_move.pose_live import UnifiedPose

_PROBE_M = 0.6
_DECAY_INTERVAL_S = 30.0
_DECAY_FACTOR = 0.9
_PITCH_UP_FZ = 0.25
_PITCH_DOWN_FZ = -0.25


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _normalize_scores(raw: dict[str, float]) -> dict[str, float]:
    if not raw:
        return {}
    lo = min(raw.values())
    hi = max(raw.values())
    if hi - lo < 1e-12:
        return {k: 1.0 for k in raw}
    return {k: (v - lo) / (hi - lo) for k, v in raw.items()}


@dataclass
class CoverageMaps:
    """Polar position grid + yaw/pitch look bins around a session anchor."""

    n_rings: int = 3
    n_sectors: int = 8
    n_yaw: int = 8
    n_pitch: int = 3
    probe_m: float = _PROBE_M
    decay_interval_s: float = _DECAY_INTERVAL_S
    decay_factor: float = _DECAY_FACTOR

    _anchor_x: float | None = field(default=None, init=False)
    _anchor_y: float | None = field(default=None, init=False)
    _radius_m: float = field(default=3.0, init=False)
    _pos_counts: list[float] = field(default_factory=list, init=False)
    _look_counts: list[float] = field(default_factory=list, init=False)
    _ref_yaw: float | None = field(default=None, init=False)
    _last_decay_at: float = field(default=0.0, init=False)
    _ready: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._pos_counts = [0.0] * (self.n_rings * self.n_sectors)
        self._look_counts = [0.0] * (self.n_yaw * self.n_pitch)

    @property
    def ready(self) -> bool:
        return self._ready

    def reset(self) -> None:
        self._anchor_x = None
        self._anchor_y = None
        self._radius_m = 3.0
        self._pos_counts = [0.0] * (self.n_rings * self.n_sectors)
        self._look_counts = [0.0] * (self.n_yaw * self.n_pitch)
        self._ref_yaw = None
        self._last_decay_at = 0.0
        self._ready = False

    def set_anchor(
        self,
        *,
        anchor_x: float,
        anchor_y: float,
        radius_m: float,
        ref_forward_x: float = 0.0,
        ref_forward_y: float = 1.0,
    ) -> None:
        self._anchor_x = float(anchor_x)
        self._anchor_y = float(anchor_y)
        self._radius_m = max(0.1, float(radius_m))
        self._ref_yaw = math.atan2(ref_forward_x, ref_forward_y)
        self._pos_counts = [0.0] * (self.n_rings * self.n_sectors)
        self._look_counts = [0.0] * (self.n_yaw * self.n_pitch)
        self._last_decay_at = time.monotonic()
        self._ready = True

    def observe(self, pose: UnifiedPose, *, now: float | None = None) -> None:
        if not self._ready or self._anchor_x is None or self._anchor_y is None:
            return
        clock = time.monotonic() if now is None else float(now)
        self._maybe_decay(clock)

        pos_idx = self._pos_index(pose.x, pose.y)
        if pos_idx is not None:
            self._pos_counts[pos_idx] += 1.0

        look_idx = self._look_index(pose.forward_x, pose.forward_y, pose.forward_z)
        if look_idx is not None:
            self._look_counts[look_idx] += 1.0

    def novelty_move(
        self,
        *,
        pos_x: float,
        pos_y: float,
        forward_x: float,
        forward_y: float,
        translations: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, float]:
        """Per-translation novelty in [0, 1]; higher = less visited destination."""
        names = list(translations) if translations is not None else list(TRANSLATIONS)
        if not self._ready or self._anchor_x is None or self._anchor_y is None:
            return {n: 1.0 for n in names}

        raw: dict[str, float] = {}
        for name in names:
            if name == "none":
                idx = self._pos_index(pos_x, pos_y)
            else:
                wx, wy = world_vel_for_translation(
                    name, forward_x=forward_x, forward_y=forward_y
                )
                px = pos_x + self.probe_m * wx
                py = pos_y + self.probe_m * wy
                px, py = self._clamp_to_radius(px, py)
                idx = self._pos_index(px, py)
            visits = self._pos_counts[idx] if idx is not None else 0.0
            raw[name] = 1.0 / (1.0 + visits)
        return _normalize_scores(raw)

    def novelty_look(
        self,
        *,
        forward_x: float,
        forward_y: float,
        forward_z: float,
        rotations: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, float]:
        """Per-rotation novelty in [0, 1]; higher = less visited target look bin."""
        names = list(rotations) if rotations is not None else list(ROTATIONS)
        if not self._ready:
            return {n: 1.0 for n in names}

        yaw_i, pitch_i = self._yaw_pitch_bins(forward_x, forward_y, forward_z)
        raw: dict[str, float] = {}
        for name in names:
            ty, tp = self._target_look_bins(name, yaw_i, pitch_i)
            idx = ty * self.n_pitch + tp
            visits = self._look_counts[idx]
            raw[name] = 1.0 / (1.0 + visits)
        return _normalize_scores(raw)

    def fuse_weight(
        self,
        *,
        prior: float,
        translation: str,
        rotation: str,
        move_novelty: dict[str, float],
        look_novelty: dict[str, float],
        beta: float,
        gamma: float,
    ) -> float:
        """w_final = prior × (1 + β·novelty_move) × (1 + γ·novelty_look)."""
        nm = _clamp01(move_novelty.get(translation, 1.0))
        nl = _clamp01(look_novelty.get(rotation, 1.0))
        return max(0.0, float(prior)) * (1.0 + float(beta) * nm) * (
            1.0 + float(gamma) * nl
        )

    def _maybe_decay(self, clock: float) -> None:
        if self._last_decay_at <= 0:
            self._last_decay_at = clock
            return
        interval = max(1.0, float(self.decay_interval_s))
        factor = max(0.0, min(1.0, float(self.decay_factor)))
        while clock - self._last_decay_at >= interval:
            self._pos_counts = [c * factor for c in self._pos_counts]
            self._look_counts = [c * factor for c in self._look_counts]
            self._last_decay_at += interval

    def _clamp_to_radius(self, x: float, y: float) -> tuple[float, float]:
        assert self._anchor_x is not None and self._anchor_y is not None
        dx = x - self._anchor_x
        dy = y - self._anchor_y
        dist = math.hypot(dx, dy)
        limit = self._radius_m * 0.98
        if dist <= limit or dist < 1e-9:
            return x, y
        s = limit / dist
        return self._anchor_x + dx * s, self._anchor_y + dy * s

    def _pos_index(self, x: float, y: float) -> int | None:
        if self._anchor_x is None or self._anchor_y is None:
            return None
        dx = x - self._anchor_x
        dy = y - self._anchor_y
        dist = math.hypot(dx, dy)
        # Angle from +Y toward +X (clockwise-friendly sectoring for game forward).
        ang = math.atan2(dx, dy)
        if ang < 0:
            ang += 2.0 * math.pi
        sector = int(ang / (2.0 * math.pi) * self.n_sectors) % self.n_sectors
        ring = int(min(self.n_rings - 1, (dist / self._radius_m) * self.n_rings))
        if dist > self._radius_m * 1.05:
            ring = self.n_rings - 1
        return ring * self.n_sectors + sector

    def _yaw_pitch_bins(
        self, forward_x: float, forward_y: float, forward_z: float
    ) -> tuple[int, int]:
        fx, fy, fz = forward_x, forward_y, forward_z
        hn = math.hypot(fx, fy)
        if hn > 1e-6:
            fx /= hn
            fy /= hn
        else:
            fx, fy = 0.0, 1.0
        yaw = math.atan2(fx, fy)
        ref = self._ref_yaw if self._ref_yaw is not None else 0.0
        rel = (yaw - ref) % (2.0 * math.pi)
        yaw_i = int(rel / (2.0 * math.pi) * self.n_yaw) % self.n_yaw

        if fz >= _PITCH_UP_FZ:
            pitch_i = 0  # up
        elif fz <= _PITCH_DOWN_FZ:
            pitch_i = 2  # down
        else:
            pitch_i = 1  # mid
        return yaw_i, pitch_i

    def _look_index(
        self, forward_x: float, forward_y: float, forward_z: float
    ) -> int | None:
        yaw_i, pitch_i = self._yaw_pitch_bins(forward_x, forward_y, forward_z)
        return yaw_i * self.n_pitch + pitch_i

    def _target_look_bins(
        self, rotation: str, yaw_i: int, pitch_i: int
    ) -> tuple[int, int]:
        ty, tp = yaw_i, pitch_i
        if rotation == "none":
            return ty, tp
        # Positive mouse yaw = look right = increase sector index in our atan2(dx,dy)
        # convention (sectors increase clockwise from +Y toward +X).
        if "yaw_right" in rotation:
            ty = (yaw_i + 1) % self.n_yaw
        elif "yaw_left" in rotation:
            ty = (yaw_i - 1) % self.n_yaw
        if "pitch_up" in rotation:
            tp = max(0, pitch_i - 1)
        elif "pitch_down" in rotation:
            tp = min(self.n_pitch - 1, pitch_i + 1)
        return ty, tp
