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
from game_recorder.auto_move.trajectory_patterns import (
    INVERSE_ROTATION,
    INVERSE_TRANSLATION,
    PARADIGMS,
    PAUSE_PARADIGM,
    Episode,
    PlannedTurn,
    limit_pitch_runs,
    paradigm_sequence,
    peak_outbound_units,
    plan_episode,
    plan_pause,
)
from game_recorder.camera_sync import GTA_CAMERA_SOURCE, WUKONG_CAMERA_SOURCE
from game_recorder.config import Config
from game_recorder.storage.auto_move_writer import (
    AUTO_MOVE_FILENAME,
    AUTO_MOVE_SCHEMA,
    AutoMoveWriter,
)


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

    def test_sky_bins_are_not_attractive_look_targets(self) -> None:
        maps = CoverageMaps()
        maps.set_anchor(anchor_x=0.0, anchor_y=0.0, radius_m=3.0)
        pitch = math.radians(45)
        nov = maps.novelty_look(
            forward_x=0.0,
            forward_y=math.cos(pitch),
            forward_z=math.sin(pitch),
        )
        self.assertGreater(nov["pitch_down"], nov["pitch_up"])
        self.assertGreater(nov["yaw_right"], nov["pitch_up"])

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


class TrajectoryPatternTests(unittest.TestCase):
    def test_inverse_maps_are_involutions_over_the_wbench_vocabulary(self) -> None:
        for name, table in (("translation", INVERSE_TRANSLATION), ("rotation", INVERSE_ROTATION)):
            for token, opposite in table.items():
                self.assertEqual(table[opposite], token, f"{name}:{token}")

    def test_roundtrip_second_half_reverses_the_first(self) -> None:
        rng = random.Random(0)
        for _ in range(20):
            seq = paradigm_sequence("roundtrip", rng=rng, turn_count=4)
            self.assertEqual(seq[0], seq[1])
            self.assertEqual(seq[2], seq[3])
            self.assertEqual(seq[2], INVERSE_TRANSLATION[seq[0]])

    def test_loop_is_a_closing_four_cycle(self) -> None:
        rng = random.Random(1)
        for _ in range(20):
            a, b, c, d = paradigm_sequence("loop", rng=rng, turn_count=4)
            self.assertEqual(c, INVERSE_TRANSLATION[a])
            self.assertEqual(d, INVERSE_TRANSLATION[b])
            # A perpendicular pair, otherwise the "loop" is just a roundtrip.
            self.assertNotIn(b, (a, INVERSE_TRANSLATION[a]))

    def test_l_shape_turns_perpendicular_without_returning(self) -> None:
        rng = random.Random(2)
        for _ in range(20):
            seq = paradigm_sequence("l_shape", rng=rng, turn_count=4)
            first, second = seq[0], seq[2]
            self.assertEqual(seq[:2], (first, first))
            self.assertEqual(seq[2:], (second, second))
            self.assertNotIn(second, (first, INVERSE_TRANSLATION[first]))

    def test_zigzag_alternates_with_period_one(self) -> None:
        rng = random.Random(3)
        for _ in range(20):
            seq = paradigm_sequence("zigzag", rng=rng, turn_count=4)
            self.assertEqual(seq[0], seq[2])
            self.assertEqual(seq[1], seq[3])
            self.assertNotEqual(seq[0], seq[1])

    def test_repeat_holds_one_token_and_progressive_never_repeats(self) -> None:
        rng = random.Random(4)
        self.assertEqual(len(set(paradigm_sequence("repeat", rng=rng, turn_count=4))), 1)
        for _ in range(20):
            seq = paradigm_sequence("progressive", rng=rng, turn_count=4)
            for previous, current in zip(seq, seq[1:]):
                self.assertNotEqual(previous, current)

    def test_peak_outbound_reflects_path_geometry(self) -> None:
        rng = random.Random(5)
        repeat = plan_episode(
            rng=rng, hold_s=4.0, paradigm="repeat",
            channel="translation", turn_count=4,
        )
        roundtrip = plan_episode(
            rng=rng, hold_s=4.0, paradigm="roundtrip",
            channel="translation", turn_count=4,
        )
        loop = plan_episode(
            rng=rng, hold_s=4.0, paradigm="loop",
            channel="translation", turn_count=4,
        )
        look = plan_episode(
            rng=rng, hold_s=4.0, paradigm="roundtrip",
            channel="rotation", carrier="none", turn_count=4,
        )
        self.assertAlmostEqual(peak_outbound_units(repeat.turns), 4.0)
        self.assertAlmostEqual(peak_outbound_units(roundtrip.turns), 2.0)
        self.assertAlmostEqual(peak_outbound_units(loop.turns), math.sqrt(2.0), places=5)
        self.assertEqual(peak_outbound_units(look.turns), 0.0)

    def test_rotation_channel_carrier_walks_while_looking(self) -> None:
        episode = plan_episode(
            rng=random.Random(6),
            hold_s=4.0,
            paradigm="roundtrip",
            channel="rotation",
            carrier="forward",
            turn_count=4,
        )
        # This is WBench's ``W+up W+up W+down W+down``: pitch reverses, walk holds.
        self.assertEqual({t.translation for t in episode.turns}, {"forward"})
        self.assertEqual(episode.turns[2].rotation, INVERSE_ROTATION[episode.turns[0].rotation])

    def test_every_planned_pair_exists_in_the_catalog(self) -> None:
        catalog = load_action_catalog(alpha=1.0)
        rng = random.Random(7)
        for paradigm in PARADIGMS:
            for channel in ("translation", "rotation"):
                for carrier in ("none", "forward"):
                    episode = plan_episode(
                        rng=rng,
                        hold_s=4.0,
                        paradigm=paradigm,
                        channel=channel,
                        carrier=carrier,
                    )
                    for turn in episode.turns:
                        self.assertIn(
                            (turn.translation, turn.rotation),
                            catalog.by_pair,
                            f"{paradigm}/{channel}/{carrier}",
                        )

    def test_yaw_only_alphabet_falls_back_for_perpendicular_paradigms(self) -> None:
        # Without pitch the rotation alphabet has a single axis, so loop/l_shape
        # cannot close or turn — they must be planned on translations instead.
        for paradigm in ("loop", "l_shape"):
            episode = plan_episode(
                rng=random.Random(8),
                hold_s=4.0,
                paradigm=paradigm,
                channel="rotation",
                allow_pitch=False,
            )
            self.assertEqual(episode.channel, "translation")

    def test_loop_keeps_rotation_channel_at_the_look_rate(self) -> None:
        # Horizontal pans in loop / l_shape must not be rewritten as walks just
        # to avoid pitch; pitch legs are shortened later instead.
        rng = random.Random(0)
        n = 400
        rotation = 0
        for _ in range(n):
            episode = plan_episode(
                rng=rng, hold_s=4.0, paradigm="loop", allow_pitch=True
            )
            if episode.channel == "rotation":
                rotation += 1
        self.assertGreater(rotation / n, 0.30)
        self.assertLess(rotation / n, 0.50)

    def test_rotation_channel_samples_pitch_less_than_yaw(self) -> None:
        rng = random.Random(0)
        n = 400
        pitch = 0
        for _ in range(n):
            seq = paradigm_sequence(
                "repeat", rng=rng, turn_count=1, channel="rotation"
            )
            if "pitch" in seq[0]:
                pitch += 1
        self.assertLess(pitch / n, 0.30)
        self.assertGreater(pitch / n, 0.05)

    def test_limit_pitch_runs_shortens_same_direction_holds(self) -> None:
        turns = tuple(
            PlannedTurn("none", "pitch_up", 4.0, i) for i in range(4)
        )
        limited = limit_pitch_runs(turns, max_same_dir_s=2.0)
        self.assertAlmostEqual(sum(t.hold_s for t in limited), 2.0)
        yaw = tuple(PlannedTurn("none", "yaw_right", 4.0, i) for i in range(4))
        self.assertEqual(limit_pitch_runs(yaw, max_same_dir_s=2.0), yaw)


class ParadigmPolicyTests(unittest.TestCase):
    @staticmethod
    def _pose(x: float, y: float, source: str = "gta") -> UnifiedPose:
        return UnifiedPose(
            0, x, y, 0.0, source, forward_x=0.0, forward_y=1.0, forward_z=0.0
        )

    @staticmethod
    def _start(policy: BalancedRadiusPolicy) -> None:
        """Clean slate under a caller-supplied clock.

        ``reset()`` seeds the first action off the real monotonic clock, so tests
        that pass their own ``now`` must clear that hold and its episode first.
        Stand-still pauses are suppressed so the assertions below see a paradigm
        episode at the first boundary regardless of the seed.
        """
        policy.pause_chance = 0.0
        policy.reset()
        policy._abort_episode()
        policy._hold_until = 0.0
        policy._paradigm_hold_off_until = 0.0

    def test_planned_turns_are_labeled_and_held_for_the_turn_duration(self) -> None:
        policy = BalancedRadiusPolicy(
            radius_m=50.0,
            paradigm_turn_hold_s=4.0,
            paradigm_episode_chance=1.0,
            rate_track_hz=100.0,
            rng=random.Random(0),
        )
        self._start(policy)
        action = policy.step(self._pose(0.0, 0.0), dt=1.0 / 30.0, now=100.0)
        self.assertIn(action.paradigm, PARADIGMS)
        self.assertEqual(action.turn_index, 0)
        self.assertAlmostEqual(policy._hold_until, 104.0)

    def test_turn_index_advances_across_the_episode(self) -> None:
        policy = BalancedRadiusPolicy(
            radius_m=50.0,
            paradigm_turn_hold_s=1.0,
            paradigm_episode_chance=1.0,
            rate_track_hz=100.0,
            rng=random.Random(1),
        )
        self._start(policy)
        # Keep drifting: a perfectly still pose trips the stuck detector, which
        # preempts the plan for its own reasons.
        first = policy.step(self._pose(0.0, 0.0), dt=1.0 / 30.0, now=100.0)
        second = policy.step(self._pose(0.0, 1.0), dt=1.0 / 30.0, now=101.5)
        self.assertEqual(first.turn_index, 0)
        self.assertEqual(second.turn_index, 1)
        self.assertEqual(first.paradigm, second.paradigm)

    def test_look_rates_stay_locked_for_the_whole_episode(self) -> None:
        """A roundtrip only closes in yaw if both legs turn at the same rate."""
        policy = BalancedRadiusPolicy(
            radius_m=50.0,
            paradigm_turn_hold_s=1.0,
            rate_track_hz=100.0,
            rng=random.Random(2),
        )
        self._start(policy)
        # Pin a 4-turn look-only roundtrip so the episode cannot end early and
        # replan (which legitimately draws fresh rates).
        policy._episode = plan_episode(
            rng=random.Random(0),
            hold_s=1.0,
            paradigm="roundtrip",
            channel="rotation",
            carrier="none",
            turn_count=4,
        )
        policy._episode_turn = 0

        policy.step(self._pose(0.0, 0.0), dt=1.0 / 30.0, now=100.0)
        locked = (policy._action_yaw_deg_s, policy._action_pitch_deg_s)
        self.assertGreater(locked[0], 0.0)
        for index in range(1, 4):
            policy.step(
                self._pose(0.0, float(index)), dt=1.0 / 30.0, now=100.0 + 1.5 * index
            )
            self.assertEqual(policy._current_turn_index, index)
            self.assertEqual(
                (policy._action_yaw_deg_s, policy._action_pitch_deg_s), locked
            )

    def test_translation_episode_hold_is_capped_by_the_radius_budget(self) -> None:
        policy = BalancedRadiusPolicy(
            radius_m=20.0,
            soft_radius_frac=0.55,
            paradigm_turn_hold_s=4.0,
            paradigm_margin_m=1.0,
            movement_speed_scales={"rdr2": 0.6},
            rng=random.Random(3),
        )
        policy._anchor_x = 0.0
        policy._anchor_y = 0.0
        rdr2 = self._pose(0.0, 0.0, source="rdr2")
        # soft = 11m, margin 1m, RDR2 plans at 5.0 * 0.6 = 3.0 m/s. A roundtrip
        # walks out 2 turns, so 4.0s/turn (24m) cannot fit and must be cut down.
        roundtrip = plan_episode(
            rng=random.Random(0), hold_s=4.0, paradigm="roundtrip",
            channel="translation", turn_count=4,
        )
        budget = policy._paradigm_hold_budget(roundtrip, pose=rdr2)
        self.assertLess(budget, 4.0)
        self.assertAlmostEqual(budget, 10.0 / (2.0 * 3.0))

        # Look-only plans do not move, so they keep the full WBench duration.
        look = plan_episode(
            rng=random.Random(0), hold_s=4.0, paradigm="roundtrip",
            channel="rotation", carrier="none", turn_count=4,
        )
        self.assertAlmostEqual(policy._paradigm_hold_budget(look, pose=rdr2), 4.0)

    def test_safety_margin_does_not_veto_plans_that_head_inward(self) -> None:
        """Regression: inside the margin band every plan used to be rejected."""
        policy = BalancedRadiusPolicy(
            radius_m=20.0,
            soft_radius_frac=0.55,
            paradigm_turn_hold_s=4.0,
            paradigm_margin_m=1.0,
            movement_speed_scales={"rdr2": 0.6},
            rng=random.Random(9),
        )
        policy._anchor_x = 0.0
        policy._anchor_y = 0.0
        # soft = 11m, margin 1m. Sitting at 10.5m is past soft-margin but still
        # inside soft, which is where the policy spends much of its time.
        inward = Episode(
            paradigm="roundtrip",
            channel="translation",
            carrier="none",
            turns=tuple(
                PlannedTurn(
                    translation=name, rotation="none", hold_s=4.0, turn_index=index
                )
                for index, name in enumerate(
                    ("forward", "forward", "backward", "backward")
                )
            ),
        )
        # Facing -Y at +Y 10.5 means "forward" walks back toward the anchor.
        toward = UnifiedPose(
            0, 0.0, 10.5, 0.0, "rdr2", forward_x=0.0, forward_y=-1.0, forward_z=0.0
        )
        away = UnifiedPose(
            0, 0.0, 10.5, 0.0, "rdr2", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        self.assertGreater(policy._paradigm_hold_budget(inward, pose=toward), 1.2)
        self.assertEqual(policy._paradigm_hold_budget(inward, pose=away), 0.0)

    def test_tight_budget_replans_the_paradigm_as_look_only(self) -> None:
        policy = BalancedRadiusPolicy(
            radius_m=5.0,
            soft_radius_frac=0.55,
            paradigm_turn_hold_s=4.0,
            paradigm_min_turn_hold_s=1.2,
            paradigm_margin_m=1.0,
            rng=random.Random(4),
        )
        policy._anchor_x = 0.0
        policy._anchor_y = 0.0
        # soft = 2.75m, minus 1m margin at 5 m/s leaves far less than 1.2s/turn
        # for any translation plan, so the episode must degrade to pure look.
        policy._start_episode(self._pose(0.0, 0.0, source="wukong"))
        episode = policy._episode
        assert episode is not None
        self.assertEqual(peak_outbound_units(episode.turns), 0.0)
        for turn in episode.turns:
            if "pitch" in turn.rotation:
                self.assertLessEqual(turn.hold_s, 4.0)
            else:
                self.assertAlmostEqual(turn.hold_s, 4.0)

    def test_radius_interrupt_drops_the_episode_and_holds_off_replanning(self) -> None:
        """A cut-short turn must not silently advance the plan every tick."""
        catalog = load_action_catalog(alpha=1.0)
        policy = BalancedRadiusPolicy(
            radius_m=0.5,
            soft_radius_frac=0.5,
            paradigm_turn_hold_s=4.0,
            paradigm_episode_chance=1.0,
            paradigm_cooldown_s=3.0,
            rate_track_hz=100.0,
            look_yaw_deg_s=0.0,
            look_pitch_deg_s=0.0,
            return_yaw_deg_s=0.0,
            catalog=catalog,
            rng=random.Random(5),
        )
        policy.reset()
        policy.step(self._pose(0.0, 0.0), dt=0.05, now=100.0)
        policy._current = catalog.by_pair[("forward", "none")]
        policy._hold_until = 1e9
        action = policy.step(self._pose(0.0, 0.4), dt=0.05, now=101.0)
        self.assertIsNone(policy._episode)
        self.assertIsNone(action.paradigm)
        self.assertAlmostEqual(policy._paradigm_hold_off_until, 104.0)

    def test_stuck_escape_drops_the_episode_label(self) -> None:
        policy = BalancedRadiusPolicy(
            radius_m=50.0,
            stuck_speed_mps=0.5,
            stuck_s=0.2,
            paradigm_turn_hold_s=4.0,
            rate_track_hz=100.0,
            catalog=load_action_catalog(alpha=1.0),
            rng=random.Random(6),
        )
        self._start(policy)
        # A walking plan that makes no progress: a wall, so the escape wins.
        policy._episode = plan_episode(
            rng=random.Random(0),
            hold_s=4.0,
            paradigm="repeat",
            channel="translation",
            carrier="none",
            turn_count=4,
        )
        policy._episode_turn = 0
        first = policy.step(self._pose(0.0, 0.0), dt=0.05, now=100.0)
        self.assertEqual(first.paradigm, "repeat")
        self.assertNotEqual(first.translation, "none")

        policy.step(self._pose(0.0, 0.01), dt=0.05, now=100.1)
        action = policy.step(self._pose(0.0, 0.02), dt=0.05, now=100.4)
        self.assertIsNone(action.paradigm)
        self.assertIsNone(policy._episode)

    def test_look_only_turn_is_not_treated_as_stuck(self) -> None:
        """A third of WBench turns stand still and only look — not a wall."""
        policy = BalancedRadiusPolicy(
            radius_m=50.0,
            stuck_speed_mps=0.5,
            stuck_s=0.2,
            paradigm_turn_hold_s=4.0,
            rate_track_hz=100.0,
            catalog=load_action_catalog(alpha=1.0),
            rng=random.Random(7),
        )
        self._start(policy)
        policy._episode = plan_episode(
            rng=random.Random(0),
            hold_s=4.0,
            paradigm="roundtrip",
            channel="rotation",
            carrier="none",
            turn_count=4,
        )
        policy._episode_turn = 0
        pose = self._pose(0.0, 0.0)
        first = policy.step(pose, dt=0.05, now=100.0)
        self.assertEqual(first.translation, "none")
        for tick in range(1, 40):
            policy.step(pose, dt=0.05, now=100.0 + 0.1 * tick)
        self.assertIsNone(policy._stuck_since)
        self.assertEqual(policy._current_paradigm, "roundtrip")

    def test_gaps_between_episodes_keep_free_sampling_alive(self) -> None:
        """Paradigms use ~15 of 81 bins; gaps keep the rest reachable."""
        policy = BalancedRadiusPolicy(
            radius_m=50.0,
            paradigm_episode_chance=0.0,
            paradigm_gap_min_actions=2,
            paradigm_gap_max_actions=2,
            paradigm_turn_hold_s=4.0,
            hold_min_s=1.0,
            hold_max_s=1.0,
            rate_track_hz=100.0,
            rng=random.Random(8),
        )
        self._start(policy)
        for tick in range(4):
            action = policy.step(
                self._pose(0.0, float(tick)), dt=1.0 / 30.0, now=100.0 + 1.5 * tick
            )
            self.assertIsNone(action.paradigm)
        self.assertIsNone(policy._episode)

    def test_disabling_paradigms_restores_free_sampling(self) -> None:
        policy = BalancedRadiusPolicy(
            radius_m=50.0,
            paradigms=False,
            hold_min_s=2.5,
            hold_max_s=4.5,
            rate_track_hz=100.0,
            rng=random.Random(7),
        )
        policy.reset()
        action = policy.step(self._pose(0.0, 0.0), dt=1.0 / 30.0, now=100.0)
        self.assertIsNone(action.paradigm)
        self.assertIsNone(action.turn_index)
        self.assertIsNone(policy._episode)


class PausePolicyTests(unittest.TestCase):
    """Stand-still stretches: real play is not in constant motion."""

    _pose = staticmethod(ParadigmPolicyTests._pose)

    @staticmethod
    def _start(policy: BalancedRadiusPolicy, *, chance: float) -> None:
        ParadigmPolicyTests._start(policy)
        policy.pause_chance = chance

    def test_plan_pause_is_one_motionless_turn(self) -> None:
        episode = plan_pause(7.5)
        self.assertEqual(episode.paradigm, PAUSE_PARADIGM)
        self.assertNotIn(PAUSE_PARADIGM, PARADIGMS)
        self.assertEqual(len(episode), 1)
        self.assertEqual(episode.turns[0].translation, "none")
        self.assertEqual(episode.turns[0].rotation, "none")
        self.assertEqual(episode.turns[0].hold_s, 7.5)
        self.assertEqual(episode.peak_outbound_units, 0.0)
        self.assertTrue(episode.heading_frozen)

    def test_pause_is_labeled_and_held_for_a_duration_in_range(self) -> None:
        for seed in range(6):
            policy = BalancedRadiusPolicy(
                radius_m=50.0,
                pause_min_s=5.0,
                pause_max_s=15.0,
                rate_track_hz=100.0,
                rng=random.Random(seed),
            )
            self._start(policy, chance=1.0)
            action = policy.step(self._pose(0.0, 0.0), dt=1.0 / 30.0, now=100.0)
            self.assertEqual(action.paradigm, PAUSE_PARADIGM)
            self.assertEqual(action.turn_index, 0)
            self.assertEqual(action.translation, "none")
            self.assertEqual(action.rotation, "none")
            self.assertEqual(action.keys, frozenset())
            self.assertGreaterEqual(policy._hold_until, 105.0)
            self.assertLessEqual(policy._hold_until, 115.0)

    def test_pause_commands_no_keys_and_settles_to_no_look(self) -> None:
        policy = BalancedRadiusPolicy(
            radius_m=50.0,
            pause_min_s=8.0,
            pause_max_s=8.0,
            rate_track_hz=100.0,
            rng=random.Random(3),
        )
        self._start(policy, chance=1.0)
        pose = self._pose(0.0, 0.0)
        for tick in range(30):
            action = policy.step(pose, dt=1.0 / 30.0, now=100.0 + tick / 30.0)
            self.assertEqual(action.keys, frozenset())
        # The look rate is low-passed, so it glides to a stop rather than cutting.
        self.assertAlmostEqual(action.yaw_deg_s, 0.0, places=3)
        self.assertAlmostEqual(action.pitch_deg_s, 0.0, places=3)
        self.assertEqual(action.paradigm, PAUSE_PARADIGM)

    def test_long_pause_does_not_trip_the_stuck_detector(self) -> None:
        policy = BalancedRadiusPolicy(
            radius_m=50.0,
            stuck_speed_mps=0.5,
            stuck_s=0.2,
            pause_min_s=15.0,
            pause_max_s=15.0,
            rate_track_hz=100.0,
            rng=random.Random(4),
        )
        self._start(policy, chance=1.0)
        pose = self._pose(0.0, 0.0)
        for tick in range(150):
            action = policy.step(pose, dt=0.1, now=100.0 + 0.1 * tick)
        self.assertIsNone(policy._stuck_since)
        self.assertEqual(action.paradigm, PAUSE_PARADIGM)

    def test_no_pause_is_planned_while_the_radius_forces_a_return(self) -> None:
        """Standing still outside the boundary would only prolong the excursion."""
        policy = BalancedRadiusPolicy(
            radius_m=10.0,
            rate_track_hz=100.0,
            catalog=load_action_catalog(alpha=1.0),
            rng=random.Random(5),
        )
        self._start(policy, chance=1.0)
        policy._anchor_x = 0.0
        policy._anchor_y = 0.0
        action = policy.step(self._pose(0.0, 30.0), dt=1.0 / 30.0, now=100.0)
        self.assertIsNone(action.paradigm)
        self.assertNotEqual(action.translation, "none")

    def test_zero_chance_never_pauses(self) -> None:
        policy = BalancedRadiusPolicy(
            radius_m=50.0,
            paradigm_episode_chance=1.0,
            rate_track_hz=100.0,
            rng=random.Random(6),
        )
        self._start(policy, chance=0.0)
        seen = set()
        for tick in range(60):
            action = policy.step(
                self._pose(0.0, float(tick)), dt=1.0 / 30.0, now=100.0 + 2.0 * tick
            )
            seen.add(action.paradigm)
        self.assertNotIn(PAUSE_PARADIGM, seen)


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

    def test_combined_yaw_pitch_is_not_suppressed_like_pure_pitch(self) -> None:
        policy = BalancedRadiusPolicy()
        level = UnifiedPose(
            0, 0.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        pure = policy._look_behavior_weight("pitch_up", clock=1.0, pose=level)
        combo = policy._look_behavior_weight(
            "yaw_right_pitch_up", clock=1.0, pose=level
        )
        yaw = policy._look_behavior_weight("yaw_right", clock=1.0, pose=level)
        self.assertGreater(combo, pure)
        self.assertGreater(yaw, combo)

    def test_pitch_command_stops_at_the_limit(self) -> None:
        policy = BalancedRadiusPolicy(pitch_limit_deg=25.0)
        policy._action_yaw_deg_s = 0.0
        policy._action_pitch_deg_s = 12.0
        high = UnifiedPose(
            0,
            0.0,
            0.0,
            0.0,
            "gta",
            forward_x=0.0,
            forward_y=math.cos(math.radians(30)),
            forward_z=math.sin(math.radians(30)),
        )
        up = policy._to_wander_action(
            policy._catalog.by_pair[("none", "pitch_up")], pose=high
        )
        self.assertEqual(up.pitch_deg_s, 0.0)
        down = policy._to_wander_action(
            policy._catalog.by_pair[("none", "pitch_down")], pose=high
        )
        self.assertGreater(down.pitch_deg_s, 0.0)

    def test_free_pitch_holds_are_capped(self) -> None:
        turn = DiscreteAction(0, "none", "pitch_up", 1.0, 1.0)
        catalog = ActionCatalog(
            actions=(turn,),
            by_id={0: turn},
            by_pair={("none", "pitch_up"): turn},
            weights=(1.0,),
        )
        policy = BalancedRadiusPolicy(
            catalog=catalog,
            paradigms=False,
            look_pitch_deg_s=15.0,
            pitch_limit_deg=25.0,
            rng=random.Random(0),
        )
        policy._resample(1.0, pose=None, force_stuck=False)
        self.assertLessEqual(policy._hold_until - 1.0, 25.0 / 15.0 + 1e-6)
        self.assertGreater(policy._hold_until, 1.0)

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
        policy = BalancedRadiusPolicy(
            walk_speed_mps=5.0,
            movement_speed_scale=0.5,
            movement_speed_scales={"gta": 0.5, "rdr2": 0.2, "cp2077": 0.4},
        )
        gta = UnifiedPose(
            0, 0.0, 0.0, 0.0, "gta", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        rdr2 = UnifiedPose(
            0, 0.0, 0.0, 0.0, "rdr2", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        cp2077 = UnifiedPose(
            0, 0.0, 0.0, 0.0, "cp2077", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        wukong = UnifiedPose(
            0, 0.0, 0.0, 0.0, "wukong", forward_x=0.0, forward_y=1.0, forward_z=0.0
        )
        self.assertEqual(policy._estimated_walk_speed(gta), 2.5)
        self.assertEqual(policy._estimated_walk_speed(rdr2), 1.0)
        self.assertEqual(policy._estimated_walk_speed(cp2077), 2.0)
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


class AutoMoveWriterTests(unittest.TestCase):
    @staticmethod
    def _read(path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _writer(self, path: Path, **kwargs) -> AutoMoveWriter:
        return AutoMoveWriter(
            path,
            t0_perf_ns=1_000_000_000,
            t0_epoch_ms=1_700_000_000_000,
            fps=30,
            **kwargs,
        )

    def test_repeated_labels_collapse_to_one_record(self) -> None:
        """The policy re-decides at 30 Hz but holds each turn for seconds."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auto_move.jsonl"
            writer = self._writer(path)
            label = {
                "action_id": 4,
                "translation": "forward",
                "rotation": "none",
                "paradigm": "roundtrip",
                "turn_index": 0,
            }
            self.assertTrue(writer.write(**label, perf_ns=1_000_000_000))
            for tick in range(1, 120):
                self.assertFalse(
                    writer.write(**label, perf_ns=1_000_000_000 + tick * 33_000_000)
                )
            self.assertTrue(
                writer.write(
                    action_id=4,
                    translation="forward",
                    rotation="none",
                    paradigm="roundtrip",
                    turn_index=1,
                    perf_ns=5_000_000_000,
                )
            )
            writer.close()

            records = self._read(path)
            self.assertEqual(writer.total_written, 2)
            self.assertEqual([r["turn_index"] for r in records], [0, 1])

    def test_frame_and_timestamp_track_the_session_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auto_move.jsonl"
            writer = self._writer(path)
            # 2.0s after t0 at 30 fps → frame 60.
            writer.write(
                action_id=0,
                translation="none",
                rotation="yaw_right",
                paradigm=None,
                turn_index=None,
                perf_ns=3_000_000_000,
            )
            writer.close()

            record = self._read(path)[0]
            self.assertEqual(record["frame"], 60)
            self.assertEqual(record["t_unix_ms"], 1_700_000_002_000.0)
            self.assertIsNone(record["paradigm"])
            self.assertIsNone(record["turn_index"])

    def test_buffered_records_survive_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auto_move.jsonl"
            writer = self._writer(path, buffer_records=64)
            for index in range(10):
                writer.write(
                    action_id=index,
                    translation="forward",
                    rotation="none",
                    paradigm="repeat",
                    turn_index=index,
                    perf_ns=1_000_000_000 + index * 1_000_000_000,
                )
            writer.close()
            writer.close()  # idempotent
            self.assertEqual(len(self._read(path)), 10)

    def test_runner_forwards_policy_labels_to_the_writer(self) -> None:
        """Plumbing check without starting the injection thread."""
        from game_recorder.auto_move.policy_wander import WanderAction
        from game_recorder.auto_move.runner import AutoMoveRunner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = self._writer(root / "auto_move.jsonl")
            runner = AutoMoveRunner(
                output_dir=root,
                session_dir=root / "session_x",
                sources=(),
                label_writer=writer,
            )
            runner._log_action(
                WanderAction(
                    keys=frozenset({VK_W}),
                    action_id=13,
                    translation="forward",
                    rotation="none",
                    paradigm="l_shape",
                    turn_index=2,
                )
            )
            writer.close()

            record = self._read(root / "auto_move.jsonl")[0]
            self.assertEqual(record["action_id"], 13)
            self.assertEqual(record["paradigm"], "l_shape")
            self.assertEqual(record["turn_index"], 2)

    def test_writer_failure_never_breaks_the_injection_loop(self) -> None:
        from game_recorder.auto_move.policy_wander import WanderAction
        from game_recorder.auto_move.runner import AutoMoveRunner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = self._writer(root / "auto_move.jsonl")
            runner = AutoMoveRunner(
                output_dir=root,
                session_dir=root / "session_x",
                sources=(),
                label_writer=writer,
            )
            with mock.patch.object(writer, "write", side_effect=OSError("disk full")):
                runner._log_action(WanderAction(keys=frozenset(), action_id=1))
            self.assertIsNone(runner._label_writer)
            writer.close()

    def test_meta_carries_the_sidecar_reference(self) -> None:
        from game_recorder.storage.session_writer import SessionMeta

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meta.json"
            SessionMeta(
                auto_move_file=AUTO_MOVE_FILENAME,
                auto_move_schema=AUTO_MOVE_SCHEMA,
                auto_move_actions=7,
            ).save(path)
            meta = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(meta["auto_move_file"], "auto_move.jsonl")
            self.assertEqual(meta["auto_move_schema"], "auto_move_actions_v1")
            self.assertEqual(meta["auto_move_actions"], 7)
            # Absent when auto-move did not run.
            SessionMeta().save(path)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["auto_move_actions"], 0
            )


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
            self.assertEqual(cfg.auto_move_radius_m, 20.0)
            self.assertEqual(cfg.auto_move_action_hold_min_s, 2.5)
            self.assertEqual(cfg.auto_move_action_hold_max_s, 4.5)
            self.assertEqual(cfg.auto_move_speed_scale, 0.1)
            self.assertEqual(cfg.auto_move_speed_scale_gta, 0.1)
            self.assertEqual(cfg.auto_move_speed_scale_rdr2, 0.6)
            self.assertEqual(cfg.auto_move_speed_scale_cp2077, 0.15)
            self.assertEqual(cfg.movement_speed_scale_for("gta"), 0.1)
            self.assertEqual(cfg.movement_speed_scale_for("rdr2"), 0.6)
            self.assertEqual(cfg.movement_speed_scale_for("cp2077"), 0.15)
            self.assertEqual(cfg.movement_speed_scale_for("wukong"), 1.0)
            self.assertEqual(cfg.auto_move_look_yaw_min_deg_s, 15.0)
            self.assertEqual(cfg.auto_move_look_yaw_max_deg_s, 30.0)
            self.assertEqual(cfg.auto_move_look_pitch_min_deg_s, 6.0)
            self.assertEqual(cfg.auto_move_look_pitch_max_deg_s, 15.0)
            self.assertEqual(cfg.auto_move_pitch_limit_deg, 25.0)
            self.assertEqual(cfg.auto_move_cover_move_beta, 1.5)
            self.assertEqual(cfg.auto_move_cover_look_gamma, 8.0)
            self.assertTrue(cfg.auto_move_paradigms)
            self.assertEqual(cfg.auto_move_paradigm_episode_chance, 0.85)
            self.assertEqual(cfg.auto_move_paradigm_turn_hold_s, 4.0)
            self.assertEqual(cfg.auto_move_paradigm_min_turn_hold_s, 1.2)
            self.assertEqual(cfg.auto_move_paradigm_cooldown_s, 3.0)
            self.assertEqual(cfg.auto_move_paradigm_weights, {})
            self.assertEqual(cfg.auto_move_pause_chance, 0.15)
            self.assertEqual(cfg.auto_move_pause_min_s, 5.0)
            self.assertEqual(cfg.auto_move_pause_max_s, 15.0)
            self.assertEqual(cfg.idle_timeout_s, 0.0)
            self.assertEqual(cfg.violent_duration_s, 0.0)
            self.assertEqual(cfg.max_recording_duration_s, 1800.0)

    def test_focus_lost_stop_window_outlasts_the_auto_move_focus_restore(self) -> None:
        from game_recorder.auto_move.runner import _FOCUS_REFRESH_S

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(output_dir=Path(tmp) / "out")
            # The restorer must get at least two attempts before the watchdog fires,
            # otherwise a transient focus steal always kills an unattended session.
            self.assertGreaterEqual(cfg.focus_lost_stop_after_s, 2 * _FOCUS_REFRESH_S)

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
