from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CPP_PATH = PROJECT_ROOT / "rdr2-camera" / "CameraPoseLogger" / "main.cpp"
PROJECT_PATH = (
    PROJECT_ROOT
    / "rdr2-camera"
    / "CameraPoseLogger"
    / "CameraPoseLogger.vcxproj"
)


class Rdr2CameraLoggerStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cpp = CPP_PATH.read_text(encoding="utf-8")
        cls.project = PROJECT_PATH.read_text(encoding="utf-8")

    def test_uses_final_rendered_camera_native_hashes(self) -> None:
        expected = {
            "GetFinalRenderedCamCoord": "0x5352E025EC2B416F",
            "GetFinalRenderedCamRot": "0x602685BD85DD26CA",
            "GetFinalRenderedCamFov": "0x04AF77971E508F6A",
        }
        for function, native_hash in expected.items():
            pattern = (
                rf"{function}\s*\(\s*\)\s*\{{.*?"
                rf"invoke<[^>]+>\s*\(\s*{native_hash}(?:ULL)?"
            )
            self.assertRegex(self.cpp, re.compile(pattern, re.DOTALL))
        self.assertRegex(
            self.cpp,
            re.compile(
                r"GetFinalRenderedCamRot\s*\(\s*\)\s*\{.*?"
                r"0x602685BD85DD26CAULL\s*,\s*2\s*\)",
                re.DOTALL,
            ),
        )

    def test_disables_windows_min_max_macros(self) -> None:
        win32_define = self.cpp.index("#define WIN32_LEAN_AND_MEAN")
        nominmax_define = self.cpp.index("#define NOMINMAX")
        windows_include = self.cpp.index("#include <windows.h>")
        self.assertLess(win32_define, nominmax_define)
        self.assertLess(nominmax_define, windows_include)

    def test_header_declares_rdr2_schema_and_geometry_contract(self) -> None:
        for fragment in (
            r'\"schema\":\"rdr2_camera_v1\"',
            r'\"world_units\":\"meters\"',
            r'\"camera_to_world_translation_units\":\"meters\"',
            r'\"matrix_layout\":\"row_major\"',
            r'\"matrix_vector_convention\":\"row_vector\"',
            r'\"world_axes\":\"x_right_y_forward_z_up\"',
            r'\"camera_axes\":\"x_right_y_forward_z_up\"',
            r'\"fov_axis\":\"vertical\"',
        ):
            self.assertIn(fragment, self.cpp)

    def test_samples_write_matrix_fov_and_viewport_fields(self) -> None:
        self.assertRegex(self.cpp, r"const double matrix\s*\[\s*16\s*\]")
        self.assertIn(r'\"camera_to_world\":[', self.cpp)
        self.assertIn(r'\"fov_vertical_deg\":', self.cpp)
        self.assertIn(r'\"viewport_px\":[', self.cpp)
        self.assertRegex(
            self.cpp,
            re.compile(
                r"sample\.position\.x\s*,\s*sample\.position\.y\s*,\s*"
                r"sample\.position\.z\s*,\s*1\.0",
                re.DOTALL,
            ),
        )

    def test_project_is_release_x64_with_static_runtime(self) -> None:
        root = ET.fromstring(self.project)
        namespace = {"msb": "http://schemas.microsoft.com/developer/msbuild/2003"}
        configurations = {
            node.attrib.get("Include")
            for node in root.findall(".//msb:ProjectConfiguration", namespace)
        }
        self.assertEqual(configurations, {"Release|x64"})
        self.assertEqual(
            root.findtext(".//msb:RuntimeLibrary", namespaces=namespace),
            "MultiThreaded",
        )
        self.assertEqual(
            root.findtext(".//msb:ConfigurationType", namespaces=namespace),
            "DynamicLibrary",
        )
        self.assertIn("$(SDK_ROOT)\\inc", self.project)
        self.assertIn("ScriptHookRDR2.lib", self.project)


if __name__ == "__main__":
    unittest.main()
