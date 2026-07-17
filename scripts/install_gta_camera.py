#!/usr/bin/env python3
"""Install GTA V camera pose logger onto a machine.

Copies vendored ScriptHookV + ScriptHookVDotNet + CameraPoseLogger into the
GTA V folder. Interactive install only asks for the GTA root directory when
it cannot be detected automatically.

Usage::

    python scripts/install_gta_camera.py
    python scripts/install_gta_camera.py --gta-dir "D:\\...\\Grand Theft Auto V"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]

SHVDN_API = (
    "https://api.github.com/repos/scripthookvdotnet/scripthookvdotnet-nightly/releases/latest"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PROJ = PROJECT_ROOT / "gta-camera" / "CameraPoseLogger" / "CameraPoseLogger.csproj"
VENDORED_SHVDN = PROJECT_ROOT / "gta-camera" / "vendor" / "ScriptHookVDotNet"
VENDORED_SHV = PROJECT_ROOT / "gta-camera" / "vendor" / "ScriptHookV"
PREBUILT_DLL = PROJECT_ROOT / "gta-camera" / "dist" / "CameraPoseLogger.dll"

# Steam store app id for Grand Theft Auto V (classic).
GTA_STEAM_APP_ID = "271590"
GTA_STEAM_DIRNAMES = (
    "Grand Theft Auto V",
    "Grand Theft Auto V Enhanced",
)
GTA_EXE_NAMES = (
    "GTA5.exe",
    "GTA5_Enhanced.exe",
    "PlayGTAV.exe",
)


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            normalized = path.expanduser().resolve() if path.exists() else path.expanduser()
        except OSError:
            normalized = path.expanduser()
        key = os.path.normcase(str(normalized)).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
    return unique


def _registry_values(hive: object, subkey: str, names: tuple[str, ...]) -> list[str]:
    if winreg is None:
        return []
    views = [0]
    for attr in ("KEY_WOW64_32KEY", "KEY_WOW64_64KEY"):
        view = getattr(winreg, attr, 0)
        if view and view not in views:
            views.append(view)
    values: list[str] = []
    for view in views:
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as key:
                for name in names:
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    if value:
                        values.append(os.path.expandvars(str(value)).strip().strip('"'))
        except OSError:
            continue
    return values


def _windows_drive_roots() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import ctypes

        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except (AttributeError, OSError):
        mask = 0
    roots: list[Path] = []
    for index, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        path = Path(f"{letter}:/")
        if (mask & (1 << index)) or (not mask and path.exists()):
            roots.append(path)
    return roots


def _steam_roots() -> list[Path]:
    """Locate Steam installs via registry, Program Files, and common drive layouts."""
    roots: list[Path] = []
    if winreg is not None:
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for value in _registry_values(
                hive,
                r"SOFTWARE\Valve\Steam",
                ("SteamPath", "InstallPath"),
            ):
                roots.append(Path(value))
    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        value = os.environ.get(env_name, "").strip()
        if value:
            roots.append(Path(value) / "Steam")
    roots.extend(
        (
            Path(r"C:\Program Files (x86)\Steam"),
            Path(r"C:\Program Files\Steam"),
        )
    )
    # Non-default installs like F:\steam are common on gaming PCs /网吧.
    for drive in _windows_drive_roots():
        roots.extend(
            (
                drive / "Steam",
                drive / "steam",
                drive / "SteamLibrary",
                drive / "Program Files (x86)" / "Steam",
                drive / "Program Files" / "Steam",
            )
        )
    return _unique_paths(roots)


def _steam_libraries(steam_root: Path) -> list[Path]:
    libraries = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return libraries
    for match in re.finditer(r'"path"\s+"([^"]+)"', text, flags=re.IGNORECASE):
        value = match.group(1).replace("\\\\", "\\")
        if value:
            libraries.append(Path(value))
    return _unique_paths(libraries)


def _acf_install_dir(acf_path: Path) -> str | None:
    try:
        text = acf_path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return None
    match = re.search(r'"installdir"\s+"([^"]+)"', text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _registered_gta_locations() -> list[Path]:
    if winreg is None:
        return []
    locations: list[Path] = []
    hives = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    keys = (
        (
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App {GTA_STEAM_APP_ID}",
            ("InstallLocation",),
        ),
        (
            r"SOFTWARE\Rockstar Games\GTAV",
            ("InstallFolder", "Install Folder", "InstallPath"),
        ),
        (
            r"SOFTWARE\Rockstar Games\Grand Theft Auto V",
            ("InstallFolder", "Install Folder", "InstallPath"),
        ),
        (
            r"SOFTWARE\WOW6432Node\Rockstar Games\GTAV",
            ("InstallFolder", "Install Folder", "InstallPath"),
        ),
        (
            r"SOFTWARE\WOW6432Node\Rockstar Games\Grand Theft Auto V",
            ("InstallFolder", "Install Folder", "InstallPath"),
        ),
    )
    for hive in hives:
        for key, names in keys:
            locations.extend(Path(value) for value in _registry_values(hive, key, names))
        uninstall_base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        views = [0]
        for attr in ("KEY_WOW64_32KEY", "KEY_WOW64_64KEY"):
            view = getattr(winreg, attr, 0)
            if view and view not in views:
                views.append(view)
        for view in views:
            try:
                with winreg.OpenKey(
                    hive, uninstall_base, 0, winreg.KEY_READ | view
                ) as uninstall:
                    count = winreg.QueryInfoKey(uninstall)[0]
                    subkeys = [
                        winreg.EnumKey(uninstall, index) for index in range(count)
                    ]
            except OSError:
                continue
            for subkey in subkeys:
                full_key = uninstall_base + "\\" + subkey
                display_names = _registry_values(hive, full_key, ("DisplayName",))
                if not any(
                    "grand theft auto v" in name.casefold() for name in display_names
                ):
                    continue
                locations.extend(
                    Path(value)
                    for value in _registry_values(
                        hive, full_key, ("InstallLocation", "InstallPath")
                    )
                )
    return locations


def find_gta_candidates() -> list[Path]:
    """Discover likely GTA V roots (may include non-existent paths)."""
    found: list[Path] = []

    env = os.environ.get("GTAV_DIR", "").strip().strip('"')
    if env:
        found.append(Path(env))

    found.extend(_registered_gta_locations())

    for steam_root in _steam_roots():
        for library in _steam_libraries(steam_root):
            common = library / "steamapps" / "common"
            manifest = library / "steamapps" / f"appmanifest_{GTA_STEAM_APP_ID}.acf"
            installdir = _acf_install_dir(manifest)
            if installdir:
                found.append(common / installdir)
            for dirname in GTA_STEAM_DIRNAMES:
                found.append(common / dirname)

    program_files = [
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    ]
    for root in filter(None, program_files):
        found.extend(
            (
                Path(root) / "Rockstar Games" / "Grand Theft Auto V",
                Path(root) / "Epic Games" / "GTAV",
            )
        )

    # Last-resort common layouts across drives (no hard-coded single drive letter).
    for drive in _windows_drive_roots():
        for dirname in GTA_STEAM_DIRNAMES:
            found.extend(
                (
                    drive / "Games" / dirname,
                    drive / "Rockstar Games" / dirname,
                )
            )

    return _unique_paths(found)


def is_gta_dir(path: Path) -> bool:
    return any((path / name).is_file() for name in GTA_EXE_NAMES)


def resolve_gta_dir(explicit: Path | None, *, prompt: bool) -> tuple[Path | None, bool]:
    """Return ``(gta_dir, skipped)``.

    ``skipped=True`` means the user chose to skip (empty Enter).
    ``(None, False)`` means unavailable / invalid without a voluntary skip.
    """
    if explicit is not None:
        if is_gta_dir(explicit):
            return explicit.resolve(), False
        _print(f"[错误] 不是有效的 GTA V 目录：{explicit}")
        if not prompt:
            return None, False
        _print("请重新输入，或直接回车跳过 GTA 相机插件安装。")

    cands = [p for p in find_gta_candidates() if is_gta_dir(p)]
    if len(cands) == 1 and explicit is None:
        return cands[0].resolve(), False
    if len(cands) > 1 and explicit is None:
        _print("检测到多个 GTA V 安装：")
        for i, p in enumerate(cands, 1):
            _print(f"  [{i}] {p}")
        if not prompt:
            _print(f"[自动] 无人值守模式使用：{cands[0]}")
            return cands[0].resolve(), False
        try:
            choice = input(
                f"选择 [1-{len(cands)}]，或输入完整路径；直接回车跳过: "
            ).strip().strip('"')
        except EOFError:
            choice = ""
        if not choice:
            _print("[跳过] 未安装 GTA 相机插件。")
            return None, True
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(cands):
                return cands[idx].resolve(), False
            _print("[错误] 无效选项。")
            return None, False
        path = Path(choice)
        if is_gta_dir(path):
            return path.resolve(), False
        _print(f"[错误] 目录无效（需要含 GTA5.exe）：{path}")
        return None, False

    if not prompt:
        _print("[错误] 未找到 GTA V。设置 GTAV_DIR 或传入 --gta-dir。")
        return None, False

    _print("未自动找到 GTA V。")
    while True:
        try:
            typed = input(
                "请输入 GTA 主目录路径（含 GTA5.exe 的文件夹；直接回车跳过）: "
            ).strip().strip('"')
        except EOFError:
            typed = ""
        if not typed:
            _print("[跳过] 未安装 GTA 相机插件。")
            return None, True
        path = Path(typed)
        if is_gta_dir(path):
            return path.resolve(), False
        _print(f"[错误] 不是有效目录（未找到 GTA5.exe）：{path}")
        _print("请重新输入，或直接回车跳过。")


def scripthookv_installed(gta: Path) -> bool:
    return (gta / "ScriptHookV.dll").is_file() and (
        (gta / "dinput8.dll").is_file() or (gta / "version.dll").is_file()
    )


def shvdn_installed(gta: Path) -> bool:
    return (gta / "ScriptHookVDotNet.asi").is_file() and (
        (gta / "ScriptHookVDotNet3.dll").is_file()
        or (gta / "scripts" / "ScriptHookVDotNet3.dll").is_file()
    )


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "game-recorder-gta-camera-installer"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def install_shvdn_from_vendor(gta: Path) -> bool:
    """Copy bundled SHVDN into the GTA folder. Returns True if vendor was used."""
    if not (VENDORED_SHVDN / "ScriptHookVDotNet.asi").is_file():
        return False
    if not (VENDORED_SHVDN / "ScriptHookVDotNet3.dll").is_file():
        return False
    ver = ""
    ver_path = VENDORED_SHVDN / "VERSION.txt"
    if ver_path.is_file():
        ver = ver_path.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
    _print(f"使用项目内置 ScriptHookVDotNet{(' ' + ver) if ver else ''} …")
    for name in (
        "ScriptHookVDotNet.asi",
        "ScriptHookVDotNet.dll",
        "ScriptHookVDotNet2.dll",
        "ScriptHookVDotNet3.dll",
    ):
        src = VENDORED_SHVDN / name
        if src.is_file():
            shutil.copy2(src, gta / name)
            _print(f"  已复制 {name}")
    for src in VENDORED_SHVDN.glob("ScriptHookVDotNet*.xml"):
        shutil.copy2(src, gta / src.name)
    for notice in ("LICENSE.txt", "README.txt"):
        src = VENDORED_SHVDN / notice
        if src.is_file():
            shutil.copy2(src, gta / f"ScriptHookVDotNet_{notice}")
    return True


def download_shvdn(gta: Path, cache_dir: Path) -> None:
    _print("正在查询 ScriptHookVDotNet 最新 release …")
    req = urllib.request.Request(
        SHVDN_API,
        headers={
            "User-Agent": "game-recorder-gta-camera-installer",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)

    assets = data.get("assets") or []
    zip_asset = None
    for a in assets:
        name = (a.get("name") or "").lower()
        if name.endswith(".zip") and "scripthookvdotnet" in name:
            zip_asset = a
            break
    if zip_asset is None:
        for a in assets:
            if (a.get("name") or "").lower().endswith(".zip"):
                zip_asset = a
                break
    if zip_asset is None:
        raise RuntimeError("GitHub release 中未找到 SHVDN zip 资源")

    url = zip_asset["browser_download_url"]
    _print(f"正在下载 {zip_asset['name']} …")
    zip_path = cache_dir / zip_asset["name"]
    _download(url, zip_path)

    extract_dir = cache_dir / "shvdn_extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    # Zip layout varies: files at root or in a single subfolder
    roots = list(extract_dir.iterdir())
    src_root = roots[0] if len(roots) == 1 and roots[0].is_dir() else extract_dir

    for name in (
        "ScriptHookVDotNet.asi",
        "ScriptHookVDotNet.dll",
        "ScriptHookVDotNet2.dll",
        "ScriptHookVDotNet3.dll",
    ):
        src = src_root / name
        if src.is_file():
            shutil.copy2(src, gta / name)
            _print(f"  已复制 {name}")

    # Some builds put xml / config next to asi
    for src in src_root.glob("ScriptHookVDotNet*.xml"):
        shutil.copy2(src, gta / src.name)


def find_csc() -> Path | None:
    """Locate .NET Framework csc.exe (no SDK required on most Windows boxes)."""
    roots = [
        os.environ.get("WINDIR", r"C:\Windows"),
        r"C:\Windows",
    ]
    rels = [
        r"Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        r"Microsoft.NET\Framework\v4.0.30319\csc.exe",
    ]
    for root in roots:
        for rel in rels:
            p = Path(root) / rel
            if p.is_file():
                return p
    return None


def ensure_dotnet() -> bool:
    try:
        r = subprocess.run(
            ["dotnet", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _find_built_dll() -> Path | None:
    matches = list((PLUGIN_PROJ.parent / "bin").rglob("CameraPoseLogger.dll"))
    if matches:
        return matches[0]
    dist = PROJECT_ROOT / "gta-camera" / "dist" / "CameraPoseLogger.dll"
    if dist.is_file():
        return dist
    return None


def build_plugin_with_csc(gta: Path) -> Path:
    csc = find_csc()
    if csc is None:
        raise RuntimeError("未找到 csc.exe")
    ref = gta / "ScriptHookVDotNet3.dll"
    if not ref.is_file():
        raise FileNotFoundError(f"缺少 {ref}")

    src = PLUGIN_PROJ.parent / "CameraPoseLogger.cs"
    out_dir = PLUGIN_PROJ.parent / "bin" / "Release"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dll = out_dir / "CameraPoseLogger.dll"

    # System.Windows.Forms is in the GAC / reference assemblies next to csc
    fw_dir = csc.parent
    winforms = fw_dir / "System.Windows.Forms.dll"
    system_drawing = fw_dir / "System.Drawing.dll"

    cmd = [
        str(csc),
        "/nologo",
        "/optimize+",
        "/target:library",
        f"/out:{out_dll}",
        f"/reference:{ref}",
    ]
    if winforms.is_file():
        cmd.append(f"/reference:{winforms}")
    if system_drawing.is_file():
        cmd.append(f"/reference:{system_drawing}")
    cmd.append(str(src))

    _print(f"正在用 Framework csc 编译 CameraPoseLogger …")
    _print(f"  csc: {csc}")
    r = subprocess.run(cmd, cwd=str(PLUGIN_PROJ.parent), check=False)
    if r.returncode != 0 or not out_dll.is_file():
        raise RuntimeError("csc 编译失败")
    return out_dll


def build_plugin_with_dotnet(gta: Path) -> Path:
    ref = gta / "ScriptHookVDotNet3.dll"
    if not ref.is_file():
        raise FileNotFoundError(f"缺少 {ref}（请先安装 ScriptHookVDotNet）")

    _print("正在用 dotnet SDK 编译 CameraPoseLogger …")
    r = subprocess.run(
        [
            "dotnet",
            "build",
            str(PLUGIN_PROJ),
            "-c",
            "Release",
            f"-p:GtaVDir={gta}",
        ],
        cwd=str(PLUGIN_PROJ.parent),
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError("dotnet build 失败")

    dll = PLUGIN_PROJ.parent / "bin" / "Release" / "CameraPoseLogger.dll"
    if not dll.is_file():
        matches = list((PLUGIN_PROJ.parent / "bin").rglob("CameraPoseLogger.dll"))
        if not matches:
            raise FileNotFoundError("编译成功但未找到 CameraPoseLogger.dll")
        dll = matches[0]
    return dll


def build_plugin(gta: Path) -> Path:
    if not PLUGIN_PROJ.is_file():
        raise FileNotFoundError(f"缺少工程：{PLUGIN_PROJ}")

    existing = _find_built_dll()
    if PREBUILT_DLL.is_file():
        _print(f"使用预编译插件：{PREBUILT_DLL}")
        return PREBUILT_DLL

    # 2) Framework csc — no SDK needed
    try:
        return build_plugin_with_csc(gta)
    except Exception as exc:
        _print(f"csc 编译不可用：{exc}")

    # 3) Full SDK
    if ensure_dotnet():
        return build_plugin_with_dotnet(gta)

    if existing is not None:
        _print(f"回退使用已有编译产物：{existing}")
        return existing

    raise RuntimeError(
        "无法编译 CameraPoseLogger：未找到 .NET Framework csc.exe，也没有 dotnet SDK。\n"
        "  可安装 .NET SDK: https://dotnet.microsoft.com/download\n"
        "  或在有 SDK 的机器编译后把 DLL 放到 gta-camera\\dist\\CameraPoseLogger.dll"
    )


def write_config(gta: Path, recordings_root: Path) -> Path:
    scripts = gta / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    cam_dir = (Path(recordings_root).resolve().parent / ".gta_camera")
    cam_dir.mkdir(parents=True, exist_ok=True)
    control = cam_dir / "active_session.json"
    cfg = {
        "output_dir": str(cam_dir).replace("\\", "/"),
        "control_file": str(control).replace("\\", "/"),
        "follow_recorder": True,
        "sample_hz": 30,
        "toggle_key": "none",
        "flush_key": "F9",
    }
    path = scripts / "camera_pose_logger.config.json"
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Seed idle control file so plugin has a known path
    idle = {
        "status": "idle",
        "updated_at_ms": 0,
    }
    control.write_text(json.dumps(idle, indent=2) + "\n", encoding="utf-8")
    return path


def install_plugin_dll(gta: Path, dll: Path) -> Path:
    scripts = gta / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    dest = scripts / "CameraPoseLogger.dll"
    shutil.copy2(dll, dest)
    return dest


def install_scripthookv_from_vendor(gta: Path) -> None:
    """Copy vendored ScriptHookV.dll + dinput8.dll into the GTA root."""
    dll = VENDORED_SHV / "ScriptHookV.dll"
    dinput = VENDORED_SHV / "dinput8.dll"
    if not dll.is_file() or not dinput.is_file():
        raise FileNotFoundError(
            f"项目缺少 ScriptHookV 文件，请放到:\n  {VENDORED_SHV}\\ScriptHookV.dll\n  {VENDORED_SHV}\\dinput8.dll"
        )
    ver = ""
    ver_path = VENDORED_SHV / "VERSION.txt"
    if ver_path.is_file():
        ver = ver_path.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
    _print(f"正在安装 ScriptHookV{(' (' + ver + ')') if ver else ''} …")
    shutil.copy2(dll, gta / "ScriptHookV.dll")
    shutil.copy2(dinput, gta / "dinput8.dll")
    _print("  已复制 ScriptHookV.dll")
    _print("  已复制 dinput8.dll")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 GTA V 相机位姿采集插件")
    parser.add_argument("--gta-dir", type=Path, default=None, help="GTA V 安装目录")
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=PROJECT_ROOT / "recordings",
        help="game-recorder 输出目录（默认项目 recordings/）",
    )
    parser.add_argument(
        "--skip-shvdn-download",
        action="store_true",
        help="不联网下载 SHVDN（优先用项目内 vendor）",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="无人值守：找不到游戏时失败，不询问路径",
    )
    args = parser.parse_args(argv)

    _print("============================================================")
    _print("  GTA 相机轨迹插件安装")
    _print("============================================================")

    gta, skipped = resolve_gta_dir(args.gta_dir, prompt=not args.no_prompt)
    if gta is None:
        return 3 if skipped else 1
    _print(f"GTA V: {gta}")

    cache = PROJECT_ROOT / ".tools" / "gta-camera-cache"
    cache.mkdir(parents=True, exist_ok=True)

    try:
        install_scripthookv_from_vendor(gta)
    except FileNotFoundError as exc:
        _print(f"[错误] {exc}")
        return 1

    if install_shvdn_from_vendor(gta):
        pass
    elif shvdn_installed(gta):
        _print("ScriptHookVDotNet: 已安装（项目 vendor 中无副本，保留现有）")
    elif args.skip_shvdn_download:
        _print("[错误] 未安装 ScriptHookVDotNet，且项目 vendor 中也没有")
        return 1
    else:
        try:
            download_shvdn(gta, cache)
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
            _print(f"[错误] ScriptHookVDotNet 下载失败：{exc}")
            _print(
                "  可将官方 zip 解压到 gta-camera\\vendor\\ScriptHookVDotNet\\ 后重试。"
            )
            return 1

    try:
        dll = build_plugin(gta)
        dest = install_plugin_dll(gta, dll)
        cfg = write_config(gta, Path(args.recordings_dir))
    except Exception as exc:
        _print(f"[错误] {exc}")
        return 1

    missing = []
    for rel in (
        "ScriptHookV.dll",
        "dinput8.dll",
        "ScriptHookVDotNet.asi",
        "ScriptHookVDotNet3.dll",
        "scripts\\CameraPoseLogger.dll",
        "scripts\\camera_pose_logger.config.json",
    ):
        if not (gta / rel).is_file():
            missing.append(rel)
    if missing:
        _print("[错误] 安装后校验失败，缺少：")
        for m in missing:
            _print(f"  - {m}")
        return 1

    control = Path(args.recordings_dir).resolve().parent / ".gta_camera" / "active_session.json"
    _print()
    _print("安装完成（已校验文件齐全）。")
    _print(f"  GTA 目录: {gta}")
    _print(f"  相机插件: {dest}")
    _print(f"  同步信号: {control}")
    _print("  下一步: 故事模式进 GTA → run.bat 录制 → session 内应有 camera.jsonl")
    _print("============================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
