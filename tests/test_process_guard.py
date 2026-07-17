from __future__ import annotations

import unittest

from game_recorder.process_guard import _replacement_powershell


class ProcessGuardTests(unittest.TestCase):
    def test_packaged_and_module_entrypoint_names_are_matched(self) -> None:
        command = _replacement_powershell(1234)

        self.assertIn("game[-_]recorder", command)
        self.assertIn("$excluded=@(1234)", command)
        self.assertIn("$excluded -notcontains $_.ProcessId", command)
        self.assertIn("$current.ParentProcessId", command)
        self.assertIn("pythonw.exe", command)
        self.assertIn("python.exe", command)


if __name__ == "__main__":
    unittest.main()
