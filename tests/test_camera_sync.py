from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_recorder.camera_sync import (
    GTA_CAMERA_SOURCE,
    WUKONG_CAMERA_SOURCE,
    CameraSample,
    FrameCaptureTime,
    align_samples_to_frames,
    finalize_session_cameras,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")


def _base_meta(total_frames: int = 2) -> dict:
    return {
        "session_id": "session_test",
        "start_epoch_ms": 1_000,
        "fps": 30,
        "duration_s": total_frames / 30,
        "event_video_sync_offset": 0,
        "total_frames": total_frames,
        "frame_timestamps_file": "frame_timestamps.jsonl",
    }


class AlignSamplesTests(unittest.TestCase):
    def test_uses_nearest_sample_and_threshold(self) -> None:
        samples = [
            CameraSample(1_005, {"type": "sample", "t_unix_ms": 1_005, "fov": 70}),
            CameraSample(1_040, {"type": "sample", "t_unix_ms": 1_040, "fov": 75}),
        ]
        frame_times = [
            FrameCaptureTime(0, 1_000.0),
            FrameCaptureTime(1, 1_033.0),
            FrameCaptureTime(2, 1_200.0),
        ]

        records, matched, missing = align_samples_to_frames(
            samples,
            start_epoch_ms=1_000,
            fps=30,
            total_frames=3,
            max_dt_ms=50,
            frame_times=frame_times,
        )

        self.assertEqual((matched, missing), (2, 1))
        self.assertEqual([record["frame"] for record in records], [0, 1])
        self.assertEqual([record["fov"] for record in records], [70, 75])
        self.assertEqual([record["dt_ms"] for record in records], [5.0, 7.0])
        self.assertNotIn("type", records[0])

    def test_duplicate_video_times_reuse_camera_sample_with_distinct_frames(self) -> None:
        sample = CameraSample(
            2_000,
            {
                "type": "sample",
                "t_unix_ms": 2_000,
                "pos": [1, 2, 3],
                "rot": [4, 5, 6],
                "fov": 80,
            },
        )
        frame_times = [
            FrameCaptureTime(0, 2_000.0),
            FrameCaptureTime(1, 2_000.0),
        ]

        records, matched, missing = align_samples_to_frames(
            [sample],
            start_epoch_ms=2_000,
            fps=30,
            total_frames=2,
            frame_times=frame_times,
        )

        self.assertEqual((matched, missing), (2, 0))
        self.assertEqual([record["frame"] for record in records], [0, 1])
        self.assertEqual(records[0]["pos"], records[1]["pos"])


class FinalizeCameraTests(unittest.TestCase):
    def test_wukong_source_writes_gta_compatible_basic_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary)
            meta = _base_meta()
            _write_jsonl(
                session_dir / "frame_timestamps.jsonl",
                [
                    {"frame": 0, "t_capture_unix_ms": 1_000.0},
                    {"frame": 1, "t_capture_unix_ms": 1_033.0},
                ],
            )
            _write_jsonl(
                session_dir / WUKONG_CAMERA_SOURCE.raw_filename,
                [
                    {
                        "type": "header",
                        "schema": "wukong_camera_v1",
                        "units": "game_world_meters_deg",
                        "rot_order": "pitch_roll_yaw_deg",
                    },
                    {
                        "type": "sample",
                        "t_unix_ms": 1_001,
                        "pos": [1.0, 2.0, 3.0],
                        "rot": [10.0, 20.0, 30.0],
                        "fov": 75.0,
                    },
                    {
                        "type": "sample",
                        "t_unix_ms": 1_034,
                        "pos": [1.1, 2.1, 3.1],
                        "rot": [11.0, 21.0, 31.0],
                        "fov": 76.0,
                    },
                ],
            )

            summary = finalize_session_cameras(
                session_dir,
                meta,
                (GTA_CAMERA_SOURCE, WUKONG_CAMERA_SOURCE),
                wait_raw_s=0,
                keep_raw=True,
            )

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["status"], "aligned")
            self.assertEqual(summary["source"], WUKONG_CAMERA_SOURCE.source)
            self.assertEqual(summary["frames_matched"], 2)
            output = [
                json.loads(line)
                for line in (session_dir / "camera.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(output), 2)
            self.assertEqual(
                set(output[0]),
                {
                    "t_unix_ms",
                    "pos",
                    "rot",
                    "fov",
                    "frame",
                    "t_capture_unix_ms",
                    "dt_ms",
                },
            )
            saved_meta = json.loads((session_dir / "meta.json").read_text())
            self.assertEqual(saved_meta["camera"]["schema"], "wukong_camera_v1")

    def test_two_sources_are_reported_as_conflict_and_raw_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary)
            meta = _base_meta(total_frames=1)
            sample = {"type": "sample", "t_unix_ms": 1_000, "fov": 70}
            for source in (GTA_CAMERA_SOURCE, WUKONG_CAMERA_SOURCE):
                _write_jsonl(
                    session_dir / source.raw_filename,
                    [{"type": "header", "schema": source.schema}, sample],
                )

            summary = finalize_session_cameras(
                session_dir,
                meta,
                (GTA_CAMERA_SOURCE, WUKONG_CAMERA_SOURCE),
                wait_raw_s=0,
            )

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["status"], "conflict")
            self.assertFalse((session_dir / "camera.jsonl").exists())
            self.assertTrue((session_dir / GTA_CAMERA_SOURCE.raw_filename).exists())
            self.assertTrue((session_dir / WUKONG_CAMERA_SOURCE.raw_filename).exists())

    def test_empty_second_source_does_not_block_valid_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary)
            meta = _base_meta(total_frames=1)
            _write_jsonl(
                session_dir / "frame_timestamps.jsonl",
                [{"frame": 0, "t_capture_unix_ms": 1_000.0}],
            )
            _write_jsonl(
                session_dir / GTA_CAMERA_SOURCE.raw_filename,
                [
                    {"type": "header", "schema": "gta_camera_v1"},
                    {"type": "sample", "t_unix_ms": 1_000, "fov": 70},
                ],
            )
            _write_jsonl(
                session_dir / WUKONG_CAMERA_SOURCE.raw_filename,
                [{"type": "header", "schema": "wukong_camera_v1"}],
            )

            summary = finalize_session_cameras(
                session_dir,
                meta,
                (GTA_CAMERA_SOURCE, WUKONG_CAMERA_SOURCE),
                wait_raw_s=0,
                keep_raw=True,
            )

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["status"], "aligned")
            self.assertEqual(summary["source"], GTA_CAMERA_SOURCE.source)

    def test_out_of_window_second_source_does_not_create_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary)
            meta = _base_meta(total_frames=1)
            _write_jsonl(
                session_dir / "frame_timestamps.jsonl",
                [{"frame": 0, "t_capture_unix_ms": 1_000.0}],
            )
            _write_jsonl(
                session_dir / GTA_CAMERA_SOURCE.raw_filename,
                [
                    {"type": "header", "schema": "gta_camera_v1"},
                    {"type": "sample", "t_unix_ms": 1_000, "fov": 70},
                ],
            )
            _write_jsonl(
                session_dir / WUKONG_CAMERA_SOURCE.raw_filename,
                [
                    {"type": "header", "schema": "wukong_camera_v1"},
                    {"type": "sample", "t_unix_ms": 5_000, "fov": 75},
                ],
            )

            summary = finalize_session_cameras(
                session_dir,
                meta,
                (GTA_CAMERA_SOURCE, WUKONG_CAMERA_SOURCE),
                wait_raw_s=0,
                keep_raw=True,
            )

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["status"], "aligned")
            self.assertEqual(summary["source"], GTA_CAMERA_SOURCE.source)
            self.assertEqual(
                summary["ignored_raw_files"],
                [WUKONG_CAMERA_SOURCE.raw_filename],
            )

    def test_zero_matches_preserves_raw_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary)
            meta = _base_meta(total_frames=1)
            _write_jsonl(
                session_dir / "frame_timestamps.jsonl",
                [{"frame": 0, "t_capture_unix_ms": 1_000.0}],
            )
            raw_path = session_dir / WUKONG_CAMERA_SOURCE.raw_filename
            _write_jsonl(
                raw_path,
                [
                    {"type": "header", "schema": "wukong_camera_v1"},
                    {"type": "sample", "t_unix_ms": 5_000, "fov": 70},
                ],
            )

            summary = finalize_session_cameras(
                session_dir,
                meta,
                (WUKONG_CAMERA_SOURCE,),
                wait_raw_s=0,
            )

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["status"], "alignment_failed")
            self.assertTrue(raw_path.exists())
            self.assertFalse((session_dir / "camera.jsonl").exists())

    def test_no_plugin_output_is_a_clean_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary)
            meta = _base_meta(total_frames=1)

            summary = finalize_session_cameras(
                session_dir,
                meta,
                (GTA_CAMERA_SOURCE, WUKONG_CAMERA_SOURCE),
                wait_raw_s=0,
            )

            self.assertIsNone(summary)
            self.assertFalse((session_dir / "camera.jsonl").exists())

    def test_legacy_gta_raw_filename_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary)
            meta = _base_meta(total_frames=1)
            _write_jsonl(
                session_dir / "frame_timestamps.jsonl",
                [{"frame": 0, "t_capture_unix_ms": 1_000.0}],
            )
            _write_jsonl(
                session_dir / "camera_raw.jsonl",
                [
                    {"type": "header", "schema": "gta_camera_v1"},
                    {"type": "sample", "t_unix_ms": 1_000, "fov": 70},
                ],
            )

            summary = finalize_session_cameras(
                session_dir,
                meta,
                (GTA_CAMERA_SOURCE,),
                wait_raw_s=0,
                keep_raw=True,
            )

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["raw_file"], "camera_raw.jsonl")


if __name__ == "__main__":
    unittest.main()
