from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = PROJECT_ROOT / "scripts" / "install_gta_camera.py"


def _load_installer():
    module_name = "_game_recorder_test_install_gta_camera"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, INSTALLER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载安装器：{INSTALLER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


installer = _load_installer()


class GtaInstallerPathDiscoveryTests(unittest.TestCase):
    def test_module_is_loaded_dynamically_from_script(self) -> None:
        self.assertEqual(Path(installer.__file__).resolve(), INSTALLER_PATH.resolve())

    def test_is_gta_dir_accepts_classic_and_enhanced_exes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            classic = root / "classic"
            enhanced = root / "enhanced"
            classic.mkdir()
            enhanced.mkdir()
            (classic / "GTA5.exe").write_bytes(b"x")
            (enhanced / "GTA5_Enhanced.exe").write_bytes(b"x")
            self.assertTrue(installer.is_gta_dir(classic))
            self.assertTrue(installer.is_gta_dir(enhanced))
            self.assertFalse(installer.is_gta_dir(root))
            self.assertFalse(installer.is_enhanced_gta(classic))
            self.assertTrue(installer.is_enhanced_gta(enhanced))

    def test_install_scripthookv_uses_xinput_on_enhanced_not_dinput8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gta = root / "enhanced"
            vendor = root / "vendor"
            gta.mkdir()
            vendor.mkdir()
            (gta / "GTA5_Enhanced.exe").write_bytes(b"game")
            (gta / "xinput1_4.dll").write_bytes(b"enhanced-loader")
            (gta / "dinput8.dll").write_bytes(b"stray-classic-loader")
            (vendor / "ScriptHookV.dll").write_bytes(b"shv")
            (vendor / "dinput8.dll").write_bytes(b"classic-loader")
            (vendor / "VERSION.txt").write_text("test\n", encoding="utf-8")
            with mock.patch.object(installer, "VENDORED_SHV", vendor):
                installer.install_scripthookv_from_vendor(gta)
            self.assertEqual((gta / "ScriptHookV.dll").read_bytes(), b"shv")
            self.assertFalse((gta / "dinput8.dll").exists())
            self.assertEqual((gta / "xinput1_4.dll").read_bytes(), b"enhanced-loader")
            self.assertTrue(installer.scripthookv_installed(gta))

    def test_install_scripthookv_requires_enhanced_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gta = root / "enhanced"
            vendor = root / "vendor"
            gta.mkdir()
            vendor.mkdir()
            (gta / "GTA5_Enhanced.exe").write_bytes(b"game")
            (vendor / "ScriptHookV.dll").write_bytes(b"shv")
            (vendor / "dinput8.dll").write_bytes(b"classic-loader")
            with mock.patch.object(installer, "VENDORED_SHV", vendor):
                with self.assertRaisesRegex(FileNotFoundError, "Enhanced"):
                    installer.install_scripthookv_from_vendor(gta)

    def test_write_config_puts_file_in_game_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gta = root / "gta"
            recordings = root / "recordings"
            gta.mkdir()
            recordings.mkdir()
            path = installer.write_config(gta, recordings)
            self.assertEqual(path, gta / "camera_pose_logger.config.json")
            self.assertTrue(path.is_file())
            text = path.read_text(encoding="utf-8")
            self.assertIn("control_file", text)
            self.assertTrue((gta / "scripts" / "camera_pose_logger.config.json").is_file())

    def test_install_scripthookv_copies_dinput8_on_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gta = root / "legacy"
            vendor = root / "vendor"
            gta.mkdir()
            vendor.mkdir()
            (gta / "GTA5.exe").write_bytes(b"game")
            (vendor / "ScriptHookV.dll").write_bytes(b"shv")
            (vendor / "dinput8.dll").write_bytes(b"classic-loader")
            with mock.patch.object(installer, "VENDORED_SHV", vendor):
                installer.install_scripthookv_from_vendor(gta)
            self.assertEqual((gta / "dinput8.dll").read_bytes(), b"classic-loader")
            self.assertTrue(installer.scripthookv_installed(gta))

    def test_find_gta_candidates_reads_registry_steam_and_vdf_libraries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            steam = root / "steam"
            library = root / "library"
            game = library / "steamapps" / "common" / "Grand Theft Auto V"
            game.mkdir(parents=True)
            (game / "GTA5.exe").write_bytes(b"x")
            (steam / "steamapps").mkdir(parents=True)
            (steam / "steamapps" / "libraryfolders.vdf").write_text(
                ' "path"\t\t"' + str(library).replace("\\", "\\\\") + '"\n',
                encoding="utf-8",
            )
            (library / "steamapps" / f"appmanifest_{installer.GTA_STEAM_APP_ID}.acf").write_text(
                '"AppState"\n{\n\t"installdir"\t\t"Grand Theft Auto V"\n}\n',
                encoding="utf-8",
            )

            with mock.patch.object(installer, "_steam_roots", return_value=[steam]):
                with mock.patch.object(installer, "_registered_gta_locations", return_value=[]):
                    with mock.patch.object(installer, "_windows_drive_roots", return_value=[]):
                        with mock.patch.dict(os.environ, {}, clear=False):
                            os.environ.pop("GTAV_DIR", None)
                            found = [
                                path
                                for path in installer.find_gta_candidates()
                                if installer.is_gta_dir(path)
                            ]

            self.assertEqual(found, [game.resolve()])

    def test_find_gta_candidates_honors_gtav_dir_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = Path(temporary) / "Grand Theft Auto V"
            game.mkdir()
            (game / "PlayGTAV.exe").write_bytes(b"x")
            with mock.patch.object(installer, "_steam_roots", return_value=[]):
                with mock.patch.object(installer, "_registered_gta_locations", return_value=[]):
                    with mock.patch.object(installer, "_windows_drive_roots", return_value=[]):
                        with mock.patch.dict(os.environ, {"GTAV_DIR": str(game)}):
                            found = [
                                path
                                for path in installer.find_gta_candidates()
                                if installer.is_gta_dir(path)
                            ]
            self.assertEqual(found, [game.resolve()])

    def test_resolve_gta_dirs_autopicks_all_candidates_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "a"
            second = root / "b"
            for path in (first, second):
                path.mkdir()
                (path / "GTA5.exe").write_bytes(b"x")
            with mock.patch.object(
                installer,
                "find_gta_candidates",
                return_value=[first, second],
            ):
                chosen, skipped = installer.resolve_gta_dirs(None, prompt=False)
            self.assertFalse(skipped)
            self.assertEqual(chosen, [first.resolve(), second.resolve()])

    def test_resolve_gta_dirs_asks_legacy_and_enhanced_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy"
            enhanced = root / "enhanced"
            legacy.mkdir()
            enhanced.mkdir()
            (legacy / "GTA5.exe").write_bytes(b"x")
            (enhanced / "GTA5_Enhanced.exe").write_bytes(b"x")
            answers = iter(["1", "1"])
            with mock.patch.object(
                installer,
                "find_gta_candidates",
                return_value=[legacy, enhanced],
            ), mock.patch("builtins.input", side_effect=lambda *_a, **_k: next(answers)):
                chosen, skipped = installer.resolve_gta_dirs(None, prompt=True)
            self.assertFalse(skipped)
            self.assertEqual(chosen, [legacy.resolve(), enhanced.resolve()])

    def test_resolve_gta_dirs_can_skip_both_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = Path(temporary) / "steam"
            game.mkdir()
            (game / "GTA5.exe").write_bytes(b"x")
            with mock.patch.object(
                installer,
                "find_gta_candidates",
                return_value=[game],
            ), mock.patch("builtins.input", return_value=""):
                chosen, skipped = installer.resolve_gta_dirs(None, prompt=True)
            self.assertTrue(skipped)
            self.assertEqual(chosen, [])


if __name__ == "__main__":
    unittest.main()
