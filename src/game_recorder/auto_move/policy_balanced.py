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
from game_recorder.auto_move.coverage_maps import CoverageMaps
from game_recorder.auto_move.policy_wander import WanderAction, WanderPhase
from game_recorder.auto_move.pose_live import UnifiedPose


@dataclass
class BalancedRadiusPolicy:
    """Sample rare human actions inside a fixed horizontal radius around an anchor."""

    radius_m: float = 20.0
    # Start cutting outward walks earlier so small radii do not feel oversized.
    soft_radius_frac: float = 0.55
    freq_alpha: float = 1.0
    hold_min_s: float = 2.5
    hold_max_s: float = 4.5
    look_yaw_min_deg_s: float = 15.0
    look_yaw_max_deg_s: float = 30.0
    look_pitch_min_deg_s: float = 6.0
    look_pitch_max_deg_s: float = 15.0
    # Optional fixed-rate overrides retained for callers/tests that need them.
    look_yaw_deg_s: float | None = None
    look_pitch_deg_s: float | None = None
    # When outside radius, blend a stronger yaw toward the anchor.
    return_yaw_deg_s: float = 55.0
    # Cap holds assuming run/sprint so we cannot plan a walk that tunnels out.
    walk_speed_mps: float = 5.0
    # Fallback when ``movement_speed_scales`` has no entry for a slowed source.
    movement_speed_scale: float = 0.1
    movement_speed_scales: dict[str, float] = field(default_factory=dict)
    slowed_sources: tuple[str, ...] = ("gta", "rdr2", "cp2077")
    # Near hard boundary: only clearly-inward moves (dot with to-anchor).
    soft_inward_min: float = 0.15
    # Outside hard radius: interrupt unless returning at least this strongly.
    hard_inward_min: float = 0.35
    # Max hold while in soft zone / recovering outside.
    boundary_hold_s: float = 0.12
    stuck_speed_mps: float = 0.15
    stuck_s: float = 1.5
    # Soften commanded look so discrete bins do not jerk the mouse.
    rate_track_hz: float = 4.0
    # Coverage reweight: w_final = prior × (1+β·move) × (1+γ·look).
    cover_move_beta: float = 1.5
    cover_look_gamma: float = 8.0
    yaw_sector_half_width_deg: float = 22.5
    yaw_dwell_boost_after_s: float = 4.0
    yaw_dwell_boost_per_s: float = 0.35
    yaw_dwell_boost_max: float = 5.0
    yaw_target_turn_boost: float = 8.0
    yaw_target_opposite_weight: float = 0.25
    yaw_target_idle_weight: float = 0.70
    yaw_target_distance_gain: float = 0.50
    pitch_action_base_weight: float = 0.30
    pitch_angle_decay_deg: float = 25.0
    pitch_same_direction_floor: float = 0.15
    pitch_extreme_deg: float = 20.0
    pitch_return_boost_per_s: float = 0.35
    pitch_return_boost_max: float = 4.0
    catalog: ActionCatalog | None = None
    coverage: CoverageMaps | None = None
    rng: random.Random = field(default_factory=random.Random)

    _catalog: ActionCatalog = field(init=False, repr=False)
    _coverage: CoverageMaps = field(init=False, repr=False)
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
    _action_yaw_deg_s: float = field(default=0.0, init=False)
    _action_pitch_deg_s: float = field(default=0.0, init=False)
    _yaw_dwell_center: float | None = field(default=None, init=False)
    _yaw_dwell_since: float = field(default=0.0, init=False)
    _yaw_target_bin: int | None = field(default=None, init=False)
    _pitch_extreme_sign: int = field(default=0, init=False)
    _pitch_extreme_since: float = field(default=0.0, init=False)
    _escape_until: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if self.catalog is not None:
            self._catalog = self.catalog
        else:
            self._catalog = default_action_catalog(alpha=float(self.freq_alpha))
        self._coverage = self.coverage if self.coverage is not None else CoverageMaps()

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
        self._action_yaw_deg_s = 0.0
        self._action_pitch_deg_s = 0.0
        self._yaw_dwell_center = None
        self._yaw_dwell_since = 0.0
        self._yaw_target_bin = None
        self._pitch_extreme_sign = 0
        self._pitch_extreme_since = 0.0
        self._escape_until = 0.0
        self._coverage.reset()
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
            self._coverage.observe(pose, now=clock)
            self._observe_look_dwell(pose, clock)
            self._update_yaw_target(pose)

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
            # boundaries — otherwise a long walk hold can overshoot small radii
            # by 1–2m at run speed.
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

    def _inward_score(self, translation: str, pose: UnifiedPose) -> float:
        return translation_inward_score(
            translation,
            pos_x=pose.x,
            pos_y=pose.y,
            anchor_x=float(self._anchor_x),
            anchor_y=float(self._anchor_y),
            forward_x=pose.forward_x,
            forward_y=pose.forward_y,
        )

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
            # Outside: keep holding only while clearly walking back in.
            if tr == "none":
                return True
            return self._inward_score(tr, pose) < float(self.hard_inward_min)

        if dist >= soft and tr != "none":
            # Soft zone: cut anything that is not clearly inward (incl. tangential).
            return self._inward_score(tr, pose) < float(self.soft_inward_min)

        # Inner zone: cut early when the next couple of policy ticks would
        # cross soft while walking outward (do not use the full remaining hold,
        # or every outward walk would be aborted on the first tick).
        if tr != "none":
            speed = self._estimated_walk_speed(pose)
            horizon = 2.0 / 30.0
            score = self._inward_score(tr, pose)
            radial_out = max(0.0, -score) * speed * horizon
            if dist + radial_out >= soft:
                return True

        return False

    def _maybe_set_anchor(self, pose: UnifiedPose) -> None:
        if self._anchor_x is None:
            self._anchor_x = float(pose.x)
            self._anchor_y = float(pose.y)
            self._coverage.set_anchor(
                anchor_x=self._anchor_x,
                anchor_y=self._anchor_y,
                radius_m=float(self.radius_m),
                ref_forward_x=pose.forward_x,
                ref_forward_y=pose.forward_y,
            )

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

    def _observe_look_dwell(self, pose: UnifiedPose, clock: float) -> None:
        yaw = math.atan2(pose.forward_x, pose.forward_y)
        center = self._yaw_dwell_center
        half_width = math.radians(max(1.0, float(self.yaw_sector_half_width_deg)))
        if center is None:
            self._yaw_dwell_center = yaw
            self._yaw_dwell_since = clock
        else:
            delta = abs(math.atan2(math.sin(yaw - center), math.cos(yaw - center)))
            if delta >= half_width:
                self._yaw_dwell_center = yaw
                self._yaw_dwell_since = clock

        norm = math.sqrt(
            pose.forward_x * pose.forward_x
            + pose.forward_y * pose.forward_y
            + pose.forward_z * pose.forward_z
        )
        pitch_deg = (
            math.degrees(math.asin(max(-1.0, min(1.0, pose.forward_z / norm))))
            if norm > 1e-6
            else 0.0
        )
        threshold = max(1.0, float(self.pitch_extreme_deg))
        sign = 1 if pitch_deg >= threshold else -1 if pitch_deg <= -threshold else 0
        if sign == 0:
            self._pitch_extreme_sign = 0
            self._pitch_extreme_since = 0.0
        elif sign != self._pitch_extreme_sign:
            self._pitch_extreme_sign = sign
            self._pitch_extreme_since = clock

    def _look_behavior_weight(
        self,
        rotation: str,
        *,
        clock: float,
        pose: UnifiedPose | None,
    ) -> float:
        """Soft priors for varied yaw and natural, non-saturating pitch."""
        factor = 1.0
        has_yaw = "yaw_right" in rotation or "yaw_left" in rotation
        if (
            pose is not None
            and self._yaw_target_bin is not None
            and self._coverage.ready
        ):
            desired = self._coverage.yaw_turn_to_bin(
                pose.forward_x, pose.forward_y, self._yaw_target_bin
            )
            if desired is not None:
                if desired in rotation:
                    factor *= max(0.01, float(self.yaw_target_turn_boost))
                elif has_yaw:
                    factor *= max(0.01, float(self.yaw_target_opposite_weight))
                else:
                    factor *= max(0.01, float(self.yaw_target_idle_weight))

        if has_yaw and self._yaw_dwell_since > 0.0:
            overdue = max(
                0.0,
                clock
                - self._yaw_dwell_since
                - max(0.0, float(self.yaw_dwell_boost_after_s)),
            )
            max_extra = max(0.0, float(self.yaw_dwell_boost_max) - 1.0)
            factor *= 1.0 + min(
                max_extra, overdue * max(0.0, float(self.yaw_dwell_boost_per_s))
            )

        pitch_up = "pitch_up" in rotation
        pitch_down = "pitch_down" in rotation
        if not pitch_up and not pitch_down:
            return factor

        factor *= max(0.01, float(self.pitch_action_base_weight))
        pitch_deg = 0.0
        if pose is not None:
            norm = math.sqrt(
                pose.forward_x * pose.forward_x
                + pose.forward_y * pose.forward_y
                + pose.forward_z * pose.forward_z
            )
            if norm > 1e-6:
                pitch_deg = math.degrees(
                    math.asin(max(-1.0, min(1.0, pose.forward_z / norm)))
                )

        same_direction_angle = (
            max(0.0, pitch_deg)
            if pitch_up
            else max(0.0, -pitch_deg)
        )
        if same_direction_angle > 0.0:
            decay = math.exp(
                -same_direction_angle / max(1.0, float(self.pitch_angle_decay_deg))
            )
            factor *= max(float(self.pitch_same_direction_floor), decay)
        else:
            factor *= 1.0 + min(1.5, abs(pitch_deg) / 30.0)

        returning = (
            self._pitch_extreme_sign > 0 and pitch_down
        ) or (
            self._pitch_extreme_sign < 0 and pitch_up
        )
        if returning and self._pitch_extreme_since > 0.0:
            dwell = max(0.0, clock - self._pitch_extreme_since)
            max_extra = max(0.0, float(self.pitch_return_boost_max) - 1.0)
            factor *= 1.0 + min(
                max_extra, dwell * max(0.0, float(self.pitch_return_boost_per_s))
            )
        return max(0.001, factor)

    def _update_yaw_target(self, pose: UnifiedPose) -> None:
        if not self._coverage.ready or self._coverage.n_yaw <= 1:
            return
        current = self._coverage.yaw_bin(pose.forward_x, pose.forward_y)
        if self._yaw_target_bin == current:
            self._yaw_target_bin = None
        if self._yaw_target_bin is not None:
            return

        counts = self._coverage.yaw_visit_counts()
        candidates = [i for i in range(self._coverage.n_yaw) if i != current]
        weights: list[float] = []
        for target in candidates:
            clockwise = (target - current) % self._coverage.n_yaw
            distance = min(clockwise, self._coverage.n_yaw - clockwise)
            novelty = 1.0 / (1.0 + counts[target])
            weights.append(
                novelty
                * (1.0 + max(0.0, float(self.yaw_target_distance_gain)) * distance)
            )
        self._yaw_target_bin = self.rng.choices(candidates, weights=weights, k=1)[0]

    def _sample_look_rates(self) -> None:
        if self.look_yaw_deg_s is None:
            yaw_lo = max(0.0, min(self.look_yaw_min_deg_s, self.look_yaw_max_deg_s))
            yaw_hi = max(0.0, max(self.look_yaw_min_deg_s, self.look_yaw_max_deg_s))
            self._action_yaw_deg_s = self.rng.uniform(yaw_lo, yaw_hi)
        else:
            self._action_yaw_deg_s = max(0.0, abs(float(self.look_yaw_deg_s)))

        if self.look_pitch_deg_s is None:
            pitch_lo = max(
                0.0, min(self.look_pitch_min_deg_s, self.look_pitch_max_deg_s)
            )
            pitch_hi = max(
                0.0, max(self.look_pitch_min_deg_s, self.look_pitch_max_deg_s)
            )
            self._action_pitch_deg_s = self.rng.uniform(pitch_lo, pitch_hi)
        else:
            self._action_pitch_deg_s = max(0.0, abs(float(self.look_pitch_deg_s)))

    def _estimated_walk_speed(self, pose: UnifiedPose | None) -> float:
        scale = 1.0
        if pose is not None and pose.source_key in self.slowed_sources:
            scale = float(
                self.movement_speed_scales.get(
                    pose.source_key, self.movement_speed_scale
                )
            )
            scale = max(0.05, min(1.0, scale))
        return max(0.5, float(self.walk_speed_mps) * scale)

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

        move_nov: dict[str, float] = {}
        look_nov: dict[str, float] = {}
        if (
            pose is not None
            and self._coverage.ready
            and self._forced_translation is None
            and not force_stuck
        ):
            move_nov = self._coverage.novelty_move(
                pos_x=pose.x,
                pos_y=pose.y,
                forward_x=pose.forward_x,
                forward_y=pose.forward_y,
                translations=tuple(sorted(allowed_translations)),
            )
            look_nov = self._coverage.novelty_look(
                forward_x=pose.forward_x,
                forward_y=pose.forward_y,
                forward_z=pose.forward_z,
            )

        weights: list[float] = []
        for a in candidates:
            if move_nov or look_nov:
                w = self._coverage.fuse_weight(
                    prior=a.weight,
                    translation=a.translation,
                    rotation=a.rotation,
                    move_novelty=move_nov or {a.translation: 1.0},
                    look_novelty=look_nov or {a.rotation: 1.0},
                    beta=self.cover_move_beta,
                    gamma=self.cover_look_gamma,
                )
            else:
                w = max(0.0, a.weight)
            w *= self._look_behavior_weight(a.rotation, clock=clock, pose=pose)
            weights.append(w)

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
        self._sample_look_rates()
        # Cap hold by remaining distance so long holds cannot tunnel out,
        # even before soft-zone interrupts fire.
        hold = self._sample_hold()
        if (
            pose is not None
            and self._anchor_x is not None
            and self._anchor_y is not None
        ):
            radius = max(0.1, float(self.radius_m))
            soft = radius * max(0.1, min(1.0, float(self.soft_radius_frac)))
            dist = math.hypot(pose.x - self._anchor_x, pose.y - self._anchor_y)
            speed = self._estimated_walk_speed(pose)
            boundary_hold = max(0.05, float(self.boundary_hold_s))
            if forced is not None:
                hold = min(hold, boundary_hold)
            elif chosen.translation != "none":
                score = self._inward_score(chosen.translation, pose)
                # Outward / tangential: only walk as far as soft allows.
                if score <= 0.0:
                    remaining = max(0.05, soft - dist)
                    hold = min(hold, remaining / speed)
                # Clearly inward movement is safe to hold for the full sampled
                # duration. Radius checks still run at 30 Hz if the camera turns.
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
        soft_min = float(self.soft_inward_min)

        if dist >= radius:
            forced = nearest_inward_translation(
                pos_x=pose.x,
                pos_y=pose.y,
                anchor_x=self._anchor_x,
                anchor_y=self._anchor_y,
                forward_x=pose.forward_x,
                forward_y=pose.forward_y,
            )
            score = self._inward_score(forced, pose)
            # Prefer any inward/strafe recovery; only stop+turn when nothing helps.
            if score < 0.05:
                self._forced_translation = "none"
                return {"none"}
            self._forced_translation = forced
            return {forced}

        if dist >= soft:
            allowed: set[str] = {"none"}
            for name in TRANSLATIONS:
                if name == "none":
                    continue
                if self._inward_score(name, pose) >= soft_min:
                    allowed.add(name)
            if force_stuck:
                # Stuck escape must still respect soft-zone inward gate.
                allowed &= set(STUCK_TRANSLATIONS) | {"none"}
            if allowed == {"none"} or not allowed:
                forced = nearest_inward_translation(
                    pos_x=pose.x,
                    pos_y=pose.y,
                    anchor_x=self._anchor_x,
                    anchor_y=self._anchor_y,
                    forward_x=pose.forward_x,
                    forward_y=pose.forward_y,
                )
                if self._inward_score(forced, pose) >= 0.05:
                    self._forced_translation = forced
                    return {forced}
                self._forced_translation = "none"
                return {"none"}
            return allowed

        if force_stuck:
            # Inside soft: allow stuck escapes, but drop ones that point outward.
            allowed_stuck = {
                name
                for name in STUCK_TRANSLATIONS
                if self._inward_score(name, pose) >= -0.05
            }
            return allowed_stuck or set(STUCK_TRANSLATIONS)
        return set(TRANSLATIONS)

    def _to_wander_action(
        self,
        discrete: DiscreteAction,
        *,
        pose: UnifiedPose | None,
    ) -> WanderAction:
        yaw, pitch = rotation_rates(
            discrete.rotation,
            yaw_deg_s=self._action_yaw_deg_s,
            pitch_deg_s=self._action_pitch_deg_s,
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
