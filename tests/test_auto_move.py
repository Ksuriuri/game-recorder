"""Unit tests for auto-move pose normalization, wander policy, and config wiring."""

from __future__ import annotations

import json
import math
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_recorder.auto_move.action_space import (
    ActionCatalog,
    DiscreteAction,
    TRANSLATIONS,
    load_action_catalog,
    nearest_inward_translation,
    rotation_rates,
    translation_inward_score,
    translation_keys,
)
from game_recorder.auto_move.coverage_maps import CoverageMaps
from game_recorder.auto_move.input_inject import VK_A, VK_D, VK_S, VK_W, InputInjector
from game_recorder.auto_move.policy_balanced import BalancedRadiusPolicy
from game_recorder.auto_move.policy_wander import WanderPhase, WanderPolicy, apply_action
from game_recorder.auto_move.pose_live import (
    LivePoseReader,
    UnifiedPose,
    candidate_raw_paths,
    extract_unified_pose,
)
from game_recorder.camera_sync import GTA_CAMERA_SOURCE, WUKONG_CAMERA_SOURCE
from game_recorder.config import Config


class PoseNormalizeTests(unittest.TestCase):
    def test_gta_row_vector_translation(self) -> None:
        header = {
            "world_axes": "x_right_y_forward_z_up",
            "matrix_vector_convention": "row_vector",
        }
        # Identity rotation, translation (10, 20, 30) in last row.
        matrix = [
            1, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 1, 0,
            10, 20, 30, 1,
        ]
        pose = extract_unified_pose(
            {"type": "sample", "t_unix_ms": 1000, "camera_to_world": matrix},
            header,
            source_key="gta",
        )
        assert pose is not None
        self.assertEqual((pose.x, pose.y, pose.z), (10.0, 20.0, 30.0))

    def test_wukong_ue_axes(self) -> None:
        header = {
            "world_axes": "x_forward_y_right_z_up",
            "matrix_vector_convention": "row_vector",
        }
        matrix = [
            1, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 1, 0,
            5, 7, 9, 1,  # native (forward, right, up)
        ]
        pose = extract_unified_pose(
            {"type": "sample", "t_unix_ms": 1, "camera_to_world": matrix},
            header,
            source_key="wukong",
        )
        assert pose is not None
        # unified (right, forward, up) = (7, 5, 9)
        self.assertEqual((pose.x, pose.y, pose.z), (7.0, 5.0, 9.0))

    def test_cp2077_explicit_position_and_column_matrix(self) -> None:
        header = {
            "world_axes": "x_game_y_game_z_up",
            "matrix_vector_convention": "column_vector",
        }
        pose = extract_unified_pose(
            {
                "type": "sample",
                "t_unix_ms": 42,
                "camera_position_world": [1.5, 2.5, 3.5],
            },
            header,
            source_key="cp2077",
        )
        assert pose is not None
        self.assertEqual((pose.x, pose.y, pose.z), (1.5, 2.5, 3.5))

        matrix = [
            1, 0, 0, 11,
            0, 1, 0, 22,
            0, 0, 1, 33,
            0, 0, 0, 1,
        ]
        pose2 = extract_unified_pose(
            {"type": "sample", "t_unix_ms": 43, "camera_to_world": matrix},
            header,
            source_key="cp2077",
        )
        assert pose2 is not None
        self.assertEqual((pose2.x, pose2.y, pose2.z), (11.0, 22.0, 33.0))


class LivePoseReaderTests(unittest.TestCase):
    def test_tails_jsonl_and_updates_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "recordings"
            session_dir = output_dir / "session_test"
            session_dir.mkdir(parents=True)
            raw = session_dir / GTA_CAMERA_SOURCE.raw_filename
            header = {
                "type": "header",
                "schema": "gta_camera_v2",
                "world_axes": "x_right_y_forward_z_up",
                "matrix_vector_convention": "row_vector",
            }
            sample = {
                "type": "sample",
                "t_unix_ms": 1000,
                "camera_to_world": [
                    1, 0, 0, 0,
                    0, 1, 0, 0,
                    0, 0, 1, 0,
                    1, 2, 3, 1,
                ],
            }
            with raw.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(header) + "\n")
                stream.write(json.dumps(sample) + "\n")

            reader = LivePoseReader(
                output_dir=output_dir,
                session_dir=session_dir,
                sources=(GTA_CAMERA_SOURCE,),
            )
            pose = reader.poll()
            assert pose is not None
            self.assertEqual((pose.x, pose.y, pose.z), (1.0, 2.0, 3.0))

            sample2 = dict(sample)
            sample2["t_unix_ms"] = 1100
            sample2["camera_to_world"] = [
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                4, 5, 6, 1,
            ]
            with raw.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(sample2) + "\n")
            pose2 = reader.poll()
            assert pose2 is not None
            self.assertEqual((pose2.x, pose2.y, pose2.z), (4.0, 5.0, 6.0))

    def test_candidate_paths_include_session_file(self) -> None:
        paths = candidate_raw_paths(
            output_dir=Path("recordings"),
            session_dir=Path("recordings/session_x"),
            source=WUKONG_CAMERA_SOURCE,
        )
        self.assertTrue(any(p.name == "camera_raw_wukong.jsonl" for p in paths))


class ActionSpaceTests(unittest.TestCase):
    def test_catalog_has_81_bins_and_inverse_weights(self) -> None:
        catalog = load_action_catalog(alpha=1.0)
        self.assertEqual(len(catalog), 81)
        forward_none = catalog.by_pair[("forward", "none")]
        rare = catalog.by_pair[("backward_right", "pitch_down")]
        self.assertLess(forward_none.weight, rare.weight)
        self.assertGreater(forward_none.dense_pct, rare.dense_pct)

    def test_translation_and_rotation_mapping(self) -> None:
        self.assertEqual(translation_keys("none"), frozenset())
        self.assertEqual(translation_keys("forward"), frozenset({VK_W}))
        self.assertEqual(translation_keys("forward_left"), frozenset({VK_W, VK_A}))
        self.assertEqual(translation_keys("backward_right"), frozenset({VK_S, VK_D}))
        self.assertEqual(rotation_rates("none"), (0.0, 0.0))
        self.assertEqual(rotation_rates("yaw_left", yaw_deg_s=40.0), (-40.0, 0.0))
        self.assertEqual(rotation_rates("pitch_up", pitch_deg_s=12.0), (0.0, -12.0))
        self.assertEqual(
            rotation_rates("yaw_right_pitch_down", yaw_deg_s=40.0, pitch_deg_s=12.0),
            (40.0, 12.0),
        )

    def test_inward_score_prefers_back_when_past_anchor(self) -> None:
        # Facing +Y; standing at y=5 with anchor at origin → need backward.
        score_fwd = translation_inward_score(
            "forward",
            pos_x=0.0,
            pos_y=5.0,
            anchor_x=0.0,
            anchor_y=0.0,
            forward_x=0.0,
            forward_y=1.0,
        )
        score_back = translation_inward_score(
            "backward",
            pos_x=0.0,
            pos_y=5.0,
            anchor_x=0.0,
            anchor_y=0.0,
            forward_x=0.0,
            forward_y=1.0,
        )
        self.assertLess(score_fwd, 0.0)
        self.assertGreater(score_back, 0.0)
        nearest = nearest_inward_translation(
            pos_x=0.0,
            pos_y=5.0,
            anchor_x=0.0,
            anchor_y=0.0,
            forward_x=0.0,
            forward_y=1.0,
        )
        self.assertEqual(nearest, "backward")


class CoverageMapsTests(unittest.TestCase):
    def test_ring_count_tracks_active_radius(self) -> None:
        maps = CoverageMaps()
        maps.set_anchor(anchor_x=0.0, anchor_y=0.0, radius_m=10.0)
        self.assertEqual(maps.n_rings, 10)
        self.assertEqual(len(maps._pos_counts), 10 * maps.n_sectors)

        maps.set_anchor(anchor_x=0.0, anchor_y=0.0, radius_m=2.2)
        self.assertEqual(maps.n_rings, 3)
        self.assertEqual(len(maps._pos_counts), 3 * maps.n_sectors)

    def test_pitch_uses_six_angular_bins(self) -> None:
        maps = CoverageMaps()
        bins = []
        for degrees in (75, 45, 15, -15, -45, -75):
            pitch = math.radians(degrees)
            _, pitch_i = maps._yaw_pitch_bins(
                0.0, math.cos(pitch), math.sin(pitch)
            )
            bins.append(pitch_i)
        self.assertEqual(bins, [0, 1, 2, 3, 4, 5])

    def test_visited_sector_lowers_move_novelty(self) -> None:
        maps = CoverageMaps()
        maps.set_anchor(
            anchor_x=0.0,
            anchor_y=0.0,
            radius_m=3.0,
            ref_forward_x=0.0,
            ref_forward_y=1.0,
        )
        # Sit and mark the forward (+Y) cell many times.
        pose = UnifiedPose(
            0, 0.0, 0.8, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        for i in range(20):
            maps.observe(pose, now=float(i))

        nov = maps.novelty_move(
            pos_x=0.0,
            pos_y=0.0,
            forward_x=0.0,
            forward_y=1.0,
        )
        self.assertLess(nov["forward"], nov["backward"])
        self.assertLess(nov["forward"], nov["left"])

    def test_visited_yaw_lowers_same_direction_look_novelty(self) -> None:
        maps = CoverageMaps()
        maps.set_anchor(
            anchor_x=0.0,
            anchor_y=0.0,
            radius_m=3.0,
            ref_forward_x=0.0,
            ref_forward_y=1.0,
        )
        # Looking +Y (ref). Mark current look bin heavily.
        pose = UnifiedPose(
            0, 0.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        for i in range(20):
            maps.observe(pose, now=float(i))

        nov = maps.novelty_look(
            forward_x=0.0,
            forward_y=1.0,
            forward_z=0.0,
        )
        # Either half-turn leaves the heavily visited current direction.
        self.assertLess(nov["none"], nov["yaw_right"])
        self.assertLess(nov["none"], nov["yaw_left"])

    def test_look_novelty_prefers_undercovered_half_turn(self) -> None:
        maps = CoverageMaps()
        maps.set_anchor(
            anchor_x=0.0,
            anchor_y=0.0,
            radius_m=10.0,
            ref_forward_x=0.0,
            ref_forward_y=1.0,
        )
        _, pitch_i = maps._yaw_pitch_bins(0.0, 1.0, 0.0)
        # The left half is already covered; the right half is untouched.
        for yaw_i in (7, 6, 5):
            maps._look_counts[yaw_i * maps.n_pitch + pitch_i] = 100.0

        nov = maps.novelty_look(
            forward_x=0.0,
            forward_y=1.0,
            forward_z=0.0,
        )

        self.assertGreater(nov["yaw_right"], nov["yaw_left"])

    def test_fuse_keeps_rare_prior_advantage(self) -> None:
        maps = CoverageMaps()
        catalog = load_action_catalog(alpha=1.0)
        common = catalog.by_pair[("forward", "none")]
        rare = catalog.by_pair[("backward_right", "pitch_down")]
        move = {t: 1.0 for t in TRANSLATIONS}
        look = {r: 1.0 for r in (
            "none",
            "yaw_right",
            "yaw_left",
            "pitch_up",
            "pitch_down",
            "yaw_right_pitch_up",
            "yaw_right_pitch_down",
            "yaw_left_pitch_up",
            "yaw_left_pitch_down",
        )}
        w_common = maps.fuse_weight(
            prior=common.weight,
            translation=common.translation,
            rotation=common.rotation,
            move_novelty=move,
            look_novelty=look,
            beta=1.5,
            gamma=1.5,
        )
        w_rare = maps.fuse_weight(
            prior=rare.weight,
            translation=rare.translation,
            rotation=rare.rotation,
            move_novelty=move,
            look_novelty=look,
            beta=1.5,
            gamma=1.5,
        )
        self.assertGreater(w_rare, w_common)


class BalancedRadiusPolicyTests(unittest.TestCase):
    def test_yaw_target_bias_is_persistent_but_not_forced(self) -> None:
        policy = BalancedRadiusPolicy()
        policy._coverage.set_anchor(
            anchor_x=0.0,
            anchor_y=0.0,
            radius_m=10.0,
            ref_forward_x=0.0,
            ref_forward_y=1.0,
        )
        policy._yaw_target_bin = 3
        facing_reference = UnifiedPose(
            0, 0.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )

        toward = policy._look_behavior_weight(
            "yaw_right", clock=1.0, pose=facing_reference
        )
        opposite = policy._look_behavior_weight(
            "yaw_left", clock=1.0, pose=facing_reference
        )
        idle = policy._look_behavior_weight(
            "none", clock=1.0, pose=facing_reference
        )

        self.assertGreater(toward, idle)
        self.assertGreater(idle, opposite)
        self.assertGreater(opposite, 0.0)

    def test_long_yaw_dwell_smoothly_boosts_turn_weight(self) -> None:
        policy = BalancedRadiusPolicy(
            yaw_dwell_boost_after_s=4.0,
            yaw_dwell_boost_per_s=0.35,
        )
        policy._yaw_dwell_since = 1.0

        early = policy._look_behavior_weight(
            "yaw_right", clock=2.0, pose=None
        )
        late = policy._look_behavior_weight(
            "yaw_right", clock=12.0, pose=None
        )

        self.assertEqual(early, 1.0)
        self.assertGreater(late, early)
        self.assertLessEqual(late, policy.yaw_dwell_boost_max)

    def test_pitch_weights_are_soft_and_return_toward_level(self) -> None:
        policy = BalancedRadiusPolicy()
        level = UnifiedPose(
            0, 0.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        level_pitch = policy._look_behavior_weight(
            "pitch_up", clock=1.0, pose=level
        )
        horizontal = policy._look_behavior_weight("none", clock=1.0, pose=level)
        self.assertGreater(level_pitch, 0.0)
        self.assertLess(level_pitch, horizontal)

        upward = UnifiedPose(
            0,
            0.0,
            0.0,
            0.0,
            "gta",
            forward_x=0.0,
            forward_y=math.sqrt(0.75),
            forward_z=0.5,
        )
        policy._observe_look_dwell(upward, 1.0)
        policy._observe_look_dwell(upward, 10.0)
        keep_up = policy._look_behavior_weight(
            "pitch_up", clock=10.0, pose=upward
        )
        return_down = policy._look_behavior_weight(
            "pitch_down", clock=10.0, pose=upward
        )
        self.assertGreater(keep_up, 0.0)
        self.assertGreater(return_down, keep_up)

    def test_look_speed_is_sampled_once_per_action(self) -> None:
        turn = DiscreteAction(0, "none", "yaw_right_pitch_down", 1.0, 1.0)
        catalog = ActionCatalog(
            actions=(turn,),
            by_id={0: turn},
            by_pair={("none", "yaw_right_pitch_down"): turn},
            weights=(1.0,),
        )
        policy = BalancedRadiusPolicy(catalog=catalog, rng=random.Random(7))

        policy._resample(1.0, pose=None, force_stuck=False)
        first = (policy._action_yaw_deg_s, policy._action_pitch_deg_s)
        self.assertGreaterEqual(first[0], 15.0)
        self.assertLessEqual(first[0], 30.0)
        self.assertGreaterEqual(first[1], 6.0)
        self.assertLessEqual(first[1], 15.0)

        policy._resample(2.0, pose=None, force_stuck=False)
        second = (policy._action_yaw_deg_s, policy._action_pitch_deg_s)
        self.assertNotEqual(first, second)

    def test_speed_estimate_only_slows_supported_games(self) -> None:
        policy = BalancedRadiusPolicy(walk_speed_mps=5.0, movement_speed_scale=0.5)
        gta = UnifiedPose(
            0, 0.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        wukong = UnifiedPose(
            0, 0.0, 0.0, 0.0, "wukong", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        self.assertEqual(policy._estimated_walk_speed(gta), 2.5)
        self.assertEqual(policy._estimated_walk_speed(wukong), 5.0)

    def test_step_without_pose_does_not_crash(self) -> None:
        policy = BalancedRadiusPolicy(
            hold_min_s=0.2,
            hold_max_s=0.2,
            rate_track_hz=100.0,
            rng=random.Random(0),
        )
        policy.reset()
        action = policy.step(None, dt=1.0 / 30.0, now=1.0)
        self.assertIsNotNone(action.action_id)
        self.assertIn(action.translation, TRANSLATIONS)

    def test_outside_radius_forces_inward_translation(self) -> None:
        policy = BalancedRadiusPolicy(
            radius_m=3.0,
            soft_radius_frac=0.5,
            hold_min_s=0.01,
            hold_max_s=0.01,
            rate_track_hz=100.0,
            look_yaw_deg_s=0.0,
            look_pitch_deg_s=0.0,
            return_yaw_deg_s=0.0,
            rng=random.Random(1),
        )
        policy.reset()
        # Lock anchor at origin facing +Y.
        anchor = UnifiedPose(
            0, 0.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        policy.step(anchor, dt=0.05, now=1.0)

        outside = UnifiedPose(
            100, 0.0, 4.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        # Force resample with pose outside hard radius.
        policy._hold_until = 0.0
        action = policy.step(outside, dt=0.05, now=2.0)
        self.assertEqual(action.translation, "backward")
        self.assertEqual(action.keys, frozenset({VK_S}))

        # Soft zone: outward translations should be filtered from allowed set.
        soft = UnifiedPose(
            200, 0.0, 2.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        allowed = policy._allowed_translations(soft, force_stuck=False)
        self.assertNotIn("forward", allowed)
        self.assertIn("backward", allowed)

    def test_mid_hold_outward_walk_interrupted_at_soft_radius(self) -> None:
        """Regression: small radii must not wait for hold expiry to turn back."""
        catalog = load_action_catalog(alpha=1.0)
        forward_none = catalog.by_pair[("forward", "none")]
        policy = BalancedRadiusPolicy(
            radius_m=0.5,
            soft_radius_frac=0.5,
            hold_min_s=5.0,
            hold_max_s=5.0,
            rate_track_hz=100.0,
            look_yaw_deg_s=0.0,
            look_pitch_deg_s=0.0,
            return_yaw_deg_s=0.0,
            catalog=catalog,
            rng=random.Random(0),
        )
        policy.reset()
        anchor = UnifiedPose(
            0, 0.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        policy.step(anchor, dt=0.05, now=1.0)
        # Simulate a long outward hold that would previously overshoot.
        policy._current = forward_none
        policy._hold_until = 1e9
        past_soft = UnifiedPose(
            50, 0.0, 0.4, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        action = policy.step(past_soft, dt=0.05, now=2.0)
        self.assertNotEqual(action.translation, "forward")
        self.assertIn(action.translation, ("backward", "none", "left", "right",
                                           "backward_left", "backward_right",
                                           "forward_left", "forward_right"))
        # Soft zone now requires clearly-inward motion (not tangential).
        score = translation_inward_score(
            action.translation or "none",
            pos_x=0.0,
            pos_y=0.4,
            anchor_x=0.0,
            anchor_y=0.0,
            forward_x=0.0,
            forward_y=1.0,
        )
        if action.translation != "none":
            self.assertGreaterEqual(score, policy.soft_inward_min)

    def test_soft_zone_rejects_tangential_drift(self) -> None:
        """Tangential strafe near soft must not keep holding past the boundary."""
        catalog = load_action_catalog(alpha=1.0)
        left_none = catalog.by_pair[("left", "none")]
        policy = BalancedRadiusPolicy(
            radius_m=3.0,
            soft_radius_frac=0.5,
            soft_inward_min=0.15,
            hold_min_s=5.0,
            hold_max_s=5.0,
            rate_track_hz=100.0,
            look_yaw_deg_s=0.0,
            look_pitch_deg_s=0.0,
            return_yaw_deg_s=0.0,
            catalog=catalog,
            rng=random.Random(0),
        )
        policy.reset()
        anchor = UnifiedPose(
            0, 0.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        policy.step(anchor, dt=0.05, now=1.0)
        policy._current = left_none
        policy._hold_until = 1e9
        soft = UnifiedPose(
            50, 0.0, 2.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        action = policy.step(soft, dt=0.05, now=2.0)
        self.assertNotEqual(action.translation, "left")
        allowed = policy._allowed_translations(soft, force_stuck=False)
        self.assertNotIn("left", allowed)
        self.assertNotIn("right", allowed)
        self.assertNotIn("forward", allowed)
        self.assertIn("backward", allowed)

    def test_soft_zone_inward_action_keeps_full_hold(self) -> None:
        """Safe inward movement must not be resampled every boundary tick."""
        backward = DiscreteAction(0, "backward", "none", 1.0, 1.0)
        catalog = ActionCatalog(
            actions=(backward,),
            by_id={0: backward},
            by_pair={("backward", "none"): backward},
            weights=(1.0,),
        )
        policy = BalancedRadiusPolicy(
            radius_m=3.0,
            soft_radius_frac=0.5,
            hold_min_s=3.0,
            hold_max_s=3.0,
            catalog=catalog,
            rng=random.Random(0),
        )
        policy._anchor_x = 0.0
        policy._anchor_y = 0.0
        soft = UnifiedPose(
            50, 0.0, 2.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )

        policy._resample(10.0, pose=soft, force_stuck=False)

        self.assertEqual(policy._current, backward)
        self.assertAlmostEqual(policy._hold_until, 13.0)

    def test_outside_prefers_strafe_recovery_over_idle(self) -> None:
        """When outside, take a weakly-inward strafe instead of freezing."""
        policy = BalancedRadiusPolicy(
            radius_m=3.0,
            soft_radius_frac=0.5,
            hold_min_s=0.01,
            hold_max_s=0.01,
            rate_track_hz=100.0,
            look_yaw_deg_s=0.0,
            look_pitch_deg_s=0.0,
            return_yaw_deg_s=0.0,
            rng=random.Random(2),
        )
        policy.reset()
        anchor = UnifiedPose(
            0, 0.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        policy.step(anchor, dt=0.05, now=1.0)
        # Outside on +X while facing +Y → left (-X? wait: right is +X in cam).
        # Facing +Y: right=+X, left=-X. Outside at +X=4 → need left (toward origin).
        outside = UnifiedPose(
            100, 4.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        policy._hold_until = 0.0
        action = policy.step(outside, dt=0.05, now=2.0)
        self.assertEqual(action.translation, "left")
        self.assertIn(VK_A, action.keys)


class WanderPolicyTests(unittest.TestCase):
    def test_stuck_triggers_turn_or_backup(self) -> None:
        policy = WanderPolicy(
            stuck_speed_mps=0.5,
            stuck_s=0.2,
            turn_duration_s=0.5,
            backup_duration_s=0.5,
            repath_min_s=100.0,
            repath_max_s=100.0,
            look_yaw_max_deg_s=0.0,
            look_pitch_max_deg_s=0.0,
            rate_track_hz=100.0,
        )
        policy.reset()
        # Freeze repath clock so only stuck logic fires.
        policy._next_repath_at = 1e9
        p0 = UnifiedPose(0, 0.0, 0.0, 0.0, "gta")
        action = policy.step(p0, dt=0.05, now=1.0)
        self.assertEqual(action.phase, WanderPhase.WALK)
        self.assertIn(VK_W, action.keys)

        # Nearly stationary for > stuck_s while "holding W".
        p1 = UnifiedPose(100, 0.01, 0.0, 0.0, "gta")
        policy.step(p1, dt=0.05, now=1.1)
        p2 = UnifiedPose(200, 0.02, 0.0, 0.0, "gta")
        action2 = policy.step(p2, dt=0.05, now=1.35)
        self.assertIn(action2.phase, (WanderPhase.TURN, WanderPhase.BACKUP))

    def test_apply_action_integrates_rates(self) -> None:
        injector = InputInjector()
        with mock.patch.object(injector, "set_keys") as set_keys, mock.patch.object(
            injector, "move_mouse"
        ) as move_mouse:
            from game_recorder.auto_move.policy_wander import WanderAction

            apply_action(
                injector,
                WanderAction(
                    keys=frozenset({VK_W, VK_S}),
                    yaw_deg_s=10.0,
                    pitch_deg_s=-5.0,
                ),
                dt=0.1,
                pixels_per_deg=6.0,
            )
            set_keys.assert_called_once()
            move_mouse.assert_called_once_with(6.0, -3.0)


class ConfigAutoMoveTests(unittest.TestCase):
    def test_auto_move_defaults_on_and_disables_idle_and_violent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                output_dir=Path(tmp) / "out",
                idle_timeout_s=10.0,
                violent_duration_s=1.0,
            )
            self.assertTrue(cfg.auto_move)
            self.assertEqual(cfg.auto_move_policy, "balanced")
            self.assertEqual(cfg.auto_move_radius_m, 10.0)
            self.assertEqual(cfg.auto_move_action_hold_min_s, 2.5)
            self.assertEqual(cfg.auto_move_action_hold_max_s, 4.5)
            self.assertEqual(cfg.auto_move_speed_scale, 0.1)
            self.assertEqual(cfg.auto_move_look_yaw_min_deg_s, 15.0)
            self.assertEqual(cfg.auto_move_look_yaw_max_deg_s, 30.0)
            self.assertEqual(cfg.auto_move_look_pitch_min_deg_s, 6.0)
            self.assertEqual(cfg.auto_move_look_pitch_max_deg_s, 15.0)
            self.assertEqual(cfg.auto_move_cover_move_beta, 1.5)
            self.assertEqual(cfg.auto_move_cover_look_gamma, 8.0)
            self.assertEqual(cfg.idle_timeout_s, 0.0)
            self.assertEqual(cfg.violent_duration_s, 0.0)

    def test_begin_auto_move_is_noop_when_disabled(self) -> None:
        from game_recorder.session import Session

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(output_dir=Path(tmp) / "out", auto_move=False)
            session = Session(cfg)
            session.begin_auto_move()
            self.assertIsNone(session._auto_move)


class SendInputSmokeTests(unittest.TestCase):
    """Lightweight OS smoke: SendInput must accept key/mouse batches on Windows."""

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_sendinput_key_and_mouse_roundtrip(self) -> None:
        injector = InputInjector()
        injector.set_keys(frozenset({VK_W}))
        self.assertEqual(injector.held_keys, frozenset({VK_W}))
        injector.move_mouse(1, 0)
        injector.release_all()
        self.assertEqual(injector.held_keys, frozenset())

    def test_mouse_subpixel_accumulates(self) -> None:
        injector = InputInjector()
        with mock.patch(
            "game_recorder.auto_move.input_inject._send_inputs"
        ) as send:
            injector.move_mouse(0.4, 0.0)
            send.assert_not_called()
            injector.move_mouse(0.4, 0.0)
            send.assert_not_called()
            injector.move_mouse(0.4, 0.0)
            send.assert_called_once()
            args = send.call_args[0][0]
            self.assertEqual(len(args), 1)
            self.assertEqual(args[0].union.mi.dx, 1)


if __name__ == "__main__":
    unittest.main()
