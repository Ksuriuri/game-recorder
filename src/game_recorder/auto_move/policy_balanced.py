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
from game_recorder.auto_move.trajectory_patterns import (
    Episode,
    PlannedTurn,
    max_step_scale,
    plan_episode,
    plan_pause,
    prefix_displacements,
)


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
    # Multi-turn trajectory paradigms (WBench nav_cate) planned ahead of the
    # per-action sampler. Free sampling takes over whenever no plan is active.
    paradigms: bool = True
    # Chance of planning an episode at each decision point rather than leaving a
    # gap of free sampling. 0.85 puts ~85% of turns inside a paradigm while the
    # gaps still reach ~57 of the 81 action bins; pushing past ~0.9 collapses
    # coverage to the ~15 bins the WBench vocabulary uses.
    paradigm_episode_chance: float = 0.85
    paradigm_gap_min_actions: int = 1
    paradigm_gap_max_actions: int = 3
    paradigm_turn_hold_s: float = 4.0
    # Below this the plan is replanned look-only instead of being walked out.
    paradigm_min_turn_hold_s: float = 1.2
    paradigm_margin_m: float = 1.0
    # Direction re-rolls when the first draw does not fit the radius. The
    # paradigm, channel and turn count are held fixed, so only the heading
    # changes — WBench constrains the turns relative to each other, not their
    # absolute bearing. Without this the policy degrades to look-only whenever
    # it sits near the boundary, which is most of the time.
    paradigm_plan_attempts: int = 4
    # After a guard aborts an episode, sample freely for this long so the
    # inward bias can walk back in before the next episode is planned.
    paradigm_cooldown_s: float = 3.0
    paradigm_allow_pitch: bool = True
    paradigm_weights: dict[str, float] = field(default_factory=dict)
    # Standing completely still (no keys, no look) for a sampled stretch, rolled
    # at every episode boundary. Labelled ``pause`` in the sidecar.
    pause_chance: float = 0.15
    pause_min_s: float = 5.0
    pause_max_s: float = 15.0
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
    _episode: Episode | None = field(default=None, init=False)
    _episode_turn: int = field(default=0, init=False)
    _current_paradigm: str | None = field(default=None, init=False)
    _current_turn_index: int | None = field(default=None, init=False)
    _paradigm_hold_off_until: float = field(default=0.0, init=False)
    _free_actions_left: int = field(default=0, init=False)

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
        self._abort_episode()
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
        else:
            radius_interrupt = self._should_interrupt_for_radius(pose)
            if radius_interrupt or clock >= self._hold_until or self._current is None:
                # Radius is checked every policy tick (~30Hz), not only at hold
                # boundaries — otherwise a long walk hold can overshoot small
                # radii by 1–2m at run speed. An interrupt cuts the current turn
                # short, so drop the episode rather than advancing its plan once
                # per tick while the boundary condition persists.
                if radius_interrupt:
                    self._abort_episode(cooldown_until=self._cooldown_from(clock))
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
        if self._current is not None and self._current.translation == "none":
            # Standing still on purpose (look-only action or planned turn) — the
            # stuck sensor only means anything while a walk is commanded.
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

        planned = self._next_planned_turn(
            clock,
            pose=pose,
            allowed=allowed_translations,
            force_stuck=force_stuck,
        )
        if planned is not None:
            action = self._catalog.by_pair.get((planned.translation, planned.rotation))
            if action is not None:
                self._current = action
                self._hold_until = clock + max(0.05, planned.hold_s)
                return
            # Trimmed/custom catalog without that pair — fall through to sampling.
            self._abort_episode(cooldown_until=self._cooldown_from(clock))

        self._current_paradigm = None
        self._current_turn_index = None
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

    def _abort_episode(self, *, cooldown_until: float | None = None) -> None:
        had_episode = self._episode is not None
        self._episode = None
        self._episode_turn = 0
        self._current_paradigm = None
        self._current_turn_index = None
        self._free_actions_left = 0
        # Only start the hold-off when a plan was actually dropped. A guard that
        # keeps firing while free sampling recovers must not extend suppression
        # tick after tick, or paradigms never come back at small radii.
        if cooldown_until is not None and had_episode:
            self._paradigm_hold_off_until = cooldown_until

    def _cooldown_from(self, clock: float) -> float:
        return clock + max(0.0, float(self.paradigm_cooldown_s))

    def _next_planned_turn(
        self,
        clock: float,
        *,
        pose: UnifiedPose | None,
        allowed: set[str],
        force_stuck: bool,
    ) -> PlannedTurn | None:
        """Next turn of the active paradigm episode, or ``None`` to sample freely."""
        if not self.paradigms:
            return None
        # Stuck escape and the hard-radius override both need a specific
        # translation, so the rest of the plan would no longer match its label.
        if force_stuck or self._forced_translation is not None:
            self._abort_episode(cooldown_until=self._cooldown_from(clock))
            return None
        if clock < self._paradigm_hold_off_until:
            return None

        if self._episode is None or self._episode_turn >= len(self._episode):
            if self._free_actions_left <= 0 and self._roll_pause():
                self._start_pause()
            else:
                if self._free_actions_left <= 0 and self.rng.random() >= max(
                    0.0, min(1.0, float(self.paradigm_episode_chance))
                ):
                    low = max(1, int(self.paradigm_gap_min_actions))
                    high = max(low, int(self.paradigm_gap_max_actions))
                    self._free_actions_left = self.rng.randint(low, high)
                if self._free_actions_left > 0:
                    self._free_actions_left -= 1
                    return None
                self._start_episode(pose)

        episode = self._episode
        assert episode is not None
        turn = episode.turns[self._episode_turn]
        if turn.translation not in allowed:
            # The radius gate rejects this step; distorting it would produce a
            # trajectory that no longer matches the paradigm label.
            self._abort_episode(cooldown_until=self._cooldown_from(clock))
            return None

        self._episode_turn += 1
        if turn.turn_index == 0:
            # Lock look rates for the whole episode: a roundtrip only closes in
            # yaw if the return leg turns at the same rate as the outbound leg.
            self._sample_look_rates()
        self._current_paradigm = episode.paradigm
        self._current_turn_index = turn.turn_index
        return turn

    def _roll_pause(self) -> bool:
        chance = min(1.0, max(0.0, float(self.pause_chance)))
        return chance > 0.0 and self.rng.random() < chance

    def _start_pause(self) -> None:
        """Queue a stand-still episode; no radius ladder, it never moves."""
        lo = max(0.05, min(float(self.pause_min_s), float(self.pause_max_s)))
        hi = max(lo, float(self.pause_max_s))
        self._episode = plan_pause(self.rng.uniform(lo, hi))
        self._episode_turn = 0

    def _start_episode(self, pose: UnifiedPose | None) -> None:
        hold = max(0.05, float(self.paradigm_turn_hold_s))
        floor = max(0.05, min(hold, float(self.paradigm_min_turn_hold_s)))
        weights = dict(self.paradigm_weights) if self.paradigm_weights else None
        episode = plan_episode(
            rng=self.rng,
            hold_s=hold,
            paradigm_weights=weights,
            allow_pitch=bool(self.paradigm_allow_pitch),
        )
        budget = self._paradigm_hold_budget(episode, pose=pose)

        for _ in range(max(1, int(self.paradigm_plan_attempts)) - 1):
            if budget >= hold:
                break
            candidate = plan_episode(
                rng=self.rng,
                hold_s=hold,
                paradigm=episode.paradigm,
                channel=episode.channel,
                carrier=episode.carrier,
                turn_count=len(episode),
                allow_pitch=bool(self.paradigm_allow_pitch),
            )
            candidate_budget = self._paradigm_hold_budget(candidate, pose=pose)
            if candidate_budget > budget:
                episode = candidate
                budget = candidate_budget

        # A four-turn walk at run speed does not fit a 20m radius. Shorten the
        # plan before giving up on translation: a two-turn roundtrip, zigzag or
        # L-shape is still that paradigm, while a two-turn loop is not a loop.
        if budget < floor and len(episode) > 2 and episode.paradigm != "loop":
            episode = plan_episode(
                rng=self.rng,
                hold_s=hold,
                paradigm=episode.paradigm,
                channel=episode.channel,
                carrier=episode.carrier,
                turn_count=2,
                allow_pitch=bool(self.paradigm_allow_pitch),
            )
            budget = self._paradigm_hold_budget(episode, pose=pose)

        if budget < floor:
            # Still no room to walk it out. Replan look-only: standing still and
            # turning keeps the paradigm intact and ignores the radius entirely.
            episode = plan_episode(
                rng=self.rng,
                hold_s=hold,
                paradigm=episode.paradigm,
                channel="rotation",
                carrier="none",
                turn_count=len(episode),
                allow_pitch=bool(self.paradigm_allow_pitch),
            )
            budget = self._paradigm_hold_budget(episode, pose=pose)

        if budget < hold:
            episode = episode.with_hold(max(floor, budget))
        self._episode = episode
        self._episode_turn = 0

    def _paradigm_hold_budget(
        self,
        episode: Episode,
        *,
        pose: UnifiedPose | None,
    ) -> float:
        """Longest per-turn hold that keeps *episode* inside the soft radius."""
        hold = max(0.05, float(self.paradigm_turn_hold_s))
        peak = episode.peak_outbound_units
        if peak <= 1e-9:
            return hold
        if pose is None or self._anchor_x is None or self._anchor_y is None:
            return hold
        radius = max(0.1, float(self.radius_m))
        soft = radius * max(0.1, min(1.0, float(self.soft_radius_frac)))
        # Cap the margin at a fraction of soft: a flat 1m would eat most of the
        # usable room at a 5m radius.
        margin = min(max(0.0, float(self.paradigm_margin_m)), soft * 0.2)
        offset_x = pose.x - self._anchor_x
        offset_y = pose.y - self._anchor_y
        dist = math.hypot(offset_x, offset_y)
        # The margin is a buffer, not a veto. Standing inside it (the policy
        # spends much of its time hovering near soft) must still allow plans
        # that hold or reduce the distance, or every episode degrades.
        limit = max(soft - margin, dist)
        speed = self._estimated_walk_speed(pose)

        if episode.heading_frozen:
            # The plan never turns the camera, so its waypoints are exact: walk
            # each one against the limit instead of assuming the whole plan
            # heads straight out. Near the boundary this is the difference
            # between a real trajectory and being forced to stand still.
            travel = max_step_scale(
                prefix_displacements(
                    episode.turns,
                    forward_x=pose.forward_x,
                    forward_y=pose.forward_y,
                ),
                offset_x=offset_x,
                offset_y=offset_y,
                limit=limit,
            )
            return min(hold, travel / speed)

        # A rotating plan curves unpredictably — keep the radial worst case.
        budget = limit - dist
        if budget <= 0.0:
            return 0.0
        return min(hold, budget / (peak * speed))

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
            paradigm=self._current_paradigm,
            turn_index=self._current_turn_index,
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
            paradigm=action.paradigm,
            turn_index=action.turn_index,
        )
