#!/usr/bin/env python3
"""Install GTA V camera pose logger onto a machine.

Copies vendored ScriptHookV + ScriptHookVDotNet + CameraPoseLogger into the
GTA V folder. Interactive install only asks for the GTA root directory when
it cannot be detected automatically.

Usage::

    python scripts/install_gta_camera.py
    python scripts/install_gta_camera.py --no-prompt
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
PREBUILT_ASI = PROJECT_ROOT / "gta-camera" / "dist" / "CameraPoseLogger.asi"
ASI_BUILD_OUT = (
    PROJECT_ROOT
    / "gta-camera"
    / "AsiCameraPoseLogger"
    / "bin"
    / "Release"
    / "CameraPoseLogger.asi"
)
SHVDN_FILES = (
    "ScriptHookVDotNet.asi",
    "ScriptHookVDotNet.dll",
    "ScriptHookVDotNet2.dll",
    "ScriptHookVDotNet3.dll",
    "ScriptHookVDotNet_README.txt",
    "ScriptHookVDotNet_LICENSE.txt",
)

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
# Processes that lock ScriptHookV.dll / ASI files while running.
GTA_PROCESS_NAMES = (
    "GTA5.exe",
    "GTA5_Enhanced.exe",
    "GTA5_Enhanced_BE.exe",
    "PlayGTAV.exe",
)
# Enhanced Edition must use ScriptHookV's xinput1_4 ASI loader (or OpenRPF's
# dsound.dll). Classic dinput8.dll on Enhanced commonly prevents launch,
# especially when stacked with an existing Enhanced loader / crack proxies.
ENHANCED_ASI_LOADERS = ("xinput1_4.dll", "dsound.dll")
CLASSIC_ASI_LOADER = "dinput8.dll"


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


def is_enhanced_gta(path: Path) -> bool:
    """True when the install is GTA V Enhanced (not Legacy)."""
    return (path / "GTA5_Enhanced.exe").is_file()


def is_legacy_gta(path: Path) -> bool:
    """True when the install has classic GTA5.exe."""
    return (path / "GTA5.exe").is_file()


def gta_edition_label(path: Path) -> str:
    legacy = is_legacy_gta(path)
    enhanced = is_enhanced_gta(path)
    if legacy and enhanced:
        return "Legacy+Enhanced"
    if enhanced:
        return "Enhanced"
    if legacy:
        return "Legacy"
    return "GTA V"


def asi_loader_present(gta: Path) -> bool:
    """Return whether a ScriptHook-compatible ASI loader is already present."""
    if is_enhanced_gta(gta):
        return any((gta / name).is_file() for name in ENHANCED_ASI_LOADERS)
    return (gta / CLASSIC_ASI_LOADER).is_file()


def running_gta_processes() -> list[str]:
    """Return running GTA-related image names (case-preserved from tasklist)."""
    if os.name != "nt":
        return []
    creation_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_no_window,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    import csv
    import io

    wanted = {name.casefold() for name in GTA_PROCESS_NAMES}
    found: set[str] = set()
    for row in csv.reader(io.StringIO(result.stdout)):
        if not row:
            continue
        image = row[0].strip().strip('"')
        if image.casefold() in wanted:
            found.add(image)
    return sorted(found, key=str.casefold)


def _gta_process_pids(image: str) -> list[int]:
    """Return PIDs for one image name via tasklist."""
    if os.name != "nt":
        return []
    creation_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_no_window,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []

    import csv
    import io

    pids: list[int] = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 2:
            continue
        name = row[0].strip().strip('"')
        if name.casefold() != image.casefold():
            continue
        pid_text = row[1].strip().strip('"')
        if pid_text.isdigit():
            pids.append(int(pid_text))
    return pids


def _run_silent(cmd: list[str]) -> None:
    creation_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_no_window,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def close_gta_processes(*, wait_seconds: float = 3.0) -> list[str]:
    """Force-close leftover GTA processes that lock ScriptHookV/ASI files.

    Returns the image names that were detected before killing.
    """
    import time

    running = running_gta_processes()
    if not running:
        return []

    _print("检测到游戏仍在运行，正在结束进程以免占用插件文件：")
    for image in running:
        _print(f"  结束 {image}")
        _run_silent(["taskkill", "/F", "/IM", image, "/T"])
        for pid in _gta_process_pids(image):
            _run_silent(["taskkill", "/F", "/PID", str(pid), "/T"])
        # Hung Enhanced processes sometimes ignore /IM but accept WMI terminate.
        _run_silent(
            ["wmic", "process", "where", f"name='{image}'", "call", "terminate"]
        )

    deadline = time.monotonic() + max(0.5, wait_seconds)
    while time.monotonic() < deadline:
        if not running_gta_processes():
            break
        time.sleep(0.25)

    leftover = running_gta_processes()
    if leftover:
        _print(
            "[警告] 仍有进程未退出："
            + "、".join(leftover)
            + "；若复制失败请到任务管理器手动结束后再试。"
        )
    else:
        _print("  游戏进程已结束。")
    return running


def _copy_replace(src: Path, dst: Path) -> None:
    """Copy ``src`` onto ``dst``, with a clearer lock error if Permission denied."""
    try:
        shutil.copy2(src, dst)
    except PermissionError as exc:
        raise PermissionError(
            f"无法写入 {dst}（文件被占用）。\n"
            "  通常是 GTA5 / PlayGTAV 仍在运行；安装器已尝试结束进程，"
            "请确认任务管理器中无残留后重试。"
        ) from exc


def _prompt_gta_edition(
    *,
    title: str,
    exe_names: tuple[str, ...],
    candidates: list[Path],
) -> Path | None:
    """Ask once for one GTA edition. Empty Enter skips."""
    _print()
    _print(title)
    if candidates:
        for index, path in enumerate(candidates, 1):
            _print(f"  [{index}] {path}")
        hint = f"选择 [1-{len(candidates)}]、输入完整路径；直接回车跳过: "
    else:
        _print("  （未自动检测到）")
        hint = f"请输入含 {' / '.join(exe_names)} 的目录；直接回车跳过: "

    while True:
        try:
            choice = input(hint).strip().strip('"')
        except EOFError:
            choice = ""
        if not choice:
            return None
        if choice.isdigit() and candidates:
            idx = int(choice) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx].resolve()
            _print("[错误] 无效选项，请重试或直接回车跳过。")
            continue
        path = Path(choice)
        if any((path / name).is_file() for name in exe_names):
            return path.resolve()
        _print(f"[错误] 目录无效（需要含 {' / '.join(exe_names)}）：{path}")
        _print("请重新输入，或直接回车跳过。")


def resolve_gta_dirs(
    explicit: Path | None,
    *,
    prompt: bool,
) -> tuple[list[Path], bool]:
    """Return ``(dirs_to_install, skipped_all)``.

    Interactive mode asks for Legacy and Enhanced separately (Enter skips each),
    matching the other per-game installers.
    """
    if explicit is not None:
        if is_gta_dir(explicit):
            return [explicit.resolve()], False
        _print(f"[错误] 不是有效的 GTA V 目录：{explicit}")
        if not prompt:
            return [], False
        _print("改为交互询问经典版 / 增强版路径。")

    cands = [p for p in find_gta_candidates() if is_gta_dir(p)]
    if cands:
        _print("检测到以下 GTA V 安装：")
        for index, path in enumerate(cands, 1):
            _print(f"  [{index}] {path}  ({gta_edition_label(path)})")
    else:
        _print("未自动找到 GTA V 安装。")

    if not prompt:
        if not cands:
            _print("[错误] 未找到 GTA V。设置 GTAV_DIR 或传入 --gta-dir。")
            return [], False
        chosen = [path.resolve() for path in cands]
        _print("[自动] 无人值守模式将安装到以上全部检测到的目录。")
        return chosen, False

    _print()
    _print("将分别询问经典版与增强版（可只装其中一个；直接回车跳过该项）。")
    legacy_cands = [p for p in cands if is_legacy_gta(p)]
    enhanced_cands = [p for p in cands if is_enhanced_gta(p)]

    selected: list[Path] = []
    legacy = _prompt_gta_edition(
        title="经典版 GTA V（GTA5.exe）",
        exe_names=("GTA5.exe",),
        candidates=legacy_cands,
    )
    if legacy is not None:
        selected.append(legacy)

    enhanced = _prompt_gta_edition(
        title="增强版 GTA V（GTA5_Enhanced.exe）",
        exe_names=("GTA5_Enhanced.exe",),
        candidates=enhanced_cands,
    )
    if enhanced is not None:
        selected.append(enhanced)

    unique = _unique_paths(selected)
    if not unique:
        _print("[跳过] 未安装 GTA 相机插件。")
        return [], True
    return unique, False


def resolve_gta_dir(explicit: Path | None, *, prompt: bool) -> tuple[Path | None, bool]:
    """Back-compat wrapper: first selected dir, or ``(None, skipped)``."""
    dirs, skipped = resolve_gta_dirs(explicit, prompt=prompt)
    if not dirs:
        return None, skipped
    return dirs[0], False


def scripthookv_installed(gta: Path) -> bool:
    return (gta / "ScriptHookV.dll").is_file() and asi_loader_present(gta)


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
    """Write camera_pose_logger.config.json next to the ASI (game root)."""
    cam_dir = Path(recordings_root).resolve().parent / ".gta_camera"
    cam_dir.mkdir(parents=True, exist_ok=True)
    control = cam_dir / "active_session.json"
    cfg = {
        "output_dir": str(cam_dir).replace("\\", "/"),
        "control_file": str(control).replace("\\", "/"),
        "follow_recorder": True,
        "sample_hz": 30,
        "poll_interval_ms": 100,
        "flush_every_samples": 30,
        "toggle_key": "none",
        "flush_key": "F9",
    }
    path = gta / "camera_pose_logger.config.json"
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Keep a scripts/ copy for any leftover SHVDN setups.
    scripts = gta / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "camera_pose_logger.config.json").write_text(
        path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    idle = {
        "status": "idle",
        "updated_at_ms": 0,
    }
    control.write_text(json.dumps(idle, indent=2) + "\n", encoding="utf-8")
    return path


def find_asi_plugin() -> Path | None:
    if PREBUILT_ASI.is_file():
        return PREBUILT_ASI
    if ASI_BUILD_OUT.is_file():
        return ASI_BUILD_OUT
    matches = list(
        (PROJECT_ROOT / "gta-camera" / "AsiCameraPoseLogger").rglob("CameraPoseLogger.asi")
    )
    return matches[0] if matches else None


def build_asi_plugin() -> Path:
    existing = find_asi_plugin()
    if existing is not None and existing.resolve() == PREBUILT_ASI.resolve():
        _print(f"使用预编译原生插件：{existing}")
        return existing

    msbuild_candidates = [
        PROJECT_ROOT / ".tools" / "vs2022-buildtools" / "MSBuild" / "Current" / "Bin" / "MSBuild.exe",
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe"),
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe"),
    ]
    msbuild = next((p for p in msbuild_candidates if p.is_file()), None)
    if msbuild is None:
        if existing is not None:
            _print(f"未找到 MSBuild，回退已有 ASI：{existing}")
            return existing
        raise RuntimeError(
            "缺少 CameraPoseLogger.asi，且未找到 MSBuild。\n"
            "  请先运行 gta-camera\\build_asi.bat，或把 ASI 放到 gta-camera\\dist\\"
        )

    proj = PROJECT_ROOT / "gta-camera" / "AsiCameraPoseLogger" / "CameraPoseLogger.vcxproj"
    _print(f"正在编译原生 CameraPoseLogger.asi …\n  msbuild: {msbuild}")
    result = subprocess.run(
        [
            str(msbuild),
            str(proj),
            "/m",
            "/nologo",
            "/p:Configuration=Release",
            "/p:Platform=x64",
        ],
        cwd=str(proj.parent),
        check=False,
    )
    if result.returncode != 0 or not ASI_BUILD_OUT.is_file():
        raise RuntimeError("CameraPoseLogger.asi 编译失败")
    PREBUILT_ASI.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ASI_BUILD_OUT, PREBUILT_ASI)
    return PREBUILT_ASI


def remove_shvdn_stack(gta: Path) -> None:
    """Remove SHVDN (.NET) stack that hangs GTA V Enhanced on startup."""
    removed = False
    for name in SHVDN_FILES:
        path = gta / name
        if path.is_file():
            try:
                path.unlink()
            except PermissionError as exc:
                raise PermissionError(
                    f"无法删除 {path}（仍被占用）。请确认游戏已退出后重试。"
                ) from exc
            removed = True
            _print(f"  已移除 {name}")
    legacy_dll = gta / "scripts" / "CameraPoseLogger.dll"
    if legacy_dll.is_file():
        try:
            legacy_dll.unlink()
        except PermissionError as exc:
            raise PermissionError(
                f"无法删除 {legacy_dll}（仍被占用）。请确认游戏已退出后重试。"
            ) from exc
        removed = True
        _print("  已移除 scripts\\CameraPoseLogger.dll")
    if removed:
        _print("  （SHVDN/.NET 相机会导致 Enhanced 启动卡死，已改用原生 ASI）")


# Crack bundles that commonly freeze Enhanced once ScriptHookV scripts start.
ENHANCED_INCOMPATIBLE_ASIS = (
    "NativeTrainer.asi",
    "NativeTrainerConfig.xml",
    "TrainerV.asi",
    "trainerv.ini",
    "OpenRPF.asi",
)


def quarantine_enhanced_incompatible_mods(gta: Path) -> None:
    """Move known-hanging crack trainers out of the Enhanced game root."""
    if not is_enhanced_gta(gta):
        return
    quarantine = gta / "_mods_incompatible_enhanced"
    moved = False
    for name in ENHANCED_INCOMPATIBLE_ASIS:
        src = gta / name
        if not src.is_file():
            continue
        quarantine.mkdir(parents=True, exist_ok=True)
        dest = quarantine / name
        try:
            if dest.exists():
                dest.unlink()
            src.rename(dest)
        except PermissionError as exc:
            raise PermissionError(
                f"无法移动 {src}（仍被占用）。请确认游戏已退出后重试。"
            ) from exc
        moved = True
        _print(f"  已隔离 {name} → _mods_incompatible_enhanced\\")
    # Prefer a single Enhanced ASI loader; dsound + xinput together is fragile.
    dsound = gta / "dsound.dll"
    if dsound.is_file() and (gta / "xinput1_4.dll").is_file():
        quarantine.mkdir(parents=True, exist_ok=True)
        dest = quarantine / "dsound.dll"
        try:
            if dest.exists():
                dest.unlink()
            dsound.rename(dest)
        except PermissionError as exc:
            raise PermissionError(
                f"无法移动 {dsound}（仍被占用）。请确认游戏已退出后重试。"
            ) from exc
        moved = True
        _print("  已隔离 dsound.dll（与 xinput1_4 双加载器冲突）")
    if moved:
        _print("  说明: 这些破解自带修改器在 Enhanced+当前 ScriptHookV 下会卡死启动")


def install_plugin_dll(gta: Path, dll: Path) -> Path:
    scripts = gta / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    dest = scripts / "CameraPoseLogger.dll"
    shutil.copy2(dll, dest)
    return dest


def install_scripthookv_from_vendor(gta: Path) -> None:
    """Copy vendored ScriptHookV into the GTA root with the correct ASI loader.

    Legacy uses ``dinput8.dll``. Enhanced must keep ``xinput1_4.dll`` /
    ``dsound.dll`` and must not receive classic ``dinput8.dll``.
    """
    dll = VENDORED_SHV / "ScriptHookV.dll"
    dinput = VENDORED_SHV / CLASSIC_ASI_LOADER
    if not dll.is_file():
        raise FileNotFoundError(
            f"项目缺少 ScriptHookV 文件，请放到:\n  {VENDORED_SHV}\\ScriptHookV.dll"
        )
    enhanced = is_enhanced_gta(gta)
    if not enhanced and not dinput.is_file():
        raise FileNotFoundError(
            f"项目缺少 ScriptHookV ASI 加载器，请放到:\n  {VENDORED_SHV}\\{CLASSIC_ASI_LOADER}"
        )
    ver = ""
    ver_path = VENDORED_SHV / "VERSION.txt"
    if ver_path.is_file():
        ver = ver_path.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
    edition = "Enhanced" if enhanced else "Legacy"
    _print(f"正在安装 ScriptHookV{(' (' + ver + ')') if ver else ''} [{edition}] …")
    _copy_replace(dll, gta / "ScriptHookV.dll")
    _print("  已复制 ScriptHookV.dll")

    if enhanced:
        # Classic dinput8 on Enhanced commonly blocks launch when stacked with
        # xinput1_4/dsound (or crack version.dll proxies).
        stray = gta / CLASSIC_ASI_LOADER
        if stray.is_file():
            try:
                stray.unlink()
            except PermissionError as exc:
                raise PermissionError(
                    f"无法删除冲突文件 {stray}（仍被占用）。请确认游戏已退出后重试。"
                ) from exc
            _print(f"  已移除冲突的 {CLASSIC_ASI_LOADER}（Enhanced 不能用经典加载器）")
        present = [name for name in ENHANCED_ASI_LOADERS if (gta / name).is_file()]
        if not present:
            raise FileNotFoundError(
                "检测到 GTA V Enhanced，但根目录没有 ASI 加载器。\n"
                f"  请保留游戏自带的 {ENHANCED_ASI_LOADERS[0]}（ScriptHookV Enhanced）\n"
                f"  或 {ENHANCED_ASI_LOADERS[1]}（OpenRPF），不要安装经典版 {CLASSIC_ASI_LOADER}。"
            )
        _print(f"  使用已有 Enhanced ASI 加载器: {', '.join(present)}")
        return

    _copy_replace(dinput, gta / CLASSIC_ASI_LOADER)
    _print(f"  已复制 {CLASSIC_ASI_LOADER}")


def install_asi_plugin(gta: Path, asi: Path) -> Path:
    dest = gta / "CameraPoseLogger.asi"
    _copy_replace(asi, dest)
    return dest


def install_one_gta(gta: Path, recordings_dir: Path, *, asi: Path) -> int:
    """Install camera stack into one GTA root. Returns 0 on success."""
    edition = gta_edition_label(gta)
    _print()
    _print(f"---- 安装到 [{edition}] {gta} ----")
    close_gta_processes()

    try:
        install_scripthookv_from_vendor(gta)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        _print(f"[错误] {exc}")
        return 1

    try:
        remove_shvdn_stack(gta)
        quarantine_enhanced_incompatible_mods(gta)
        dest = install_asi_plugin(gta, asi)
        cfg = write_config(gta, recordings_dir)
    except (PermissionError, OSError) as exc:
        _print(f"[错误] {exc}")
        return 1
    except Exception as exc:
        _print(f"[错误] {exc}")
        return 1

    missing: list[str] = []
    required = [
        "ScriptHookV.dll",
        "CameraPoseLogger.asi",
        "camera_pose_logger.config.json",
    ]
    if is_enhanced_gta(gta):
        if not asi_loader_present(gta):
            missing.append(" 或 ".join(ENHANCED_ASI_LOADERS))
        if (gta / CLASSIC_ASI_LOADER).is_file():
            _print(f"[错误] Enhanced 目录仍存在冲突文件 {CLASSIC_ASI_LOADER}，请删除后重试")
            return 1
    else:
        required.insert(1, CLASSIC_ASI_LOADER)
    for rel in required:
        if not (gta / rel).is_file():
            missing.append(rel)
    if missing:
        _print("[错误] 安装后校验失败，缺少：")
        for item in missing:
            _print(f"  - {item}")
        return 1

    _print(f"  相机插件: {dest}")
    _print(f"  配置文件: {cfg}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 GTA V 相机位姿采集插件")
    parser.add_argument(
        "--gta-dir",
        type=Path,
        default=None,
        help="仅安装到该目录（默认交互分别询问经典版/增强版）",
    )
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=PROJECT_ROOT / "recordings",
        help="game-recorder 输出目录（默认项目 recordings/）",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="无人值守：不询问，自动安装到检测到的全部 GTA 目录",
    )
    args = parser.parse_args(argv)

    _print("============================================================")
    _print("  GTA 相机轨迹插件安装（原生 ASI）")
    _print("============================================================")

    dirs, skipped = resolve_gta_dirs(args.gta_dir, prompt=not args.no_prompt)
    if not dirs:
        return 3 if skipped else 1

    try:
        asi = build_asi_plugin()
    except Exception as exc:
        _print(f"[错误] {exc}")
        return 1

    recordings = Path(args.recordings_dir)
    succeeded: list[Path] = []
    failed: list[Path] = []
    for gta in dirs:
        if install_one_gta(gta, recordings, asi=asi) == 0:
            succeeded.append(gta)
        else:
            failed.append(gta)

    control = recordings.resolve().parent / ".gta_camera" / "active_session.json"
    _print()
    if succeeded:
        _print("安装完成：")
        for gta in succeeded:
            _print(f"  [OK] [{gta_edition_label(gta)}] {gta}")
        _print(f"  同步信号: {control}")
        _print("  下一步: 故事模式进 GTA → run.bat 录制 → session 内应有 camera.jsonl")
    if failed:
        _print("以下目录安装失败：")
        for gta in failed:
            _print(f"  [失败] [{gta_edition_label(gta)}] {gta}")
    _print("============================================================")
    if failed and not succeeded:
        return 1
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
