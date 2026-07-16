#!/usr/bin/env python3
"""Build and transactionally install the RDR2 camera logger on Windows.

The module intentionally imports on non-Windows systems so its pure helpers can
be unit tested there.  Only ``main`` enforces the Windows-only restriction.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:
    import winreg  # type: ignore[import-not-found]
except ImportError:
    winreg = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAMERA_ROOT = PROJECT_ROOT / "rdr2-camera"
PLUGIN_PROJECT = CAMERA_ROOT / "CameraPoseLogger" / "CameraPoseLogger.vcxproj"
SDK_CACHE = PROJECT_ROOT / ".tools" / "rdr2-camera-sdk"
DOWNLOAD_CACHE = PROJECT_ROOT / ".tools" / "rdr2-camera-downloads"
STATE_DIRNAME = ".game_recorder_rdr2_camera"
STATE_FILENAME = "state.json"
STATE_SCHEMA = 1
CONTROL_DIRNAME = ".rdr2_camera"
CONTROL_FILENAME = "active_session.json"
CONFIG_FILENAME = "rdr2_camera.config.json"
BUILD_TOOLS_URL = "https://aka.ms/vs/17/release/vs_BuildTools.exe"
SCRIPT_HOOK_URL = "https://www.dev-c.com/rdr2/scripthookrdr2/"
RUNTIME_GLOB = "ScriptHookRDR2_*.zip"
SDK_GLOB = "ScriptHookRDR2_SDK_*.zip"
RUNTIME_REQUIRED = ("bin/ScriptHookRDR2.dll", "bin/dinput8.dll")
SDK_REQUIRED = ("inc/main.h", "inc/natives.h", "lib/ScriptHookRDR2.lib")
KNOWN_ARCHIVE_SHA256 = {
    "scripthookrdr2_1.0.1491.17.zip": (
        "a3be69dcd33e6cffe316d7c17ee5c4f7fcedf0c7b7fa8cf68177398e6594c39f"
    ),
    "scripthookrdr2_sdk_1.0.1207.73.zip": (
        "bddf21aa303006983fd38250b71696efa2ea51b3d7068c1dd47d083f8dd08fd7"
    ),
}
MAX_ZIP_ENTRIES = 2048
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 500
DEPLOYED_NAMES = (
    "ScriptHookRDR2.dll",
    "dinput8.dll",
    "CameraPoseLoggerRDR2.asi",
)
MANAGED_NAMES = (*DEPLOYED_NAMES, CONFIG_FILENAME)
GAME_PROCESS_NAMES = {"rdr2.exe", "playrdr2.exe"}


class InstallerError(RuntimeError):
    """Expected, user-facing installation failure."""


class InstallerSkipped(InstallerError):
    """The optional RDR2 integration was intentionally skipped."""


@dataclass(frozen=True)
class ZipPayload:
    archive: Path
    data: bytes
    members: dict[str, zipfile.ZipInfo]


def _print(message: str = "") -> None:
    print(message, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        expanded = path.expanduser()
        try:
            normalized = expanded.resolve() if expanded.exists() else expanded
        except OSError:
            normalized = expanded
        key = os.path.normcase(str(normalized)).casefold()
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _registry_values(
    hive: Any, subkey: str, names: Iterable[str]
) -> list[str]:
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
            with winreg.OpenKey(
                hive, subkey, 0, winreg.KEY_READ | view
            ) as key:
                for name in names:
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    if value:
                        values.append(os.path.expandvars(str(value)).strip('"'))
        except OSError:
            continue
    return values


def _steam_roots() -> list[Path]:
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
    return _unique_paths(roots)


def _steam_libraries(steam_root: Path) -> list[Path]:
    libraries = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return libraries
    for match in re.finditer(r'"path"\s+"([^"]+)"', text, re.IGNORECASE):
        libraries.append(Path(match.group(1).replace("\\\\", "\\")))
    return _unique_paths(libraries)


def _registered_rdr2_locations() -> list[Path]:
    if winreg is None:
        return []
    locations: list[Path] = []
    hives = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    keys = (
        (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion"
            r"\Uninstall\Steam App 1174180",
            ("InstallLocation",),
        ),
        (
            r"SOFTWARE\Rockstar Games\Red Dead Redemption 2",
            ("InstallFolder", "Install Folder", "InstallPath"),
        ),
        (
            r"SOFTWARE\Rockstar Games\RDR2",
            ("InstallFolder", "Install Folder", "InstallPath"),
        ),
    )
    for hive in hives:
        for key, names in keys:
            locations.extend(Path(value) for value in _registry_values(hive, key, names))
        uninstall_base = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        )
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
                    subkeys = [winreg.EnumKey(uninstall, index) for index in range(count)]
            except OSError:
                continue
            for subkey in subkeys:
                full_key = uninstall_base + "\\" + subkey
                display_names = _registry_values(hive, full_key, ("DisplayName",))
                if not any(
                    "red dead redemption 2" in name.casefold()
                    for name in display_names
                ):
                    continue
                locations.extend(
                    Path(value)
                    for value in _registry_values(
                        hive, full_key, ("InstallLocation", "InstallPath")
                    )
                )
    return locations


def is_rdr2_dir(path: Path) -> bool:
    return (path / "RDR2.exe").is_file()


def find_rdr2_candidates() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("RDR2_DIR", "RED_DEAD_REDEMPTION_2_DIR"):
        value = os.environ.get(env_name, "").strip().strip('"')
        if value:
            candidates.append(Path(value))
    candidates.extend(_registered_rdr2_locations())
    for steam_root in _steam_roots():
        for library in _steam_libraries(steam_root):
            candidates.append(
                library / "steamapps" / "common" / "Red Dead Redemption 2"
            )
    program_files = [
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        r"C:\Program Files",
    ]
    for root in filter(None, program_files):
        candidates.extend(
            (
                Path(root) / "Rockstar Games" / "Red Dead Redemption 2",
                Path(root) / "Rockstar Games" / "RDR2",
            )
        )
    return [path.resolve() for path in _unique_paths(candidates) if is_rdr2_dir(path)]


def resolve_rdr2_dir(explicit: Path | None, *, prompt: bool) -> Path:
    if explicit is not None:
        if is_rdr2_dir(explicit):
            return explicit.resolve()
        if not prompt:
            raise InstallerError(f"不是有效的 RDR2 目录（缺少 RDR2.exe）：{explicit}")
        _print(f"[错误] 目录中没有 RDR2.exe：{explicit}")

    candidates = find_rdr2_candidates()
    if len(candidates) == 1 and explicit is None:
        return candidates[0]
    if candidates:
        if not prompt:
            _print(f"[自动] 使用检测到的 RDR2：{candidates[0]}")
            return candidates[0]
        _print("检测到 RDR2 安装：")
        for index, path in enumerate(candidates, 1):
            _print(f"  [{index}] {path}")
        answer = input(
            f"选择 [1-{len(candidates)}]，或输入完整路径："
        ).strip().strip('"')
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        chosen = Path(answer) if answer else candidates[0]
        if is_rdr2_dir(chosen):
            return chosen.resolve()
        raise InstallerError(f"目录中没有 RDR2.exe：{chosen}")

    if not prompt:
        raise InstallerSkipped("未找到 RDR2；已跳过相机插件安装")
    answer = input(
        "请输入 RDR2 游戏根目录（含 RDR2.exe；直接回车跳过）："
    ).strip().strip('"')
    if not answer:
        raise InstallerSkipped("用户跳过 RDR2 相机插件安装")
    chosen = Path(answer)
    if not is_rdr2_dir(chosen):
        raise InstallerError(f"目录中没有 RDR2.exe：{chosen}")
    return chosen.resolve()


def _zip_search_dirs() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    return _unique_paths(
        (
            home / "Desktop",
            home / "Downloads",
            Path.home() / "Desktop",
            Path.home() / "Downloads",
        )
    )


def resolve_zip(
    explicit: Path | None,
    *,
    pattern: str,
    label: str,
    prompt: bool,
) -> Path:
    if explicit is not None:
        if explicit.is_file():
            return explicit.resolve()
        raise InstallerError(f"{label} ZIP 不存在：{explicit}")
    matches: list[Path] = []
    for directory in _zip_search_dirs():
        if directory.is_dir():
            matches.extend(path for path in directory.glob(pattern) if path.is_file())
    if pattern == RUNTIME_GLOB:
        matches = [
            path
            for path in matches
            if not path.name.casefold().startswith("scripthookrdr2_sdk_")
        ]
    matches = sorted(_unique_paths(matches), key=lambda path: path.stat().st_mtime, reverse=True)
    if matches:
        _print(f"[自动] 找到 {label} ZIP：{matches[0]}")
        return matches[0].resolve()
    _print(f"未找到 {label} ZIP。官方下载：{SCRIPT_HOOK_URL}")
    if not prompt:
        raise InstallerError(f"缺少 {label} ZIP；请下载后用命令行显式指定")
    answer = input(f"请输入 {label} ZIP 路径：").strip().strip('"')
    path = Path(answer)
    if not path.is_file():
        raise InstallerError(f"{label} ZIP 不存在：{path}")
    return path.resolve()


def _safe_member_name(info: zipfile.ZipInfo) -> str:
    raw = info.filename.replace("\\", "/")
    pure = PurePosixPath(raw)
    parts = raw.rstrip("/").split("/")
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    if (
        not raw
        or "\x00" in raw
        or pure.is_absolute()
        or re.match(r"^[A-Za-z]:", raw)
        or any(part in ("", ".", "..") for part in parts)
        or any(":" in part or part.endswith((" ", ".")) for part in parts)
        or any(part.split(".", 1)[0].casefold() in reserved for part in parts)
    ):
        raise InstallerError(f"ZIP 含不安全路径：{info.filename!r}")
    # Unix symlinks can be embedded even in archives produced on Windows.
    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
        raise InstallerError(f"ZIP 含符号链接，拒绝解压：{info.filename}")
    return pure.as_posix().rstrip("/")


def read_archive_snapshot(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_ARCHIVE_BYTES:
            raise InstallerError(
                f"ZIP 文件过大：{size} > {MAX_ARCHIVE_BYTES} bytes"
            )
        with path.open("rb") as stream:
            data = stream.read(MAX_ARCHIVE_BYTES + 1)
    except OSError as exc:
        raise InstallerError(f"无法读取 ZIP {path}：{exc}") from exc
    if len(data) > MAX_ARCHIVE_BYTES:
        raise InstallerError("读取 ZIP 时超过安全大小限制")
    return data


def validate_zip(
    path: Path, required: Iterable[str], *, data: bytes | None = None
) -> ZipPayload:
    snapshot = read_archive_snapshot(path) if data is None else data
    try:
        with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise InstallerError(
                    f"ZIP 条目过多：{len(infos)} > {MAX_ZIP_ENTRIES}"
                )
            members: dict[str, zipfile.ZipInfo] = {}
            total_bytes = 0
            for info in infos:
                name = _safe_member_name(info)
                if info.is_dir():
                    continue
                if info.file_size > MAX_ZIP_MEMBER_BYTES:
                    raise InstallerError(f"ZIP 条目过大：{name}")
                total_bytes += info.file_size
                if total_bytes > MAX_ZIP_TOTAL_BYTES:
                    raise InstallerError("ZIP 解压后总大小超过安全限制")
                if (
                    info.file_size > 0
                    and info.file_size / max(1, info.compress_size)
                    > MAX_ZIP_COMPRESSION_RATIO
                ):
                    raise InstallerError(f"ZIP 条目压缩比异常：{name}")
                key = name.casefold()
                if key in members:
                    raise InstallerError(f"ZIP 含大小写冲突或重复条目：{name}")
                members[key] = info
            missing = [name for name in required if name.casefold() not in members]
            if missing:
                raise InstallerError(
                    f"ZIP {path.name} 缺少必需条目：" + "、".join(missing)
                )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, InstallerError):
            raise
        raise InstallerError(f"无法校验 ZIP {path}：{exc}") from exc
    return ZipPayload(path, snapshot, members)


def verify_archive_trust(
    path: Path,
    *,
    prompt: bool,
    allow_unknown: bool,
    data: bytes | None = None,
) -> str:
    snapshot = read_archive_snapshot(path) if data is None else data
    digest = hashlib.sha256(snapshot).hexdigest()
    expected = KNOWN_ARCHIVE_SHA256.get(path.name.casefold())
    if expected is not None and digest == expected:
        _print(f"ZIP SHA-256 已匹配内置官方版本：{path.name}")
        return digest
    if expected is not None:
        raise InstallerError(
            f"ZIP SHA-256 与已知官方版本不符：{path.name}\n"
            f"  expected: {expected}\n  actual:   {digest}"
        )
    if allow_unknown:
        _print(f"[警告] 使用未收录的新版本 ZIP：{path.name}\n  SHA-256: {digest}")
        return digest
    if not prompt:
        raise InstallerError(
            f"ZIP 版本未收录，无法在无人值守模式确认：{path.name}；"
            "核实来自官网后传入 --allow-unknown-zip"
        )
    _print(f"[警告] ZIP 版本未收录：{path}\n  SHA-256: {digest}")
    answer = input("确认它直接下载自 dev-c.com 请输入大写 YES：").strip()
    if answer != "YES":
        raise InstallerError("未确认未知 ZIP，已取消安装")
    return digest


def read_zip_member(payload: ZipPayload, member: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(payload.data)) as archive:
            return archive.read(payload.members[member.casefold()])
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise InstallerError(f"无法读取 {payload.archive.name}/{member}：{exc}") from exc


def extract_zip_safely(payload: ZipPayload, destination: Path) -> None:
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{destination.name}.extract-{uuid.uuid4().hex}"
    old = parent / f".{destination.name}.old-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        with zipfile.ZipFile(io.BytesIO(payload.data)) as archive:
            for info in payload.members.values():
                relative = Path(*PurePosixPath(_safe_member_name(info)).parts)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        for required in SDK_REQUIRED:
            if not (staging / Path(*PurePosixPath(required).parts)).is_file():
                raise InstallerError(f"SDK 解压后缺少：{required}")
        if destination.exists():
            os.replace(destination, old)
        os.replace(staging, destination)
        shutil.rmtree(old, ignore_errors=True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if old.exists() and not destination.exists():
            os.replace(old, destination)
        raise


def validate_pe_x64(path: Path | None = None, *, data: bytes | None = None, label: str = "PE") -> None:
    if data is None:
        if path is None:
            raise ValueError("path or data is required")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise InstallerError(f"无法读取 {label}：{exc}") from exc
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise InstallerError(f"{label} 不是有效 PE 文件")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 26 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise InstallerError(f"{label} 的 PE 头无效")
    machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
    optional_magic = struct.unpack_from("<H", data, pe_offset + 24)[0]
    if machine != 0x8664 or optional_magic != 0x20B:
        raise InstallerError(
            f"{label} 不是 x64 PE（machine=0x{machine:04x}, magic=0x{optional_magic:04x}）"
        )


def running_game_processes() -> list[str]:
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallerError(f"无法检查 RDR2 进程：{exc}") from exc
    if result.returncode:
        raise InstallerError(
            "无法检查 RDR2 进程：" + (result.stderr.strip() or str(result.returncode))
        )
    found = {
        row[0].strip()
        for row in csv.reader(io.StringIO(result.stdout))
        if row and row[0].strip().casefold() in GAME_PROCESS_NAMES
    }
    return sorted(found, key=str.casefold)


def ensure_game_closed() -> None:
    processes = running_game_processes()
    if processes:
        raise InstallerError(
            "RDR2 仍在运行（" + "、".join(processes) + "）；请完全退出游戏后重试"
        )


def _can_write(path: Path) -> bool:
    directory = path if path.is_dir() else path.parent
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=True):
            pass
        if path.exists() and path.is_file():
            with path.open("ab"):
                pass
        return True
    except OSError:
        return False


def needs_elevation(game_dir: Path) -> bool:
    return os.name == "nt" and (
        not _can_write(game_dir)
        or any(not _can_write(game_dir / name) for name in MANAGED_NAMES)
        or not _can_write(game_dir / STATE_DIRNAME)
    )


def elevate_and_wait(script: Path, argv: list[str], game_dir: Path) -> int:
    if os.name != "nt":
        raise InstallerError("UAC 提权只适用于 Windows")
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    parameters = subprocess.list2cmdline(
        [
            str(script),
            *argv,
            "--rdr2-dir",
            str(game_dir),
            "--skip-elevation",
        ]
    )
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    execute = shell32.ShellExecuteExW
    execute.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    execute.restype = wintypes.BOOL
    wait = kernel32.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = parameters
    info.lpDirectory = str(PROJECT_ROOT)
    info.nShow = 1
    ctypes.set_last_error(0)
    if not execute(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:
            raise InstallerError("用户取消了 UAC 提权")
        raise InstallerError(f"无法启动 UAC 子进程（Windows 错误 {error}）")
    if not info.hProcess:
        raise InstallerError("UAC 子进程未返回进程句柄")
    try:
        wait_result = wait(info.hProcess, 0xFFFFFFFF)
        if wait_result != 0:
            raise InstallerError(f"等待 UAC 子进程失败（代码 {wait_result}）")
        exit_code = wintypes.DWORD()
        if not get_exit_code(info.hProcess, ctypes.byref(exit_code)):
            raise InstallerError("无法取得 UAC 子进程退出码")
        return int(exit_code.value)
    finally:
        close(info.hProcess)


def find_vswhere() -> Path | None:
    from_path = shutil.which("vswhere.exe") or shutil.which("vswhere")
    candidates = [Path(from_path)] if from_path else []
    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(env_name, "").strip()
        if root:
            candidates.append(
                Path(root) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
            )
    return next((path for path in _unique_paths(candidates) if path.is_file()), None)


def find_msbuild() -> Path | None:
    vswhere = find_vswhere()
    if vswhere is None:
        return None
    command = [
        str(vswhere),
        "-latest",
        "-products",
        "*",
        "-requires",
        "Microsoft.Component.MSBuild",
        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
        "-property",
        "installationPath",
    ]
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    for line in result.stdout.splitlines():
        root = Path(line.strip())
        for relative in (
            Path("MSBuild") / "Current" / "Bin" / "MSBuild.exe",
            Path("MSBuild") / "15.0" / "Bin" / "MSBuild.exe",
        ):
            candidate = root / relative
            if candidate.is_file():
                return candidate
    return None


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    request = urllib.request.Request(
        url, headers={"User-Agent": "game-recorder-rdr2-camera-installer"}
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_msbuild() -> Path:
    existing = find_msbuild()
    if existing is not None:
        return existing
    installer = DOWNLOAD_CACHE / "vs_BuildTools.exe"
    _print("未找到 Visual C++ Build Tools，正在下载官方安装器 …")
    try:
        _download(BUILD_TOOLS_URL, installer)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise InstallerError(f"Build Tools 下载失败：{exc}") from exc
    command = [
        str(installer),
        "--quiet",
        "--wait",
        "--norestart",
        "--nocache",
        "--add",
        "Microsoft.VisualStudio.Workload.VCTools",
        "--includeRecommended",
    ]
    _print("正在静默安装 Microsoft.VisualStudio.Workload.VCTools …")
    result = subprocess.run(command, check=False)
    if result.returncode not in (0, 3010):
        raise InstallerError(f"Build Tools 安装失败（exit {result.returncode}）")
    discovered = find_msbuild()
    if discovered is None:
        raise InstallerError("Build Tools 安装完成，但仍无法通过 vswhere 找到 MSBuild")
    return discovered


def build_plugin(msbuild: Path, sdk_dir: Path) -> Path:
    if not PLUGIN_PROJECT.is_file():
        raise InstallerError(f"缺少插件工程：{PLUGIN_PROJECT}")
    output = (
        PLUGIN_PROJECT.parent
        / "bin"
        / "Release"
        / "RDR2CameraPoseLogger.asi"
    )
    output.unlink(missing_ok=True)
    command = [
        str(msbuild),
        str(PLUGIN_PROJECT),
        "/m",
        "/t:Build",
        "/p:Configuration=Release",
        "/p:Platform=x64",
        f"/p:SDK_ROOT={sdk_dir}",
        f"/p:RDR2SDKDir={sdk_dir}",
        "/nologo",
    ]
    result = subprocess.run(command, cwd=str(PLUGIN_PROJECT.parent), check=False)
    if result.returncode:
        raise InstallerError(f"CameraPoseLogger 构建失败（exit {result.returncode}）")
    if not output.is_file():
        raise InstallerError(f"MSBuild 成功，但未找到预期产物：{output}")
    validate_pe_x64(output, label=output.name)
    return output


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_document_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _transaction_targets(
    game_dir: Path, control_file: Path
) -> dict[str, Path]:
    targets = {
        f"managed:{name}": game_dir / name for name in MANAGED_NAMES
    }
    targets["state"] = game_dir / STATE_DIRNAME
    targets["control"] = control_file
    return targets


class InstallTransaction:
    """Persisted pre-change snapshot used for normal and interrupted rollback."""

    def __init__(self, game_dir: Path, control_file: Path) -> None:
        self.game_dir = game_dir
        self.control_file = control_file
        self.state_dir = game_dir / STATE_DIRNAME
        self.directory = game_dir / f"{STATE_DIRNAME}.transaction-{uuid.uuid4().hex}"
        targets = _transaction_targets(game_dir, control_file)
        for target in targets.values():
            if target.exists() and _is_reparse_point(target):
                raise InstallerError(f"安装目标不能是重解析点：{target}")
        self.directory.mkdir()
        manifest: dict[str, Any] = {
            "schema": 1,
            "control_file": str(control_file),
            "targets": {},
        }
        try:
            for index, (target_id, target) in enumerate(targets.items()):
                present = target.exists()
                manifest["targets"][target_id] = {
                    "present": present,
                    "snapshot": str(index),
                    "directory": target.is_dir() and not target.is_symlink(),
                }
                if present:
                    snapshot = self.directory / str(index)
                    if target.is_dir() and not target.is_symlink():
                        shutil.copytree(target, snapshot)
                    elif target.is_file() and not target.is_symlink():
                        shutil.copy2(target, snapshot)
                    else:
                        raise InstallerError(
                            f"安装目标不是普通文件/目录：{target}"
                        )
            _write_json_atomic(self.directory / "manifest.json", manifest)
            (self.directory / "PREPARED").write_text(
                "prepared\n", encoding="ascii"
            )
        except Exception:
            shutil.rmtree(self.directory, ignore_errors=True)
            raise

    def rollback(self) -> None:
        restore_transaction(self.directory, self.game_dir, self.control_file)

    def commit(self) -> None:
        (self.directory / "COMMITTED").write_text("committed\n", encoding="ascii")
        shutil.rmtree(self.directory)


def restore_transaction(
    directory: Path, game_dir: Path, expected_control_file: Path
) -> None:
    if (
        directory.is_symlink()
        or _is_reparse_point(directory)
        or directory.parent.resolve() != game_dir.resolve()
    ):
        raise InstallerError(f"事务目录不安全：{directory}")
    try:
        manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError(f"事务快照损坏，无法回滚：{directory}: {exc}") from exc
    targets = manifest.get("targets")
    if manifest.get("schema") != 1 or not isinstance(targets, dict):
        raise InstallerError(f"事务快照格式无效：{directory}")
    saved_control = manifest.get("control_file")
    if (
        not isinstance(saved_control, str)
        or Path(saved_control).resolve() != expected_control_file.resolve()
    ):
        raise InstallerError("事务控制文件与当前安装目标不一致")
    expected_targets = _transaction_targets(game_dir, expected_control_file)
    if set(targets) != set(expected_targets):
        raise InstallerError("事务目标集合不完整或包含未知目标")
    used_snapshots: set[str] = set()
    validated: list[tuple[Path, bool, bool, Path | None]] = []
    for target_id, target in expected_targets.items():
        item = targets[target_id]
        if not isinstance(item, dict):
            raise InstallerError(f"事务快照条目无效：{target_id}")
        present = item.get("present")
        is_directory = item.get("directory")
        snapshot_name = item.get("snapshot")
        if (
            not isinstance(present, bool)
            or not isinstance(is_directory, bool)
            or not isinstance(snapshot_name, str)
            or not snapshot_name.isdigit()
            or snapshot_name in used_snapshots
        ):
            raise InstallerError(f"事务快照元数据无效：{target_id}")
        used_snapshots.add(snapshot_name)
        if target.exists() and _is_reparse_point(target):
            raise InstallerError(f"拒绝删除重解析点目标：{target}")
        snapshot: Path | None = None
        if present:
            snapshot = directory / snapshot_name
            if (
                not snapshot.exists()
                or snapshot.is_symlink()
                or _is_reparse_point(snapshot)
                or snapshot.parent.resolve() != directory.resolve()
                or snapshot.is_dir() != is_directory
            ):
                raise InstallerError(f"事务快照缺失或类型不符：{target_id}")
        validated.append((target, present, is_directory, snapshot))

    # Do not mutate any target until the complete snapshot has been validated.
    for target, present, is_directory, snapshot in validated:
        _remove(target)
        if present:
            assert snapshot is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            if is_directory:
                shutil.copytree(snapshot, target)
            else:
                shutil.copy2(snapshot, target)
    shutil.rmtree(directory)


def recover_interrupted_transaction(
    game_dir: Path, expected_control_file: Path
) -> None:
    transactions = sorted(game_dir.glob(f"{STATE_DIRNAME}.transaction-*"))
    if len(transactions) > 1:
        raise InstallerError("检测到多个未完成事务，拒绝猜测恢复顺序")
    if not transactions:
        return
    transaction = transactions[0]
    if transaction.is_symlink() or _is_reparse_point(transaction):
        raise InstallerError(f"未完成事务目录不能是重解析点：{transaction}")
    if (transaction / "COMMITTED").is_file():
        shutil.rmtree(transaction)
    elif (transaction / "PREPARED").is_file():
        restore_transaction(transaction, game_dir, expected_control_file)
        _print("[恢复] 已回滚上次中断的 RDR2 相机安装。")
    else:
        shutil.rmtree(transaction)


def _read_existing_state(state_file: Path) -> dict[str, Any] | None:
    if not state_file.exists():
        return None
    try:
        value = json.loads(state_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError(f"现有安装状态损坏：{exc}") from exc
    if not isinstance(value, dict) or value.get("state_schema") != STATE_SCHEMA:
        raise InstallerError("现有安装状态版本无效，拒绝覆盖")
    return value


def _verify_managed_files_unchanged(
    game_dir: Path, existing_state: dict[str, Any] | None
) -> None:
    """Refuse to overwrite managed files changed after the previous install."""
    if existing_state is None:
        return
    hashes = existing_state.get("hashes")
    if not isinstance(hashes, dict):
        raise InstallerError("现有状态缺少已安装文件哈希，拒绝覆盖")
    for name in MANAGED_NAMES:
        expected = hashes.get(name)
        destination = game_dir / name
        if (
            not isinstance(expected, str)
            or not destination.is_file()
            or sha256_file(destination) != expected
        ):
            raise InstallerError(f"受管文件已被修改或删除，拒绝覆盖：{name}")


def _check_first_install_conflicts(
    game_dir: Path,
    expected_hashes: dict[str, str],
    *,
    force_existing: bool,
) -> None:
    for name, expected_hash in expected_hashes.items():
        destination = game_dir / name
        if not destination.exists():
            continue
        if (
            destination.is_file()
            and not _is_reparse_point(destination)
            and sha256_file(destination) == expected_hash
        ):
            continue
        if not force_existing:
            raise InstallerError(
                f"检测到未知或不同版本的现有文件：{destination}；"
                "为避免破坏其他 mod，默认不会覆盖。确认备份后可传入 --force-existing"
            )


def _validate_existing_control(
    control_file: Path,
    *,
    existing_state: dict[str, Any] | None,
    force_existing: bool,
) -> bool:
    """Return True when an invalid first-install control file may be reset."""
    if not control_file.exists():
        return False
    if (
        not control_file.is_file()
        or control_file.is_symlink()
        or _is_reparse_point(control_file)
    ):
        raise InstallerError(f"同步控制路径不是普通文件：{control_file}")
    if not _can_write(control_file) or not _can_write(control_file.parent):
        raise InstallerError(f"同步控制文件或其目录不可写：{control_file}")
    try:
        value = json.loads(control_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        value = None
    if isinstance(value, dict) and value.get("status") == "idle":
        return False
    if existing_state is not None or not force_existing:
        raise InstallerError(
            "现有同步控制文件不是安全的 idle 状态；"
            "请先停止录制。首次接管未知文件时可传入 --force-existing"
        )
    return True


def install_payload(
    game_dir: Path,
    recordings_dir: Path,
    runtime_payload: ZipPayload,
    plugin: Path,
    *,
    force_existing: bool = False,
) -> tuple[Path, Path]:
    state_dir = game_dir / STATE_DIRNAME
    state_file = state_dir / STATE_FILENAME
    control_file = recordings_dir.resolve().parent / CONTROL_DIRNAME / CONTROL_FILENAME
    recover_interrupted_transaction(game_dir, control_file)
    existing_state = _read_existing_state(state_file)
    if state_dir.exists() and existing_state is None:
        raise InstallerError(f"状态目录存在但缺少有效状态文件：{state_dir}")
    if existing_state is not None:
        saved_root = existing_state.get("game_root")
        if not isinstance(saved_root, str):
            raise InstallerError("现有状态缺少 game_root")
        try:
            same_root = Path(saved_root).resolve() == game_dir.resolve()
        except OSError as exc:
            raise InstallerError(f"现有状态中的 game_root 无效：{exc}") from exc
        if not same_root:
            raise InstallerError("现有安装状态属于另一个 RDR2 目录，拒绝覆盖")
        if not isinstance(existing_state.get("hashes"), dict):
            raise InstallerError("现有状态缺少已安装文件哈希，拒绝覆盖")
    _verify_managed_files_unchanged(game_dir, existing_state)
    reset_control = _validate_existing_control(
        control_file,
        existing_state=existing_state,
        force_existing=force_existing,
    )

    runtime_data = {
        "ScriptHookRDR2.dll": read_zip_member(
            runtime_payload, "bin/ScriptHookRDR2.dll"
        ),
        "dinput8.dll": read_zip_member(runtime_payload, "bin/dinput8.dll"),
    }
    for name, data in runtime_data.items():
        validate_pe_x64(data=data, label=name)
    validate_pe_x64(plugin, label=plugin.name)
    source_hashes = {
        name: hashlib.sha256(data).hexdigest() for name, data in runtime_data.items()
    }
    source_hashes["CameraPoseLoggerRDR2.asi"] = sha256_file(plugin)
    config = {
        "control_file": control_file.resolve().as_posix(),
        "poll_interval_ms": 100,
        "flush_every_samples": 30,
    }
    source_hashes[CONFIG_FILENAME] = hashlib.sha256(
        _json_document_bytes(config)
    ).hexdigest()
    if existing_state is None:
        _check_first_install_conflicts(
            game_dir, source_hashes, force_existing=force_existing
        )
    transaction = InstallTransaction(game_dir, control_file)
    try:
        if existing_state is None:
            state_dir.mkdir()
            backup_dir = state_dir / "backups" / "original"
            backup_dir.mkdir(parents=True)
            original: dict[str, Any] = {}
            for name in MANAGED_NAMES:
                destination = game_dir / name
                if destination.exists():
                    if not destination.is_file():
                        raise InstallerError(f"现有目标不是普通文件：{destination}")
                    shutil.copy2(destination, backup_dir / name)
                    original[name] = {
                        "present": True,
                        "sha256": sha256_file(destination),
                        "backup": str(backup_dir / name),
                    }
                else:
                    original[name] = {"present": False}
            if reset_control:
                control_backup = backup_dir / CONTROL_FILENAME
                shutil.copy2(control_file, control_backup)
                original["control_file"] = {
                    "present": True,
                    "sha256": sha256_file(control_file),
                    "backup": str(control_backup),
                }
            else:
                original["control_file"] = {
                    "present": control_file.exists(),
                    "preserved": control_file.exists(),
                }
            first_installed = datetime.now(timezone.utc).isoformat()
        else:
            original_value = existing_state.get("original_files")
            if not isinstance(original_value, dict):
                raise InstallerError("现有状态缺少原始文件备份信息")
            original = original_value
            first_installed = str(existing_state.get("first_installed_at", ""))

        for name, data in runtime_data.items():
            (game_dir / name).write_bytes(data)
        shutil.copy2(plugin, game_dir / "CameraPoseLoggerRDR2.asi")
        _write_json_atomic(game_dir / CONFIG_FILENAME, config)
        if reset_control or not control_file.exists():
            _write_json_atomic(
                control_file,
                {"status": "idle", "updated_at_ms": 0},
            )
        installed_hashes = {
            name: sha256_file(game_dir / name) for name in MANAGED_NAMES
        }
        if any(
            installed_hashes.get(name) != expected
            for name, expected in source_hashes.items()
        ):
            raise InstallerError("安装后哈希校验失败")
        try:
            installed_config = json.loads(
                (game_dir / CONFIG_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InstallerError(f"安装后配置校验失败：{exc}") from exc
        if installed_config != config:
            raise InstallerError("安装后的 RDR2 配置内容不符")
        state = {
            "state_schema": STATE_SCHEMA,
            "first_installed_at": first_installed,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "game_root": str(game_dir),
            "runtime_zip": str(runtime_payload.archive),
            "runtime_zip_sha256": hashlib.sha256(
                runtime_payload.data
            ).hexdigest(),
            "plugin_source": str(plugin),
            "control_file": str(control_file),
            "managed_files": list(MANAGED_NAMES),
            "hashes": installed_hashes,
            "original_files": original,
        }
        _write_json_atomic(state_file, state)
        transaction.commit()
    except Exception as exc:
        try:
            transaction.rollback()
        except Exception as rollback_exc:
            raise InstallerError(
                f"安装失败：{exc}；回滚失败，事务快照保留在 {transaction.directory}："
                f"{rollback_exc}"
            ) from exc
        raise InstallerError(f"安装失败，已事务回滚：{exc}") from exc
    return state_file, control_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安装 RDR2 相机位姿采集插件")
    parser.add_argument("--rdr2-dir", type=Path, help="RDR2 游戏根目录")
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=PROJECT_ROOT / "recordings",
        help="game-recorder recordings 目录",
    )
    parser.add_argument("--runtime-zip", type=Path, help="ScriptHookRDR2 官方 ZIP")
    parser.add_argument("--sdk-zip", type=Path, help="ScriptHookRDR2 SDK 官方 ZIP")
    parser.add_argument(
        "--allow-unknown-zip",
        action="store_true",
        help="允许使用未收录 SHA-256 的新版官方 ZIP",
    )
    parser.add_argument(
        "--force-existing",
        action="store_true",
        help="备份并覆盖首次安装前已存在的同名文件",
    )
    parser.add_argument("--no-prompt", action="store_true", help="禁止交互输入")
    parser.add_argument("--skip-elevation", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    original_argv = list(sys.argv[1:] if argv is None else argv)
    _print("============================================================")
    _print("  RDR2 相机插件自动安装")
    _print("============================================================")
    if os.name != "nt":
        _print("[错误] 此安装器只能在 Windows 上运行。")
        return 1
    try:
        game_dir = resolve_rdr2_dir(args.rdr2_dir, prompt=not args.no_prompt)
        runtime_zip = resolve_zip(
            args.runtime_zip,
            pattern=RUNTIME_GLOB,
            label="ScriptHookRDR2 运行时",
            prompt=not args.no_prompt,
        )
        sdk_zip = resolve_zip(
            args.sdk_zip,
            pattern=SDK_GLOB,
            label="ScriptHookRDR2 SDK",
            prompt=not args.no_prompt,
        )
        runtime_snapshot = read_archive_snapshot(runtime_zip)
        sdk_snapshot = read_archive_snapshot(sdk_zip)
        verify_archive_trust(
            runtime_zip,
            prompt=not args.no_prompt,
            allow_unknown=args.allow_unknown_zip,
            data=runtime_snapshot,
        )
        verify_archive_trust(
            sdk_zip,
            prompt=not args.no_prompt,
            allow_unknown=args.allow_unknown_zip,
            data=sdk_snapshot,
        )
        runtime_payload = validate_zip(
            runtime_zip, RUNTIME_REQUIRED, data=runtime_snapshot
        )
        sdk_payload = validate_zip(
            sdk_zip, SDK_REQUIRED, data=sdk_snapshot
        )
        validate_pe_x64(game_dir / "RDR2.exe", label="RDR2.exe")
        for member in RUNTIME_REQUIRED:
            validate_pe_x64(
                data=read_zip_member(runtime_payload, member), label=member
            )
        ensure_game_closed()
        needs_build_tools = find_msbuild() is None
        if not args.skip_elevation and (
            needs_elevation(game_dir) or needs_build_tools
        ):
            reason = (
                "安装 C++ Build Tools"
                if needs_build_tools
                else "写入游戏目录"
            )
            _print(f"{reason}需要管理员权限，正在请求 UAC …")
            return elevate_and_wait(Path(__file__).resolve(), original_argv, game_dir)
        extract_zip_safely(sdk_payload, SDK_CACHE)
        msbuild = ensure_msbuild()
        _print(f"MSBuild：{msbuild}")
        plugin = build_plugin(msbuild, SDK_CACHE)
        state_file, control_file = install_payload(
            game_dir,
            args.recordings_dir,
            runtime_payload,
            plugin,
            force_existing=args.force_existing,
        )
    except InstallerSkipped as exc:
        _print(f"[跳过] {exc}")
        return 3
    except Exception as exc:
        _print(f"[错误] {exc}")
        return 1
    _print()
    _print("[成功] RDR2 相机插件安装完成。")
    _print(f"  游戏目录：{game_dir}")
    _print(f"  状态文件：{state_file}")
    _print(f"  同步信号：{control_file}")
    _print("  安装器未启动游戏。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
