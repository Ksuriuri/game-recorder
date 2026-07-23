"""Unit tests for auto-move pose normalization, wander policy, and config wiring."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_recorder.auto_move.input_inject import VK_S, VK_W, InputInjector
from game_recorder.auto_move.policy_wander import WanderPhase, WanderPolicy, apply_action
from game_recorder.auto_move.pose_live import (
    LivePoseReader,
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
        from game_recorder.auto_move.pose_live import UnifiedPose

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
