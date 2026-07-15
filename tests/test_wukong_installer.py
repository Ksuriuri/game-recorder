from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import install_wukong_camera as installer
import uninstall_wukong_camera as uninstaller


class WukongInstallerTests(unittest.TestCase):
    def test_bundled_manifest_matches_payload(self) -> None:
        files, version = installer.load_and_verify_manifest()

        self.assertEqual(len(files), 6)
        self.assertTrue(version.startswith("wukong_camera_payload_manifest_v1:"))
        self.assertEqual(
            {item.destination_relative.as_posix() for item in files},
            {
                "dwmapi.dll",
                "ue4ss/UE4SS.dll",
                "ue4ss/UE4SS-settings.ini",
                "ue4ss/VTableLayout.ini",
                "ue4ss/Mods/mods.txt",
                "ue4ss/Mods/CameraFrameLogger/Scripts/main.lua",
            },
        )

    def test_resolves_game_root_and_win64_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game_root = Path(temporary) / "BlackMythWukong"
            win64 = game_root / "b1" / "Binaries" / "Win64"
            win64.mkdir(parents=True)
            (win64 / "b1-Win64-Shipping.exe").write_bytes(b"test")

            from_root = installer.resolve_game_layout(game_root)
            from_win64 = installer.resolve_game_layout(win64)

            self.assertIsNotNone(from_root)
            self.assertIsNotNone(from_win64)
            assert from_root is not None and from_win64 is not None
            self.assertEqual(from_root.root, from_win64.root)
            self.assertEqual(from_root.win64, from_win64.win64)

    def test_dynamic_config_and_idle_control_support_unicode_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            win64 = root / "游戏" / "b1" / "Binaries" / "Win64"
            win64.mkdir(parents=True)
            control = root / "录制 数据" / ".wukong_camera" / "active_session.json"

            config = installer.write_dynamic_config(win64, control)
            installer.seed_idle_control(control)

            config_text = config.read_text(encoding="utf-8")
            self.assertIn(control.resolve().as_posix(), config_text)
            self.assertEqual(
                json.loads(control.read_text(encoding="utf-8"))["status"],
                "idle",
            )

    def test_existing_mods_list_is_merged_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "mods.txt"
            source = root / "payload-mods.txt"
            destination.write_text("OtherMod : 1\nCameraFrameLogger : 0\n")
            source.write_text("CameraFrameLogger : 1\n")

            installer._merge_mods_txt(destination, source)

            merged = destination.read_text()
            self.assertIn("OtherMod : 1", merged)
            self.assertEqual(merged.count("CameraFrameLogger"), 1)
            self.assertIn("CameraFrameLogger : 1", merged)

    def test_uninstall_ownership_refuses_post_install_additions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game_root = Path(temporary) / "BlackMythWukong"
            win64 = game_root / "b1" / "Binaries" / "Win64"
            win64.mkdir(parents=True)
            exe = win64 / "b1-Win64-Shipping.exe"
            exe.write_bytes(b"test")
            layout = installer.GameLayout(
                game_root.resolve(),
                win64.resolve(),
                exe.resolve(),
            )

            managed = win64 / "ue4ss" / "Mods" / "mods.txt"
            managed.parent.mkdir(parents=True)
            managed.write_text("CameraFrameLogger : 1\n")
            proxy = win64 / "dwmapi.dll"
            proxy.write_bytes(b"proxy")
            config = (
                win64
                / "ue4ss"
                / "Mods"
                / "CameraFrameLogger"
                / "config.lua"
            )
            config.parent.mkdir(parents=True)
            config.write_text('return { control_file = "test" }\n')
            state = {
                "managed_files": ["dwmapi.dll", "ue4ss/Mods/mods.txt"],
                "managed_hashes": {
                    "dwmapi.dll": installer._sha256(proxy),
                    "ue4ss/Mods/mods.txt": installer._sha256(managed),
                },
                "dynamic_config": str(config),
                "dynamic_config_sha256": installer._sha256(config),
            }

            uninstaller.verify_installed_ownership(state, layout, None)
            added = win64 / "ue4ss" / "Mods" / "OtherMod" / "main.lua"
            added.parent.mkdir(parents=True)
            added.write_text("return true\n")

            with self.assertRaises(installer.InstallerError):
                uninstaller.verify_installed_ownership(state, layout, None)

    def test_upgrade_refuses_to_overwrite_modified_managed_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game_root = Path(temporary) / "BlackMythWukong"
            win64 = game_root / "b1" / "Binaries" / "Win64"
            win64.mkdir(parents=True)
            exe = win64 / "b1-Win64-Shipping.exe"
            exe.write_bytes(b"test")
            layout = installer.GameLayout(game_root, win64, exe)
            settings = win64 / "ue4ss" / "UE4SS-settings.ini"
            settings.parent.mkdir(parents=True)
            settings.write_text("user changed settings\n")
            config = (
                win64
                / "ue4ss"
                / "Mods"
                / "CameraFrameLogger"
                / "config.lua"
            )
            config.parent.mkdir(parents=True)
            config.write_text('return { control_file = "test" }\n')
            state = {
                "managed_hashes": {
                    "ue4ss/UE4SS-settings.ini": "0" * 64,
                },
                "dynamic_config": str(config.resolve()),
                "dynamic_config_sha256": installer._sha256(config),
            }

            with self.assertRaises(installer.InstallerError):
                installer.verify_upgrade_safe(state, layout)

    def test_schema_two_state_migrates_legacy_loader_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game_root = Path(temporary) / "BlackMythWukong"
            win64 = game_root / "b1" / "Binaries" / "Win64"
            win64.mkdir(parents=True)
            exe = win64 / "b1-Win64-Shipping.exe"
            exe.write_bytes(b"test")
            layout = installer.GameLayout(
                game_root.resolve(),
                win64.resolve(),
                exe.resolve(),
            )
            state_dir = game_root / installer.STATE_DIRNAME
            original_root = state_dir / "backups" / "original"
            original_root.mkdir(parents=True)
            config = (
                win64
                / "ue4ss"
                / "Mods"
                / "CameraFrameLogger"
                / "config.lua"
            )
            config.parent.mkdir(parents=True)
            config.write_text('return { control_file = "test" }\n')
            legacy = win64 / installer.LEGACY_LOADER_FILENAME
            legacy.write_bytes(b"legacy")
            state = {
                "state_schema": 2,
                "game_root": str(layout.root),
                "original_backup_path": str(original_root.resolve()),
                "had_original_dwmapi": False,
                "original_dwmapi_backup_path": None,
                "had_original_ue4ss": False,
                "original_ue4ss_backup_path": None,
                "managed_hashes": {},
                "dynamic_config": str(config.resolve()),
                "dynamic_config_sha256": installer._sha256(config),
                "control_file": str(
                    Path(temporary) / ".wukong_camera" / "active_session.json"
                ),
            }
            installer._write_json_atomic(
                state_dir / installer.STATE_FILENAME,
                state,
            )

            migrated = installer.migrate_state_v2(layout, state_dir, None)

            self.assertEqual(migrated["state_schema"], installer.STATE_SCHEMA)
            self.assertTrue(migrated["had_original_xinput"])
            self.assertTrue(migrated["pending_legacy_xinput_removal"])
            backup = Path(migrated["original_xinput_backup_path"])
            self.assertEqual(backup.read_bytes(), b"legacy")
            installer.verify_upgrade_safe(migrated, layout)

    def test_interrupted_transaction_restores_prechange_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game_root = Path(temporary) / "BlackMythWukong"
            win64 = game_root / "b1" / "Binaries" / "Win64"
            win64.mkdir(parents=True)
            exe = win64 / "b1-Win64-Shipping.exe"
            exe.write_bytes(b"test")
            layout = installer.GameLayout(game_root, win64, exe)
            live_proxy = win64 / "dwmapi.dll"
            live_proxy.write_bytes(b"original")
            state_dir = game_root / installer.STATE_DIRNAME
            snapshot = installer.ChangeSnapshot(layout, state_dir, None)
            live_proxy.write_bytes(b"partial")

            recovered = installer.recover_interrupted_transaction(
                layout,
                state_dir,
                None,
            )

            self.assertTrue(recovered)
            self.assertEqual((win64 / "dwmapi.dll").read_bytes(), b"original")
            self.assertFalse(snapshot.transaction_dir.exists())

    def test_incomplete_snapshot_never_replaces_live_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game_root = Path(temporary) / "BlackMythWukong"
            win64 = game_root / "b1" / "Binaries" / "Win64"
            win64.mkdir(parents=True)
            exe = win64 / "b1-Win64-Shipping.exe"
            exe.write_bytes(b"test")
            layout = installer.GameLayout(game_root, win64, exe)
            live_proxy = win64 / "dwmapi.dll"
            live_proxy.write_bytes(b"live complete file")
            transaction = game_root / (
                installer.STATE_DIRNAME + ".transaction-incomplete"
            )
            transaction.mkdir()
            (transaction / "dwmapi.dll").write_bytes(b"partial snapshot")

            installer.recover_interrupted_transaction(
                layout,
                game_root / installer.STATE_DIRNAME,
                None,
            )

            self.assertEqual(live_proxy.read_bytes(), b"live complete file")
            self.assertFalse(transaction.exists())

    def test_corrupt_prepared_snapshot_never_replaces_live_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game_root = Path(temporary) / "BlackMythWukong"
            win64 = game_root / "b1" / "Binaries" / "Win64"
            win64.mkdir(parents=True)
            exe = win64 / "b1-Win64-Shipping.exe"
            exe.write_bytes(b"test")
            layout = installer.GameLayout(game_root, win64, exe)
            live_proxy = win64 / "dwmapi.dll"
            live_proxy.write_bytes(b"live complete file")
            state_dir = game_root / installer.STATE_DIRNAME
            snapshot = installer.ChangeSnapshot(layout, state_dir, None)
            (snapshot.transaction_dir / "dwmapi.dll").write_bytes(b"corrupt")

            with self.assertRaises(installer.InstallerError):
                installer.recover_interrupted_transaction(
                    layout,
                    state_dir,
                    None,
                )

            self.assertEqual(live_proxy.read_bytes(), b"live complete file")
            self.assertTrue(snapshot.transaction_dir.exists())

    def test_install_then_uninstall_restores_original_ue4ss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = root / "BlackMythWukong"
            win64 = game_root / "b1" / "Binaries" / "Win64"
            win64.mkdir(parents=True)
            exe = win64 / "b1-Win64-Shipping.exe"
            exe.write_bytes(b"test")
            layout = installer.GameLayout(
                game_root.resolve(),
                win64.resolve(),
                exe.resolve(),
            )
            original_proxy = win64 / "dwmapi.dll"
            original_proxy.write_bytes(b"original proxy")
            original_xinput = win64 / installer.LEGACY_LOADER_FILENAME
            original_xinput.write_bytes(b"legacy loader")
            original_mods = win64 / "ue4ss" / "Mods" / "mods.txt"
            original_mods.parent.mkdir(parents=True)
            original_mods.write_text("OtherMod : 0\n")

            payload_root = root / "payload"
            payload_proxy = payload_root / "dwmapi.dll"
            payload_mods = payload_root / "ue4ss" / "Mods" / "mods.txt"
            payload_main = (
                payload_root
                / "ue4ss"
                / "Mods"
                / "CameraFrameLogger"
                / "Scripts"
                / "main.lua"
            )
            payload_proxy.parent.mkdir(parents=True)
            payload_proxy.write_bytes(b"new proxy")
            payload_mods.parent.mkdir(parents=True)
            payload_mods.write_text("CameraFrameLogger : 1\n")
            payload_main.parent.mkdir(parents=True)
            payload_main.write_text("return true\n")

            payload_files = []
            for source, relative in (
                (payload_proxy, Path("dwmapi.dll")),
                (payload_mods, Path("ue4ss/Mods/mods.txt")),
                (
                    payload_main,
                    Path("ue4ss/Mods/CameraFrameLogger/Scripts/main.lua"),
                ),
            ):
                payload_files.append(
                    installer.PayloadFile(
                        manifest_path="payload/" + relative.as_posix(),
                        source=source,
                        destination_relative=relative,
                        byte_count=source.stat().st_size,
                        sha256=installer._sha256(source),
                    )
                )

            installer.install(
                layout,
                root / "project" / "recordings",
                payload_files,
                "test-payload",
            )
            self.assertIn("OtherMod : 0", original_mods.read_text())
            self.assertIn("CameraFrameLogger : 1", original_mods.read_text())
            self.assertFalse(original_xinput.exists())
            original_mods.write_text(
                "OtherMod : 1\nCameraFrameLogger : 1\n",
                encoding="utf-8",
            )
            installer.install(
                layout,
                root / "project" / "recordings",
                payload_files,
                "test-payload-upgrade",
            )
            original_mods.write_text(
                "OtherMod : 2\nCameraFrameLogger : 1\n",
                encoding="utf-8",
            )

            uninstaller.uninstall(layout)

            self.assertEqual(original_proxy.read_bytes(), b"original proxy")
            self.assertEqual(original_xinput.read_bytes(), b"legacy loader")
            self.assertEqual(original_mods.read_text(), "OtherMod : 2\n")
            self.assertFalse(
                (
                    win64
                    / "ue4ss"
                    / "Mods"
                    / "CameraFrameLogger"
                    / "Scripts"
                    / "main.lua"
                ).exists()
            )

    def test_lua_payload_contains_required_basic_schema_contract(self) -> None:
        lua_path = (
            PROJECT_ROOT
            / "wukong-camera"
            / "payload"
            / "ue4ss"
            / "Mods"
            / "CameraFrameLogger"
            / "Scripts"
            / "main.lua"
        )
        source = lua_path.read_text(encoding="utf-8")

        self.assertIn('"wukong_camera_v1"', source)
        self.assertIn('"game_world_meters_deg"', source)
        self.assertIn('"pitch_roll_yaw_deg"', source)
        self.assertIn("camera.x / 100.0", source)
        self.assertIn("camera.pitch", source)
        self.assertIn("camera.roll", source)
        self.assertIn("camera.yaw", source)
        self.assertNotIn("view_projection", source)
        self.assertNotIn("camera_frames.csv", source)


if __name__ == "__main__":
    unittest.main()
