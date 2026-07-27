"""Inverse-frequency discrete actions constrained to a camera-pose radius."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from game_recorder.auto_move.action_space import (
    STUCK_TRANSLATIONS,
    TRANSLATIONS,
    ActionCatalog,
    DiscreteAction,
    default_action_catalog,
    nearest_inward_translation,
    rotation_rates,
    translation_inward_score,
    translation_keys,
)
from game_recorder.auto_move.policy_wander import WanderAction, WanderPhase
from game_recorder.auto_move.pose_live import UnifiedPose


@dataclass
class BalancedRadiusPolicy:
    """Sample rare human actions inside a fixed horizontal radius around an anchor."""

    radius_m: float = 3.0
    # Start cutting outward walks earlier so small radii do not feel oversized.
    soft_radius_frac: float = 0.5
    freq_alpha: float = 1.0
    hold_min_s: float = 0.35
    hold_max_s: float = 1.0
    look_yaw_deg_s: float = 45.0
    look_pitch_deg_s: float = 18.0
    # When outside radius, blend a stronger yaw toward the anchor.
    return_yaw_deg_s: float = 55.0
    # Estimated walk speed used to cap hold length near the boundary.
    walk_speed_mps: float = 2.0
    stuck_speed_mps: float = 0.15
    stuck_s: float = 1.5
    # Soften commanded look so discrete bins do not jerk the mouse.
    rate_track_hz: float = 8.0
    catalog: ActionCatalog | None = None
    rng: random.Random = field(default_factory=random.Random)

    _catalog: ActionCatalog = field(init=False, repr=False)
    _anchor_x: float | None = field(default=None, init=False)
    _anchor_y: float | None = field(default=None, init=False)
    _current: DiscreteAction | None = field(default=None, init=False)
    _hold_until: float = field(default=0.0, init=False)
    _forced_translation: str | None = field(default=None, init=False)
    _last_pose: UnifiedPose | None = field(default=None, init=False)
    _last_pose_mono: float = field(default=0.0, init=False)
    _stuck_since: float | None = field(default=None, init=False)
    _cmd_yaw_deg_s: float = field(default=0.0, init=False)
    _cmd_pitch_deg_s: float = field(default=0.0, init=False)
    _escape_until: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if self.catalog is not None:
            self._catalog = self.catalog
        else:
            self._catalog = default_action_catalog(alpha=float(self.freq_alpha))

    def reset(self) -> None:
        now = time.monotonic()
        self._anchor_x = None
        self._anchor_y = None
        self._current = None
        self._hold_until = 0.0
        self._forced_translation = None
        self._last_pose = None
        self._last_pose_mono = 0.0
        self._stuck_since = None
        self._cmd_yaw_deg_s = 0.0
        self._cmd_pitch_deg_s = 0.0
        self._escape_until = 0.0
        self._resample(now, pose=None, force_stuck=False)

    def step(
        self,
        pose: UnifiedPose | None,
        *,
        dt: float,
        now: float | None = None,
    ) -> WanderAction:
        clock = time.monotonic() if now is None else float(now)
        dt = max(1e-4, float(dt))

        if pose is not None:
            self._maybe_set_anchor(pose)
            self._observe_pose(pose, clock)

        stuck = (
            self._stuck_since is not None
            and (clock - self._stuck_since) >= self.stuck_s
        )
        if stuck:
            self._escape_until = clock + self.rng.uniform(0.4, 0.9)
            self._stuck_since = None
            self._resample(clock, pose=pose, force_stuck=True)
        elif self._should_interrupt_for_radius(pose) or (
            clock >= self._hold_until or self._current is None
        ):
            # Radius is checked every policy tick (~30Hz), not only at hold
            # boundaries — otherwise a 0.5–1s walk hold can overshoot small radii
            # by 1–2m at GTA walk speed.
            self._resample(
                clock,
                pose=pose,
                force_stuck=clock < self._escape_until,
            )

        assert self._current is not None
        action = self._to_wander_action(self._current, pose=pose)
        return self._finish_action(action, dt)

    def _horizontal_dist_to_anchor(self, pose: UnifiedPose) -> float | None:
        if self._anchor_x is None or self._anchor_y is None:
            return None
        return math.hypot(pose.x - self._anchor_x, pose.y - self._anchor_y)

    def _should_interrupt_for_radius(self, pose: UnifiedPose | None) -> bool:
        """True when the current hold is pushing past the soft/hard radius."""
        if pose is None or self._current is None:
            return False
        dist = self._horizontal_dist_to_anchor(pose)
        if dist is None:
            return False
        radius = max(0.1, float(self.radius_m))
        soft = radius * max(0.1, min(1.0, float(self.soft_radius_frac)))
        tr = self._current.translation

        if dist >= radius:
            # Already outside: interrupt unless we are already commanded inward.
            if tr == "none":
                return True
            score = translation_inward_score(
                tr,
                pos_x=pose.x,
                pos_y=pose.y,
                anchor_x=float(self._anchor_x),
                anchor_y=float(self._anchor_y),
                forward_x=pose.forward_x,
                forward_y=pose.forward_y,
            )
            return score < 0.15

        if dist >= soft and tr != "none":
            score = translation_inward_score(
                tr,
                pos_x=pose.x,
                pos_y=pose.y,
                anchor_x=float(self._anchor_x),
                anchor_y=float(self._anchor_y),
                forward_x=pose.forward_x,
                forward_y=pose.forward_y,
            )
            # Soft zone: cut any clearly outward hold immediately.
            return score < -0.05

        return False

    def _maybe_set_anchor(self, pose: UnifiedPose) -> None:
        if self._anchor_x is None:
            self._anchor_x = float(pose.x)
            self._anchor_y = float(pose.y)

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

    def _sample_hold(self) -> float:
        lo = min(self.hold_min_s, self.hold_max_s)
        hi = max(self.hold_min_s, self.hold_max_s)
        return self.rng.uniform(lo, hi)

    def _resample(
        self,
        clock: float,
        *,
        pose: UnifiedPose | None,
        force_stuck: bool,
    ) -> None:
        allowed_translations = self._allowed_translations(pose, force_stuck=force_stuck)
        candidates = [
            a
            for a in self._catalog.actions
            if a.translation in allowed_translations
        ]
        if not candidates:
            # Fallback: any inward / non-idle translation.
            candidates = [
                a for a in self._catalog.actions if a.translation != "none"
            ]
        if not candidates:
            candidates = list(self._catalog.actions)

        weights = [max(0.0, a.weight) for a in candidates]
        if force_stuck:
            # Boost escape translations further while stuck/escaping.
            weights = [
                w * (3.0 if a.translation in STUCK_TRANSLATIONS else 0.25)
                for a, w in zip(candidates, weights, strict=True)
            ]
        total = sum(weights)
        if total <= 0:
            chosen = self.rng.choice(candidates)
        else:
            pick = self.rng.uniform(0.0, total)
            acc = 0.0
            chosen = candidates[-1]
            for action, w in zip(candidates, weights, strict=True):
                acc += w
                if pick <= acc:
                    chosen = action
                    break

        # Outside hard radius: force translation toward anchor; keep sampled rotation.
        forced = self._forced_translation
        if forced is not None:
            pair = self._catalog.by_pair.get((forced, chosen.rotation))
            if pair is None:
                pair = self._catalog.by_pair.get((forced, "none"))
            if pair is not None:
                chosen = pair

        self._current = chosen
        # Cap hold by remaining distance to the soft boundary so small radii
        # cannot plan a 1s forward walk that tunnels through the circle.
        hold = self._sample_hold()
        if (
            pose is not None
            and self._anchor_x is not None
            and self._anchor_y is not None
        ):
            radius = max(0.1, float(self.radius_m))
            soft = radius * max(0.1, min(1.0, float(self.soft_radius_frac)))
            dist = math.hypot(pose.x - self._anchor_x, pose.y - self._anchor_y)
            speed = max(0.5, float(self.walk_speed_mps))
            if forced is not None or dist >= soft:
                hold = min(hold, 0.18)
            elif chosen.translation != "none":
                remaining = max(0.05, soft - dist)
                hold = min(hold, remaining / speed)
        self._hold_until = clock + hold

    def _allowed_translations(
        self,
        pose: UnifiedPose | None,
        *,
        force_stuck: bool,
    ) -> set[str]:
        self._forced_translation = None
        if pose is None or self._anchor_x is None or self._anchor_y is None:
            if force_stuck:
                return set(STUCK_TRANSLATIONS)
            return set(TRANSLATIONS)

        radius = max(0.1, float(self.radius_m))
        soft = radius * max(0.1, min(1.0, float(self.soft_radius_frac)))
        dist = math.hypot(pose.x - self._anchor_x, pose.y - self._anchor_y)

        if force_stuck and dist < soft:
            return set(STUCK_TRANSLATIONS)

        if dist >= radius:
            forced = nearest_inward_translation(
                pos_x=pose.x,
                pos_y=pose.y,
                anchor_x=self._anchor_x,
                anchor_y=self._anchor_y,
                forward_x=pose.forward_x,
                forward_y=pose.forward_y,
            )
            score = translation_inward_score(
                forced,
                pos_x=pose.x,
                pos_y=pose.y,
                anchor_x=self._anchor_x,
                anchor_y=self._anchor_y,
                forward_x=pose.forward_x,
                forward_y=pose.forward_y,
            )
            # Not facing inward enough: stop feet and yaw toward center first.
            if score < 0.35:
                self._forced_translation = "none"
                return {"none"}
            self._forced_translation = forced
            return {forced}

        if dist >= soft:
            allowed: set[str] = set()
            for name in TRANSLATIONS:
                if name == "none":
                    # Allow brief idle near the soft boundary.
                    allowed.add(name)
                    continue
                score = translation_inward_score(
                    name,
                    pos_x=pose.x,
                    pos_y=pose.y,
                    anchor_x=self._anchor_x,
                    anchor_y=self._anchor_y,
                    forward_x=pose.forward_x,
                    forward_y=pose.forward_y,
                )
                # Soft zone: keep non-outward moves (inward or tangential).
                if score >= -0.05:
                    allowed.add(name)
            if force_stuck:
                allowed &= set(STUCK_TRANSLATIONS) | {"none"}
            if not allowed:
                forced = nearest_inward_translation(
                    pos_x=pose.x,
                    pos_y=pose.y,
                    anchor_x=self._anchor_x,
                    anchor_y=self._anchor_y,
                    forward_x=pose.forward_x,
                    forward_y=pose.forward_y,
                )
                self._forced_translation = forced
                return {forced}
            return allowed

        if force_stuck:
            return set(STUCK_TRANSLATIONS)
        return set(TRANSLATIONS)

    def _to_wander_action(
        self,
        discrete: DiscreteAction,
        *,
        pose: UnifiedPose | None,
    ) -> WanderAction:
        yaw, pitch = rotation_rates(
            discrete.rotation,
            yaw_deg_s=self.look_yaw_deg_s,
            pitch_deg_s=self.look_pitch_deg_s,
        )

        # Outside radius: add a return yaw toward the anchor if look is idle/weak.
        if (
            pose is not None
            and self._anchor_x is not None
            and self._anchor_y is not None
        ):
            radius = max(0.1, float(self.radius_m))
            dist = math.hypot(pose.x - self._anchor_x, pose.y - self._anchor_y)
            if dist >= radius:
                yaw += self._return_yaw_assist(pose)

        phase = WanderPhase.WALK
        if discrete.translation.startswith("backward"):
            phase = WanderPhase.BACKUP
        elif discrete.translation in ("left", "right", "forward_left", "forward_right"):
            phase = WanderPhase.TURN

        return WanderAction(
            keys=translation_keys(discrete.translation),
            yaw_deg_s=yaw,
            pitch_deg_s=pitch,
            phase=phase,
            action_id=discrete.action_id,
            translation=discrete.translation,
            rotation=discrete.rotation,
        )

    def _return_yaw_assist(self, pose: UnifiedPose) -> float:
        """Extra yaw rate to face the anchor when outside the hard radius."""
        assert self._anchor_x is not None and self._anchor_y is not None
        to_ax = self._anchor_x - pose.x
        to_ay = self._anchor_y - pose.y
        if math.hypot(to_ax, to_ay) < 1e-6:
            return 0.0
        # Desired facing = direction to anchor; camera forward horizontal.
        fx, fy = pose.forward_x, pose.forward_y
        fn = math.hypot(fx, fy)
        if fn < 1e-6:
            return 0.0
        fx /= fn
        fy /= fn
        tn = math.hypot(to_ax, to_ay)
        to_ax /= tn
        to_ay /= tn
        # Signed angle from forward to to_anchor (CCW positive in XY).
        cross = fx * to_ay - fy * to_ax
        dot = fx * to_ax + fy * to_ay
        angle = math.atan2(cross, dot)
        # Positive yaw = look right = clockwise in unified XY (X right, Y forward),
        # so yaw sign opposes atan2 CCW.
        if abs(angle) < math.radians(8.0):
            return 0.0
        sign = -1.0 if angle > 0.0 else 1.0
        return sign * abs(self.return_yaw_deg_s)

    def _finish_action(self, action: WanderAction, dt: float) -> WanderAction:
        alpha = 1.0 - math.exp(-max(0.5, self.rate_track_hz) * dt)
        self._cmd_yaw_deg_s += alpha * (action.yaw_deg_s - self._cmd_yaw_deg_s)
        self._cmd_pitch_deg_s += alpha * (action.pitch_deg_s - self._cmd_pitch_deg_s)
        if abs(self._cmd_yaw_deg_s) < 0.05:
            self._cmd_yaw_deg_s = 0.0
        if abs(self._cmd_pitch_deg_s) < 0.05:
            self._cmd_pitch_deg_s = 0.0
        return WanderAction(
            keys=action.keys,
            yaw_deg_s=self._cmd_yaw_deg_s,
            pitch_deg_s=self._cmd_pitch_deg_s,
            phase=action.phase,
            action_id=action.action_id,
            translation=action.translation,
            rotation=action.rotation,
        )
