from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_recorder.camera_sync import (
    CP2077_CAMERA_SOURCE,
    finalize_session_cameras,
    frame_alignment_window_ms,
)
from game_recorder.depth_sync import finalize_cp2077_depth


class Cp2077DepthTests(unittest.TestCase):
    def test_five_fps_alignment_window_covers_one_and_a_quarter_frames(self) -> None:
        self.assertEqual(frame_alignment_window_ms(30), 50.0)
        self.assertEqual(frame_alignment_window_ms(5), 250.0)
        self.assertEqual(frame_alignment_window_ms(1), 250.0)

    def test_depth_samples_align_to_capture_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir)
            depth_dir = session_dir / "depth"
            depth_dir.mkdir()
            (depth_dir / "depth_00000000.npy").write_bytes(b"npy-a")
            (depth_dir / "depth_00000001.npy").write_bytes(b"npy-b")

            header = {
                "type": "header",
                "schema": "cp2077_depth_v1",
                "definition": "OpenCV camera-coordinate optical-axis value Zc",
                "camera_axes": "x_right_y_down_z_forward",
                "units": "m",
                "dtype": "<f4",
                "array_layout": "H_W",
                "calibration": {"kind": "empirical_cp2077_device_depth_to_zc"},
            }
            samples = [
                {
                    "type": "sample",
                    "seq": 0,
                    "t_unix_ms": 1010,
                    "file": "depth/depth_00000000.npy",
                    "width": 2,
                    "height": 1,
                },
                {
                    "type": "sample",
                    "seq": 1,
                    "t_unix_ms": 1190,
                    "file": "depth/depth_00000001.npy",
                    "width": 2,
                    "height": 1,
                },
            ]
            raw_lines = [header, *samples, {"type": "footer"}]
            (session_dir / "depth_raw_cp2077.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in raw_lines),
                encoding="utf-8",
            )
            (session_dir / "frame_timestamps.jsonl").write_text(
                json.dumps({"frame": 0, "t_capture_unix_ms": 1000.0})
                + "\n"
                + json.dumps({"frame": 1, "t_capture_unix_ms": 1200.0})
                + "\n",
                encoding="utf-8",
            )
            meta = {
                "start_epoch_ms": 1000,
                "fps": 5,
                "total_frames": 2,
                "frame_timestamps_file": "frame_timestamps.jsonl",
            }
            (session_dir / "meta.json").write_text("{}\n", encoding="utf-8")

            summary = finalize_cp2077_depth(
                session_dir,
                meta,
                wait_raw_s=0,
                max_dt_ms=50,
            )

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["status"], "aligned")
            self.assertEqual(summary["frames_matched"], 2)
            records = [
                json.loads(line)
                for line in (session_dir / "depth.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual([record["frame"] for record in records], [0, 1])
            self.assertEqual([record["dt_ms"] for record in records], [10.0, -10.0])
            saved_meta = json.loads(
                (session_dir / "meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved_meta["depth"]["units"], "m")

    def test_camera_and_depth_share_every_video_frame_in_one_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir)
            depth_dir = session_dir / "depth"
            depth_dir.mkdir()
            (depth_dir / "depth_00000000.npy").write_bytes(b"npy")

            frame_times = [
                {"frame": 0, "t_capture_unix_ms": 1_000.0},
                {"frame": 1, "t_capture_unix_ms": 1_200.0},
            ]
            (session_dir / "frame_timestamps.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in frame_times),
                encoding="utf-8",
            )
            camera_sample = {
                "type": "sample",
                "t_unix_ms": 1_100,
                "intrinsic": {"fx": 1000.0, "fy": 1000.0, "cx": 960.0, "cy": 540.0},
                "world_to_camera": list(range(16)),
            }
            (session_dir / CP2077_CAMERA_SOURCE.raw_filename).write_text(
                json.dumps({"type": "header", "schema": "cp2077_camera_v3"})
                + "\n"
                + json.dumps(camera_sample)
                + "\n",
                encoding="utf-8",
            )
            depth_records = [
                {
                    "type": "header",
                    "schema": "cp2077_depth_v1",
                    "definition": "OpenCV camera-coordinate optical-axis value Zc",
                    "units": "m",
                    "dtype": "<f4",
                    "array_layout": "H_W",
                },
                {
                    "type": "sample",
                    "seq": 0,
                    "t_unix_ms": 1_100,
                    "file": "depth/depth_00000000.npy",
                    "width": 2,
                    "height": 1,
                },
                {"type": "footer"},
            ]
            (session_dir / "depth_raw_cp2077.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in depth_records),
                encoding="utf-8",
            )
            meta = {
                "start_epoch_ms": 1_000,
                "fps": 5,
                "total_frames": 2,
                "frame_timestamps_file": "frame_timestamps.jsonl",
            }
            (session_dir / "meta.json").write_text("{}\n", encoding="utf-8")

            camera_summary = finalize_session_cameras(
                session_dir,
                meta,
                (CP2077_CAMERA_SOURCE,),
                wait_raw_s=0,
                keep_raw=True,
            )
            depth_summary = finalize_cp2077_depth(
                session_dir,
                meta,
                wait_raw_s=0,
            )

            self.assertIsNotNone(camera_summary)
            self.assertIsNotNone(depth_summary)
            assert camera_summary is not None and depth_summary is not None
            self.assertEqual(camera_summary["frames_matched"], 2)
            self.assertEqual(depth_summary["frames_matched"], 2)
            camera_rows = [
                json.loads(line)
                for line in (session_dir / "camera.jsonl").read_text().splitlines()
            ]
            depth_rows = [
                json.loads(line)
                for line in (session_dir / "depth.jsonl").read_text().splitlines()
            ]
            self.assertEqual([row["frame"] for row in camera_rows], [0, 1])
            self.assertEqual([row["frame"] for row in depth_rows], [0, 1])
            saved_meta = json.loads((session_dir / "meta.json").read_text())
            self.assertEqual(saved_meta["camera"]["status"], "aligned")
            self.assertEqual(saved_meta["depth"]["status"], "aligned")
            self.assertEqual(saved_meta["depth"]["reused_frame_records"], 1)

    def test_addon_exports_raw_depth_without_reshade_linearization(self) -> None:
        shader = (
            PROJECT_ROOT
            / "cp2077-camera"
            / "payload"
            / "reshade-shaders"
            / "Shaders"
            / "CP2077Depth.fx"
        ).read_text(encoding="utf-8")
        math_header = (
            PROJECT_ROOT
            / "cp2077-camera"
            / "depth-addon"
            / "CP2077DepthMath.hpp"
        ).read_text(encoding="utf-8")
        reshade_header = (
            PROJECT_ROOT
            / "cp2077-camera"
            / "depth-addon"
            / "vendor"
            / "reshade"
            / "include"
            / "reshade.hpp"
        ).read_text(encoding="utf-8")

        self.assertIn("texture CP2077DepthInput : DEPTH", shader)
        self.assertIn("Format = R32F", shader)
        self.assertNotIn("GetLinearizedDepth", shader)
        self.assertIn("kDepthExponentScale = 354.9329993", math_header)
        self.assertIn("device_depth_bits_to_z_m", math_header)
        self.assertIn("#define RESHADE_API_VERSION 18", reshade_header)


if __name__ == "__main__":
    unittest.main()
