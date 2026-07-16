from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGGER_PATH = PROJECT_ROOT / "gta-camera" / "CameraPoseLogger" / "CameraPoseLogger.cs"


class GtaCameraLoggerTests(unittest.TestCase):
    def test_logger_records_v2_c2w_without_redundant_pose_fields(self) -> None:
        source = LOGGER_PATH.read_text(encoding="utf-8")

        self.assertIn("gta_camera_v2", source)
        self.assertIn('GameplayCamera.Matrix', source)
        self.assertIn('GameplayCamera.IsRendering', source)
        self.assertIn('GTA.UI.Screen.Resolution', source)
        self.assertIn("camera_to_world", source)
        self.assertIn("fov_vertical_deg", source)
        self.assertIn("viewport_px", source)
        self.assertNotIn('AppendVec3(', source)
        self.assertNotIn('"player_pos"', source)
        self.assertNotIn('"player_heading"', source)


if __name__ == "__main__":
    unittest.main()
