from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = PROJECT_ROOT / "scripts" / "install_rtss.py"
SPEC = importlib.util.spec_from_file_location("install_rtss", INSTALLER_PATH)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class ExeNameTests(unittest.TestCase):
    def test_accepts_bare_name_and_full_path(self) -> None:
        self.assertEqual(installer.normalize_exe_name("RDR2.exe"), "RDR2.exe")
        self.assertEqual(
            installer.normalize_exe_name(r'"Z:\RDR2\RDR2.exe"'), "RDR2.exe"
        )
        self.assertEqual(installer.normalize_exe_name("rdr2"), "rdr2.exe")

    def test_rejects_empty_and_traversal(self) -> None:
        for value in ("", "   ", ".."):
            with self.assertRaises(installer.InstallerError):
                installer.normalize_exe_name(value)


class SignerSubjectTests(unittest.TestCase):
    def test_common_name_survives_commas_inside_quotes(self) -> None:
        subject = (
            'CN="MICRO-STAR INTERNATIONAL CO., LTD.", '
            'O="MICRO-STAR INTERNATIONAL CO., LTD.", L=New Taipei, C=TW'
        )
        self.assertEqual(
            installer._common_name(subject), "MICRO-STAR INTERNATIONAL CO., LTD."
        )

    def test_common_name_without_quotes(self) -> None:
        self.assertEqual(installer._common_name("CN=Example Corp, C=US"), "Example Corp")


class ProfileMergeTests(unittest.TestCase):
    def test_creates_sections_when_profile_is_new(self) -> None:
        text = installer.merge_profile(
            "", installer.profile_overrides(60, osd=False)
        )
        self.assertIn("[Framerate]", text)
        self.assertRegex(text, r"Limit\s+= 60")
        self.assertRegex(text, r"EnableOSD\s+= 0")
        self.assertTrue(text.endswith("\r\n"))

    def test_replaces_existing_key_in_place_and_keeps_the_rest(self) -> None:
        original = (
            "[Framerate]\r\n"
            "Limit\t\t= 0\r\n"
            "SyncLimiter\t\t= 1\r\n"
            "\r\n"
            "[Hooking]\r\n"
            "IgnoreDXGIInterop\t= 2\r\n"
        )
        text = installer.merge_profile(
            original, installer.profile_overrides(30, osd=True)
        )
        self.assertRegex(text, r"Limit\s+= 30")
        self.assertNotRegex(text, r"^Limit\s+= 0$")
        self.assertRegex(text, r"SyncLimiter\s+= 1")
        self.assertRegex(text, r"IgnoreDXGIInterop\s+= 2")
        self.assertRegex(text, r"EnableOSD\s+= 1")
        self.assertEqual(text.count("[Framerate]"), 1)

    def test_is_idempotent(self) -> None:
        overrides = installer.profile_overrides(120, osd=False)
        once = installer.merge_profile("", overrides)
        twice = installer.merge_profile(once, overrides)
        self.assertEqual(once, twice)

    def test_rejects_out_of_range_limit(self) -> None:
        with self.assertRaises(installer.InstallerError):
            installer.profile_overrides(-1, osd=False)
        with self.assertRaises(installer.InstallerError):
            installer.profile_overrides(installer.MAX_FPS + 1, osd=False)


class WriteProfileTests(unittest.TestCase):
    def test_writes_named_profile_without_bom_and_updates_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir)
            profile = installer.write_profile(
                install_dir, "RDR2.exe", 60, osd=False
            )
            self.assertEqual(profile.name, "RDR2.exe.cfg")
            self.assertEqual(profile.parent.name, installer.PROFILES_DIRNAME)
            raw = profile.read_bytes()
            self.assertTrue(raw.startswith(b"["))
            self.assertRegex(raw.decode("ascii"), r"Limit\s+= 60")

            installer.write_profile(install_dir, "RDR2.exe", 0, osd=False)
            self.assertRegex(profile.read_text(encoding="ascii"), r"Limit\s+= 0")

    def test_preserves_unrelated_user_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir)
            profile = install_dir / installer.PROFILES_DIRNAME / "RDR2.exe.cfg"
            profile.parent.mkdir(parents=True)
            profile.write_text(
                "[Hooking]\r\nIgnoreDXGIInterop\t= 2\r\n", encoding="ascii"
            )
            installer.write_profile(install_dir, "RDR2.exe", 144, osd=False)
            text = profile.read_text(encoding="ascii")
            self.assertRegex(text, r"IgnoreDXGIInterop\s+= 2")
            self.assertRegex(text, r"Limit\s+= 144")


if __name__ == "__main__":
    unittest.main()
