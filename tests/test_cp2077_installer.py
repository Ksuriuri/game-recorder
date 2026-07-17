from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = PROJECT_ROOT / "scripts" / "install_cp2077_camera.py"
SPEC = importlib.util.spec_from_file_location("install_cp2077_camera", INSTALLER_PATH)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class Cp2077InstallerTests(unittest.TestCase):
    def test_reshade_detection_requires_dxgi_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            game = Path(temp_dir)
            x64 = game / "bin" / "x64"
            x64.mkdir(parents=True)
            (x64 / "ReShade.ini").write_text("[GENERAL]\n", encoding="utf-8")
            (x64 / "d3d12.dll").write_bytes(b"old-proxy")
            self.assertFalse(installer.reshade_installed(game))
            (x64 / "dxgi.dll").write_bytes(b"cp2077-proxy")
            self.assertTrue(installer.reshade_installed(game))

    def test_effect_search_path_removes_obsolete_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ini = Path(temp_dir) / "ReShade.ini"
            ini.write_text(
                "[GENERAL]\n"
                r"EffectSearchPaths=.\reshade-shaders\Shaders\**\**,.\reshade-shaders\Shaders\**"
                "\n",
                encoding="utf-8",
            )
            installer._ensure_reshade_effect_search_path(ini)
            text = ini.read_text(encoding="utf-8")
            self.assertIn(r"EffectSearchPaths=.\reshade-shaders\Shaders\**\**", text)
            self.assertNotIn(r"Shaders\**\**,.\reshade", text)

    def test_install_payload_replaces_legacy_camera_mod(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            game = root / "game"
            mods = (
                game
                / "bin"
                / "x64"
                / "plugins"
                / "cyber_engine_tweaks"
                / "mods"
            )
            legacy = mods / "cp2077_camera_export"
            legacy.mkdir(parents=True)
            (legacy / "init.lua").write_text("legacy", encoding="utf-8")
            recordings = root / "output" / "recordings"

            addon, shader = installer.install_depth_payload(game)
            mod = installer.install_mod(game, recordings)

            self.assertFalse(legacy.exists())
            self.assertTrue((mod / "init.lua").is_file())
            self.assertTrue(addon.is_file())
            self.assertTrue(shader.is_file())
            state = recordings.parent / ".cp2077_camera" / "install.json"
            self.assertTrue(state.is_file())


if __name__ == "__main__":
    unittest.main()
