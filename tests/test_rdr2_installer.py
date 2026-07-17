from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = PROJECT_ROOT / "scripts" / "install_rdr2_camera.py"


def _load_installer():
    module_name = "_game_recorder_test_install_rdr2_camera"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, INSTALLER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载安装器：{INSTALLER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


installer = _load_installer()


def _pe(machine: int = 0x8664, optional_magic: int = 0x20B) -> bytes:
    data = bytearray(0x200)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, machine)
    struct.pack_into("<H", data, 0x98, optional_magic)
    return bytes(data)


def _write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _runtime_zip(path: Path) -> Path:
    return _write_zip(
        path,
        {
            "bin/ScriptHookRDR2.dll": _pe(),
            "bin/dinput8.dll": _pe(),
        },
    )


class Rdr2InstallerTests(unittest.TestCase):
    def test_module_is_loaded_dynamically_from_script(self) -> None:
        self.assertEqual(Path(installer.__file__).resolve(), INSTALLER_PATH.resolve())

    def test_validate_zip_rejects_zip_slip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = _write_zip(
                Path(temporary) / "unsafe.zip",
                {
                    "../escaped.txt": b"bad",
                    "inc/main.h": b"",
                    "inc/natives.h": b"",
                    "lib/ScriptHookRDR2.lib": b"",
                },
            )
            with self.assertRaisesRegex(installer.InstallerError, "不安全路径"):
                installer.validate_zip(archive, installer.SDK_REQUIRED)

    def test_validate_zip_requires_all_expected_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = _write_zip(
                Path(temporary) / "incomplete.zip",
                {"bin/ScriptHookRDR2.dll": _pe()},
            )
            with self.assertRaisesRegex(installer.InstallerError, "dinput8.dll"):
                installer.validate_zip(archive, installer.RUNTIME_REQUIRED)

    def test_validate_zip_enforces_resource_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entries = _write_zip(
                root / "entries.zip",
                {"one": b"1", "two": b"2"},
            )
            with mock.patch.object(installer, "MAX_ZIP_ENTRIES", 1):
                with self.assertRaisesRegex(installer.InstallerError, "条目过多"):
                    installer.validate_zip(entries, ())

            large_member = _write_zip(root / "member.zip", {"large": b"12"})
            with mock.patch.object(installer, "MAX_ZIP_MEMBER_BYTES", 1):
                with self.assertRaisesRegex(installer.InstallerError, "条目过大"):
                    installer.validate_zip(large_member, ())

            excessive_total = _write_zip(
                root / "total.zip",
                {"one": b"12", "two": b"34"},
            )
            with (
                mock.patch.object(installer, "MAX_ZIP_MEMBER_BYTES", 10),
                mock.patch.object(installer, "MAX_ZIP_TOTAL_BYTES", 3),
            ):
                with self.assertRaisesRegex(installer.InstallerError, "总大小"):
                    installer.validate_zip(excessive_total, ())

            compression_bomb = root / "ratio.zip"
            with zipfile.ZipFile(
                compression_bomb, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr("compressed", b"A" * 4096)
            with mock.patch.object(installer, "MAX_ZIP_COMPRESSION_RATIO", 2):
                with self.assertRaisesRegex(installer.InstallerError, "压缩比异常"):
                    installer.validate_zip(compression_bomb, ())

    def test_read_archive_snapshot_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "oversized.zip"
            archive.write_bytes(b"12345")
            with mock.patch.object(installer, "MAX_ARCHIVE_BYTES", 4):
                with self.assertRaisesRegex(installer.InstallerError, "ZIP 文件过大"):
                    installer.read_archive_snapshot(archive)

    def test_read_member_uses_validated_snapshot_after_source_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = _runtime_zip(Path(temporary) / "runtime.zip")
            payload = installer.validate_zip(archive, installer.RUNTIME_REQUIRED)
            expected = installer.read_zip_member(
                payload, "bin/ScriptHookRDR2.dll"
            )
            _write_zip(
                archive,
                {
                    "bin/ScriptHookRDR2.dll": b"replacement",
                    "bin/dinput8.dll": b"replacement",
                },
            )

            self.assertEqual(
                installer.read_zip_member(payload, "bin/ScriptHookRDR2.dll"),
                expected,
            )

    def test_extract_uses_validated_snapshot_after_source_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = {
                "inc/main.h": b"original main",
                "inc/natives.h": b"original natives",
                "lib/ScriptHookRDR2.lib": b"original library",
            }
            archive = _write_zip(root / "sdk.zip", original)
            payload = installer.validate_zip(archive, installer.SDK_REQUIRED)
            archive.write_bytes(b"source ZIP replaced after validation")
            destination = root / "sdk"

            installer.extract_zip_safely(payload, destination)

            for name, expected in original.items():
                self.assertEqual((destination / name).read_bytes(), expected)

    def test_known_archive_sha_is_accepted_unattended(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "Known.zip"
            archive.write_bytes(b"official fixture")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with mock.patch.dict(
                installer.KNOWN_ARCHIVE_SHA256,
                {archive.name.casefold(): digest},
                clear=True,
            ):
                self.assertEqual(
                    installer.verify_archive_trust(
                        archive, prompt=False, allow_unknown=False
                    ),
                    digest,
                )

    def test_known_archive_with_wrong_sha_is_always_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "Known.zip"
            archive.write_bytes(b"tampered")
            with mock.patch.dict(
                installer.KNOWN_ARCHIVE_SHA256,
                {archive.name.casefold(): "0" * 64},
                clear=True,
            ):
                with self.assertRaisesRegex(
                    installer.InstallerError, "已知官方版本不符"
                ):
                    installer.verify_archive_trust(
                        archive, prompt=False, allow_unknown=True
                    )

    def test_unknown_archive_unattended_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "NewVersion.zip"
            archive.write_bytes(b"new official fixture")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with mock.patch.dict(installer.KNOWN_ARCHIVE_SHA256, {}, clear=True):
                with self.assertRaisesRegex(
                    installer.InstallerError, "无人值守模式"
                ):
                    installer.verify_archive_trust(
                        archive, prompt=False, allow_unknown=False
                    )
                self.assertEqual(
                    installer.verify_archive_trust(
                        archive, prompt=False, allow_unknown=True
                    ),
                    digest,
                )

    def test_validate_pe_accepts_pe32_plus_x64_and_rejects_x86(self) -> None:
        installer.validate_pe_x64(data=_pe(), label="x64 fixture")
        with self.assertRaisesRegex(installer.InstallerError, "不是 x64 PE"):
            installer.validate_pe_x64(
                data=_pe(machine=0x14C, optional_magic=0x10B),
                label="x86 fixture",
            )

    def test_runtime_zip_auto_selection_excludes_sdk_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old_runtime = _runtime_zip(directory / "ScriptHookRDR2_1.0.zip")
            new_runtime = _runtime_zip(directory / "ScriptHookRDR2_2.0.zip")
            sdk = _write_zip(
                directory / "ScriptHookRDR2_SDK_99.zip",
                {
                    "inc/main.h": b"",
                    "inc/natives.h": b"",
                    "lib/ScriptHookRDR2.lib": b"",
                },
            )
            os.utime(old_runtime, (1, 1))
            os.utime(new_runtime, (2, 2))
            os.utime(sdk, (3, 3))
            with mock.patch.object(installer, "_zip_search_dirs", return_value=[directory]):
                selected = installer.resolve_zip(
                    None,
                    pattern=installer.RUNTIME_GLOB,
                    label="runtime",
                    prompt=False,
                )
            self.assertEqual(selected, new_runtime.resolve())

    def test_resolve_runtime_prefers_vendor_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vendor = Path(temporary) / "vendor"
            vendor.mkdir()
            (vendor / "ScriptHookRDR2.dll").write_bytes(_pe())
            (vendor / "dinput8.dll").write_bytes(_pe())
            (vendor / "VERSION.txt").write_text(
                "ScriptHookRDR2_test\n", encoding="utf-8"
            )
            with mock.patch.object(installer, "VENDORED_RUNTIME", vendor):
                runtime = installer.resolve_runtime(
                    None, prompt=False, allow_unknown=True
                )
            self.assertEqual(runtime.source, vendor.resolve())
            self.assertEqual(set(runtime.files), {"ScriptHookRDR2.dll", "dinput8.dll"})

    def test_resolve_runtime_accepts_extracted_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extracted = Path(temporary) / "ScriptHookRDR2_1.0.1491.17"
            (extracted / "bin").mkdir(parents=True)
            (extracted / "bin" / "ScriptHookRDR2.dll").write_bytes(_pe())
            (extracted / "bin" / "dinput8.dll").write_bytes(_pe())
            runtime = installer.resolve_runtime(
                extracted, prompt=False, allow_unknown=True
            )
            self.assertEqual(runtime.source, extracted.resolve())
            self.assertIn("ScriptHookRDR2.dll", runtime.files)

    def test_resolve_sdk_prefers_vendor_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vendor = Path(temporary) / "sdk"
            (vendor / "inc").mkdir(parents=True)
            (vendor / "lib").mkdir(parents=True)
            (vendor / "inc" / "main.h").write_text("main", encoding="utf-8")
            (vendor / "inc" / "natives.h").write_text("natives", encoding="utf-8")
            (vendor / "lib" / "ScriptHookRDR2.lib").write_bytes(b"lib")
            (vendor / "VERSION.txt").write_text(
                "ScriptHookRDR2_SDK_test\n", encoding="utf-8"
            )
            with mock.patch.object(installer, "VENDORED_SDK", vendor):
                sdk = installer.resolve_sdk_source(
                    None, prompt=False, allow_unknown=True
                )
            self.assertEqual(sdk, vendor.resolve())

    def test_find_msbuild_uses_vswhere_find_without_vc_require(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            msbuild = root / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
            msbuild.parent.mkdir(parents=True)
            msbuild.write_bytes(b"msbuild")
            with mock.patch.object(
                installer, "find_cxx_toolchain_pair", return_value=None
            ), mock.patch.object(
                installer,
                "_vswhere_lines",
                side_effect=lambda *args: (
                    [str(msbuild)] if args[:1] == ("-find",) else []
                ),
            ), mock.patch.object(
                installer, "_candidate_vs_roots", return_value=[]
            ), mock.patch.object(installer.shutil, "which", return_value=None):
                self.assertEqual(installer.find_msbuild(), msbuild)

    def test_has_cxx_toolchain_requires_cl(self) -> None:
        with mock.patch.object(installer, "find_cxx_toolchain_pair", return_value=None):
            self.assertFalse(installer.has_cxx_toolchain())
        with mock.patch.object(
            installer,
            "find_cxx_toolchain_pair",
            return_value=(Path("MSBuild.exe"), Path("cl.exe")),
        ):
            self.assertTrue(installer.has_cxx_toolchain())

    def test_pick_build_tools_skips_nonempty_leftover(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "2022"
            leftover = base / "BuildTools"
            leftover.mkdir(parents=True)
            (leftover / "junk.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(
                installer, "list_vs_instances", return_value=[]
            ), mock.patch.dict(
                os.environ,
                {"ProgramFiles(x86)": str(base.parent), "ProgramFiles": str(base.parent)},
                clear=False,
            ):
                # Force only our temp base by patching unique path construction
                chosen = installer.pick_build_tools_install_path()
            self.assertNotEqual(chosen, leftover.resolve())
            self.assertTrue(
                chosen.name in {"BuildToolsGameRecorder", "BuildTools"}
                or "BuildToolsGameRecorder" in str(chosen)
            )

    def test_resolve_plugin_prefers_dist_prebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary) / "dist" / "CameraPoseLoggerRDR2.asi"
            dist.parent.mkdir(parents=True)
            dist.write_bytes(_pe())
            with mock.patch.object(installer, "PREBUILT_PLUGIN", dist), mock.patch.object(
                installer, "ensure_cxx_toolchain"
            ) as ensure_mock:
                plugin = installer.resolve_plugin(sdk_dir=None)
            self.assertEqual(plugin, dist.resolve())
            ensure_mock.assert_not_called()

    def test_no_prompt_missing_game_is_an_intentional_skip(self) -> None:
        with mock.patch.object(installer, "find_rdr2_candidates", return_value=[]):
            with self.assertRaises(installer.InstallerSkipped):
                installer.resolve_rdr2_dir(None, prompt=False)

    def test_install_failure_rolls_back_every_managed_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "game"
            game_dir.mkdir()
            runtime = _runtime_zip(root / "ScriptHookRDR2_1.0.zip")
            payload = installer.validate_zip(runtime, installer.RUNTIME_REQUIRED)
            plugin = root / "CameraPoseLoggerRDR2.asi"
            plugin.write_bytes(_pe())
            recordings = root / "capture" / "recordings"
            original_config = b'{"owner":"user"}\n'
            (game_dir / installer.CONFIG_FILENAME).write_bytes(original_config)
            original_atomic_write = installer._write_json_atomic

            def fail_state_write(path: Path, value: dict) -> None:
                if path.name == installer.STATE_FILENAME:
                    raise OSError("injected state write failure")
                original_atomic_write(path, value)

            with mock.patch.object(
                installer, "_write_json_atomic", side_effect=fail_state_write
            ):
                with self.assertRaisesRegex(
                    installer.InstallerError, "已事务回滚"
                ):
                    installer.install_payload(
                        game_dir,
                        recordings,
                        payload,
                        plugin,
                        force_existing=True,
                    )

            for name in installer.MANAGED_NAMES:
                if name == installer.CONFIG_FILENAME:
                    continue
                self.assertFalse((game_dir / name).exists(), name)
            self.assertEqual(
                (game_dir / installer.CONFIG_FILENAME).read_bytes(),
                original_config,
            )
            self.assertFalse((game_dir / installer.STATE_DIRNAME).exists())
            self.assertFalse(
                (
                    root
                    / "capture"
                    / installer.CONTROL_DIRNAME
                    / installer.CONTROL_FILENAME
                ).exists()
            )
            self.assertEqual(
                list(game_dir.glob(f"{installer.STATE_DIRNAME}.transaction-*")),
                [],
            )

    def test_tampered_transaction_target_set_cannot_delete_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "game"
            game_dir.mkdir()
            control_file = root / "capture" / installer.CONTROL_FILENAME
            external = root / "outside.txt"
            external.write_text("keep", encoding="utf-8")
            transaction = installer.InstallTransaction(game_dir, control_file)
            manifest_path = transaction.directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["targets"][str(external)] = {
                "present": True,
                "snapshot": "999",
                "directory": False,
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(installer.InstallerError, "事务目标集合"):
                installer.recover_interrupted_transaction(game_dir, control_file)

            self.assertEqual(external.read_text(encoding="utf-8"), "keep")

    def test_tampered_transaction_control_cannot_delete_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "game"
            game_dir.mkdir()
            control_file = root / "capture" / installer.CONTROL_FILENAME
            external = root / "outside.txt"
            external.write_text("keep", encoding="utf-8")
            transaction = installer.InstallTransaction(game_dir, control_file)
            manifest_path = transaction.directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["control_file"] = str(external)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(installer.InstallerError, "事务控制文件"):
                installer.restore_transaction(
                    transaction.directory, game_dir, control_file
                )

            self.assertEqual(external.read_text(encoding="utf-8"), "keep")

    def test_first_install_refuses_unknown_managed_file_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "game"
            game_dir.mkdir()
            existing = game_dir / installer.CONFIG_FILENAME
            existing.write_bytes(b'{"owner":"user"}\n')
            runtime = _runtime_zip(root / "ScriptHookRDR2_1.0.zip")
            payload = installer.validate_zip(runtime, installer.RUNTIME_REQUIRED)
            plugin = root / "CameraPoseLoggerRDR2.asi"
            plugin.write_bytes(_pe())

            with self.assertRaisesRegex(
                installer.InstallerError, "默认不会覆盖"
            ):
                installer.install_payload(
                    game_dir, root / "recordings", payload, plugin
                )

            self.assertEqual(existing.read_bytes(), b'{"owner":"user"}\n')
            self.assertFalse((game_dir / installer.STATE_DIRNAME).exists())

    def test_force_existing_backs_up_original_managed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "game"
            game_dir.mkdir()
            original = b'{"owner":"user"}\n'
            existing = game_dir / installer.CONFIG_FILENAME
            existing.write_bytes(original)
            runtime = _runtime_zip(root / "ScriptHookRDR2_1.0.zip")
            payload = installer.validate_zip(runtime, installer.RUNTIME_REQUIRED)
            plugin = root / "CameraPoseLoggerRDR2.asi"
            plugin.write_bytes(_pe())

            state_file, _ = installer.install_payload(
                game_dir,
                root / "recordings",
                payload,
                plugin,
                force_existing=True,
            )

            state = json.loads(state_file.read_text(encoding="utf-8"))
            original_state = state["original_files"][installer.CONFIG_FILENAME]
            self.assertTrue(original_state["present"])
            self.assertEqual(
                original_state["sha256"],
                hashlib.sha256(original).hexdigest(),
            )
            self.assertEqual(
                Path(original_state["backup"]).read_bytes(),
                original,
            )
            self.assertNotEqual(existing.read_bytes(), original)

    def test_existing_idle_control_file_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "game"
            game_dir.mkdir()
            runtime = _runtime_zip(root / "ScriptHookRDR2_1.0.zip")
            payload = installer.validate_zip(runtime, installer.RUNTIME_REQUIRED)
            plugin = root / "CameraPoseLoggerRDR2.asi"
            plugin.write_bytes(_pe())
            recordings = root / "capture" / "recordings"
            control_file = (
                recordings.resolve().parent
                / installer.CONTROL_DIRNAME
                / installer.CONTROL_FILENAME
            )
            control_file.parent.mkdir(parents=True)
            original = b'{"status":"idle","owner":"other"}\n'
            control_file.write_bytes(original)

            state_file, returned_control = installer.install_payload(
                game_dir, recordings, payload, plugin
            )

            self.assertEqual(returned_control, control_file)
            self.assertEqual(control_file.read_bytes(), original)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(
                state["original_files"]["control_file"],
                {"present": True, "preserved": True},
            )

    def test_existing_recording_or_corrupt_control_is_rejected_by_default(
        self,
    ) -> None:
        cases = {
            "recording": b'{"status":"recording"}\n',
            "corrupt": b"{not-json",
        }
        for label, original in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                game_dir = root / "game"
                game_dir.mkdir()
                runtime = _runtime_zip(root / "ScriptHookRDR2_1.0.zip")
                payload = installer.validate_zip(
                    runtime, installer.RUNTIME_REQUIRED
                )
                plugin = root / "CameraPoseLoggerRDR2.asi"
                plugin.write_bytes(_pe())
                recordings = root / "capture" / "recordings"
                control_file = (
                    recordings.resolve().parent
                    / installer.CONTROL_DIRNAME
                    / installer.CONTROL_FILENAME
                )
                control_file.parent.mkdir(parents=True)
                control_file.write_bytes(original)

                with self.assertRaisesRegex(
                    installer.InstallerError, "不是安全的 idle 状态"
                ):
                    installer.install_payload(
                        game_dir, recordings, payload, plugin
                    )

                self.assertEqual(control_file.read_bytes(), original)
                self.assertFalse(
                    (game_dir / installer.STATE_DIRNAME).exists()
                )

    def test_force_existing_backs_up_and_resets_corrupt_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "game"
            game_dir.mkdir()
            runtime = _runtime_zip(root / "ScriptHookRDR2_1.0.zip")
            payload = installer.validate_zip(runtime, installer.RUNTIME_REQUIRED)
            plugin = root / "CameraPoseLoggerRDR2.asi"
            plugin.write_bytes(_pe())
            recordings = root / "capture" / "recordings"
            control_file = (
                recordings.resolve().parent
                / installer.CONTROL_DIRNAME
                / installer.CONTROL_FILENAME
            )
            control_file.parent.mkdir(parents=True)
            original = b"{not-json"
            control_file.write_bytes(original)

            state_file, returned_control = installer.install_payload(
                game_dir,
                recordings,
                payload,
                plugin,
                force_existing=True,
            )

            self.assertEqual(returned_control, control_file)
            self.assertEqual(
                json.loads(control_file.read_text(encoding="utf-8"))["status"],
                "idle",
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            saved = state["original_files"]["control_file"]
            self.assertTrue(saved["present"])
            self.assertEqual(
                saved["sha256"], hashlib.sha256(original).hexdigest()
            )
            self.assertEqual(Path(saved["backup"]).read_bytes(), original)

    def test_reinstall_refuses_modified_managed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "game"
            game_dir.mkdir()
            runtime = _runtime_zip(root / "ScriptHookRDR2_1.0.zip")
            payload = installer.validate_zip(runtime, installer.RUNTIME_REQUIRED)
            plugin = root / "CameraPoseLoggerRDR2.asi"
            plugin.write_bytes(_pe())
            recordings = root / "recordings"

            installer.install_payload(game_dir, recordings, payload, plugin)
            (game_dir / "CameraPoseLoggerRDR2.asi").write_bytes(b"modified")

            with self.assertRaisesRegex(
                installer.InstallerError, "受管文件已被修改或删除"
            ):
                installer.install_payload(game_dir, recordings, payload, plugin)


if __name__ == "__main__":
    unittest.main()
