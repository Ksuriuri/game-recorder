from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGGER_PATH = (
    PROJECT_ROOT
    / "cp2077-camera"
    / "payload"
    / "cyber_engine_tweaks"
    / "mods"
    / "CameraFrameLogger"
    / "init.lua"
)


class Cp2077CameraLoggerTests(unittest.TestCase):
    def test_logger_uses_cet_sandbox_and_seconds_delta(self) -> None:
        source = LOGGER_PATH.read_text(encoding="utf-8")

        self.assertIn('local control_file = "active_session.json"', source)
        self.assertIn("local CONTROL_POLL_SECONDS = 0.01", source)
        self.assertIn("sample_period_seconds = 1.0 / control.sample_hz", source)
        self.assertIn("session.anchor_unix_ms + session.elapsed_seconds * 1000.0", source)
        self.assertIn(
            '"clock":"recorder_publish_unix_plus_game_delta_seconds"',
            source,
        )
        self.assertNotIn("debug.getinfo", source)
        self.assertNotIn("CONTROL_POLL_MS", source)
        self.assertNotIn("join_path(control.session_dir", source)


if __name__ == "__main__":
    unittest.main()
