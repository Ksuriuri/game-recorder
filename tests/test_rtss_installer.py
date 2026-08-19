from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


class GameDiscoveryTests(unittest.TestCase):
    def _isolate(self, scanned: list[Path] | None = None) -> list[mock._patch]:
        """Silence the real registry / Steam / drive lookups."""
        return [
            mock.patch.object(installer, "_rdr2_camera_installer", return_value=None),
            mock.patch.object(installer, "_registry_install_locations", return_value=[]),
            mock.patch.object(
                installer, "_shallow_scan_dirs", return_value=scanned or []
            ),
        ]

    def test_env_override_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            game = Path(temp_dir)
            (game / "RDR2.exe").write_bytes(b"MZ")
            with mock.patch.dict(installer.os.environ, {"RTSS_GAME_DIR": str(game)}):
                for patcher in self._isolate():
                    patcher.start()
                    self.addCleanup(patcher.stop)
                self.assertEqual(installer.find_game_dirs("RDR2.exe"), [game.resolve()])

    def test_repack_install_found_by_shallow_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            game = Path(temp_dir) / "RDR2"
            game.mkdir()
            (game / "RDR2.exe").write_bytes(b"MZ")
            with mock.patch.dict(installer.os.environ, {"RTSS_GAME_DIR": ""}):
                for patcher in self._isolate(scanned=[game]):
                    patcher.start()
                    self.addCleanup(patcher.stop)
                self.assertEqual(installer.find_game_dirs("RDR2.exe"), [game.resolve()])

    def test_missing_game_resolves_to_none_without_prompting(self) -> None:
        with mock.patch.dict(installer.os.environ, {"RTSS_GAME_DIR": ""}):
            for patcher in self._isolate():
                patcher.start()
                self.addCleanup(patcher.stop)
            self.assertIsNone(
                installer.resolve_game_dir("RDR2.exe", None, prompt=False)
            )

    def test_explicit_dir_without_the_exe_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(installer.InstallerError):
                installer.resolve_game_dir(
                    "RDR2.exe", Path(temp_dir), prompt=False
                )

    def test_explicit_dir_wins_without_any_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            game = Path(temp_dir)
            (game / "RDR2.exe").write_bytes(b"MZ")
            with mock.patch.object(installer, "find_game_dirs") as lookup:
                resolved = installer.resolve_game_dir("RDR2.exe", game, prompt=False)
            self.assertEqual(resolved, game.resolve())
            lookup.assert_not_called()


class ChildArgvTests(unittest.TestCase):
    def test_elevated_child_never_prompts_or_redetects(self) -> None:
        args = installer.build_parser().parse_args(["--osd", "--offline"])
        argv = installer.child_argv(
            args, fps=45, games=["RDR2.exe"], game_dirs=[Path(r"Z:\RDR2")]
        )
        self.assertIn("--no-prompt", argv)
        self.assertIn("--skip-game-check", argv)
        self.assertEqual(argv[argv.index("--fps") + 1], "45")
        self.assertEqual(argv[argv.index("--game-exe") + 1], "RDR2.exe")
        self.assertEqual(argv[argv.index("--game-dir") + 1], r"Z:\RDR2")
        self.assertIn("--osd", argv)
        self.assertIn("--offline", argv)

    def test_flags_left_off_are_not_forwarded(self) -> None:
        args = installer.build_parser().parse_args([])
        argv = installer.child_argv(args, fps=60, games=["RDR2.exe"], game_dirs=[])
        for flag in ("--osd", "--no-start", "--offline", "--allow-unsigned"):
            self.assertNotIn(flag, argv)


class ResolveArchiveTests(unittest.TestCase):
    def test_offline_without_cache_points_at_the_bundle_builder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(installer, "DOWNLOAD_CACHE", Path(temp_dir)):
                with self.assertRaises(installer.InstallerError) as caught:
                    installer.resolve_archive(
                        None, allow_unknown=False, offline=True
                    )
        self.assertIn("build_offline_bundle", str(caught.exception))

    def test_cached_archive_is_used_offline_and_reported_as_pinned(self) -> None:
        payload = b"pretend RTSS installer"
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir)
            archive = cache / installer.RTSS_ARCHIVE_NAME
            archive.write_bytes(payload)
            with mock.patch.object(installer, "DOWNLOAD_CACHE", cache), mock.patch.object(
                installer, "RTSS_ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest()
            ):
                path, pinned = installer.resolve_archive(
                    None, allow_unknown=False, offline=True
                )
            self.assertEqual(path, archive)
            self.assertTrue(pinned)

    def test_stale_cache_is_discarded_instead_of_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir)
            archive = cache / installer.RTSS_ARCHIVE_NAME
            archive.write_bytes(b"truncated download")
            with mock.patch.object(installer, "DOWNLOAD_CACHE", cache):
                with self.assertRaises(installer.InstallerError):
                    installer.resolve_archive(
                        None, allow_unknown=False, offline=True
                    )
            self.assertFalse(archive.exists())

    def test_explicit_archive_with_unknown_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "somebody-elses-rtss.zip"
            archive.write_bytes(b"not the official build")
            with self.assertRaises(installer.InstallerError):
                installer.resolve_archive(archive, allow_unknown=False)
            path, pinned = installer.resolve_archive(archive, allow_unknown=True)
            self.assertEqual(path, archive.resolve())
            self.assertFalse(pinned)


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
