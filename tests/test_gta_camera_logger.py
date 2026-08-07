from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGGER_PATH = PROJECT_ROOT / "gta-camera" / "CameraPoseLogger" / "CameraPoseLogger.cs"
ASI_LOGGER_PATH = PROJECT_ROOT / "gta-camera" / "AsiCameraPoseLogger" / "main.cpp"


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

    def test_asi_applies_recording_movement_speed_override_each_tick(self) -> None:
        source = ASI_LOGGER_PATH.read_text(encoding="utf-8")
        self.assertIn("movement_speed_scale", source)
        self.assertIn("0xD80958FC74E988A6ULL", source)
        self.assertIn("0x085BF80FA50A39D1ULL", source)
        self.assertIn("0x433083750C5E064AULL", source)
        self.assertIn("ApplyMovementSpeedScale(movement_speed_scale_)", source)

    def test_asi_yields_until_enhanced_game_window_exists(self) -> None:
        source = ASI_LOGGER_PATH.read_text(encoding="utf-8")
        script_main = source[source.index("void ScriptMain()") :]
        self.assertLess(script_main.index("FindGameWindow()"), script_main.index("LoadConfig()"))
        self.assertLess(script_main.index("WAIT(100)"), script_main.index("LoadConfig()"))
        self.assertIn("GetTickCount64() + 30000", script_main)


if __name__ == "__main__":
    unittest.main()
