#!/usr/bin/env python3
"""Install the bundled Black Myth: Wukong UE4SS camera payload on Windows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:
    import winreg  # type: ignore[import-not-found]
except ImportError:  # Non-Windows: importing the module and --help must still work.
    winreg = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAMERA_ROOT = PROJECT_ROOT / "wukong-camera"
MANIFEST_PATH = CAMERA_ROOT / "payload-manifest.json"
EXPECTED_EXE_BYTES = 728_458_376
GAME_EXE_REL = Path("b1") / "Binaries" / "Win64" / "b1-Win64-Shipping.exe"
STATE_DIRNAME = ".game_recorder_wukong_camera"
STATE_FILENAME = "state.json"
STATE_SCHEMA = 3
CONTROL_DIRNAME = ".wukong_camera"
CONTROL_FILENAME = "active_session.json"
PAYLOAD_PREFIX = "payload"
GAME_PROCESS_NAMES = {"b1.exe", "b1-win64-shipping.exe"}
MODS_TXT_RELATIVE = Path("ue4ss") / "Mods" / "mods.txt"
LEGACY_LOADER_FILENAME = "xinput1_3.dll"


class InstallerError(RuntimeError):
    """An expected, user-facing installer failure."""


@dataclass(frozen=True)
class GameLayout:
    root: Path
    win64: Path
    exe: Path


@dataclass(frozen=True)
class PayloadFile:
    manifest_path: str
    source: Path
    destination_relative: Path
    byte_count: int
    sha256: str
    install_bytes: bytes | None = None


@dataclass(frozen=True)
class ResolveResult:
    layout: GameLayout | None
    skipped: bool


def _print(message: str = "") -> None:
    print(message, flush=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_payload_bytes(
    source: Path,
    *,
    expected_bytes: int,
    expected_sha: str,
) -> bytes | None:
    """Accept only a line-ending variant that canonicalizes to the manifest."""
    if source.suffix.casefold() not in {".ini", ".lua", ".txt"}:
        return None
    raw = source.read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    for candidate in (lf, crlf):
        if (
            candidate != raw
            and len(candidate) == expected_bytes
            and _sha256_bytes(candidate).casefold() == expected_sha.casefold()
        ):
            return candidate
    return None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def resolve_game_layout(path: Path) -> GameLayout | None:
    """Accept either the game root or its b1/Binaries/Win64 directory."""
    candidate = path.expanduser()
    layouts = (
        (
            candidate,
            candidate / "b1" / "Binaries" / "Win64",
            candidate / GAME_EXE_REL,
        ),
        (
            candidate.parents[2] if len(candidate.parents) >= 3 else candidate,
            candidate,
            candidate / "b1-Win64-Shipping.exe",
        ),
    )
    for root, win64, exe in layouts:
        if not exe.is_file():
            continue
        try:
            root_resolved = root.resolve()
            win64_resolved = win64.resolve()
            exe_resolved = exe.resolve()
        except OSError:
            continue
        expected_win64 = (root_resolved / "b1" / "Binaries" / "Win64").resolve()
        expected_exe = (expected_win64 / "b1-Win64-Shipping.exe").resolve()
        if win64_resolved != expected_win64 or exe_resolved != expected_exe:
            continue
        if not _is_within(win64_resolved, root_resolved):
            continue
        return GameLayout(root=root_resolved, win64=win64_resolved, exe=exe_resolved)
    return None


def _registry_values(
    hive: Any,
    subkey: str,
    names: Iterable[str],
) -> list[str]:
    if winreg is None:
        return []
    values: list[str] = []
    views = [0]
    for attr in ("KEY_WOW64_32KEY", "KEY_WOW64_64KEY"):
        value = getattr(winreg, attr, 0)
        if value and value not in views:
            views.append(value)
    for view in views:
        try:
            with winreg.OpenKey(  # type: ignore[union-attr]
                hive,
                subkey,
                0,
                winreg.KEY_READ | view,  # type: ignore[union-attr]
            ) as key:
                for name in names:
                    try:
                        value, _ = winreg.QueryValueEx(key, name)  # type: ignore[union-attr]
                    except OSError:
                        continue
                    if value:
                        values.append(str(value))
        except OSError:
            continue
    return values


def _steam_registry_roots() -> list[Path]:
    if winreg is None:
        return []
    roots: list[Path] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for value in _registry_values(
            hive,
            r"SOFTWARE\Valve\Steam",
            ("SteamPath", "InstallPath"),
        ):
            roots.append(Path(value))
    return roots


def _registered_game_locations() -> list[Path]:
    if winreg is None:
        return []
    locations: list[Path] = []
    uninstall_key = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion"
        r"\Uninstall\Steam App 2358720"
    )
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for value in _registry_values(hive, uninstall_key, ("InstallLocation",)):
            locations.append(Path(value))
    return locations


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
    roots = _steam_registry_roots()
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
    for drive in _windows_drive_roots():
        roots.extend(
            (
                drive / "Steam",
                drive / "SteamLibrary",
                drive / "Program Files (x86)" / "Steam",
                drive / "Program Files" / "Steam",
            )
        )
    return _unique_paths(roots)


def _library_paths_from_vdf(steam_root: Path) -> list[Path]:
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        return []
    try:
        text = vdf.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return []
    libraries: list[Path] = []
    for match in re.finditer(r'"path"\s+"([^"]+)"', text, flags=re.IGNORECASE):
        value = match.group(1).replace("\\\\", "\\")
        if value:
            libraries.append(Path(value))
    return libraries


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


def find_wukong_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_path = os.environ.get("WUKONG_DIR", "").strip().strip('"')
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(_registered_game_locations())

    steam_roots = _steam_roots()
    libraries: list[Path] = list(steam_roots)
    for steam_root in steam_roots:
        libraries.extend(_library_paths_from_vdf(steam_root))
    for library in _unique_paths(libraries):
        candidates.append(
            library / "steamapps" / "common" / "BlackMythWukong"
        )

    layouts: list[Path] = []
    seen_roots: set[str] = set()
    for candidate in _unique_paths(candidates):
        layout = resolve_game_layout(candidate)
        if layout is None:
            continue
        key = os.path.normcase(str(layout.root)).casefold()
        if key in seen_roots:
            continue
        seen_roots.add(key)
        layouts.append(layout.root)
    return layouts


def resolve_wukong_dir(
    explicit: Path | None,
    *,
    prompt: bool,
    prefer_installed: bool = False,
) -> ResolveResult:
    if explicit is not None:
        layout = resolve_game_layout(explicit)
        if layout is not None:
            return ResolveResult(layout, False)
        _print(f"[错误] 不是有效的《黑神话：悟空》目录：{explicit}")
        _print(f"       必须能找到 {GAME_EXE_REL}.")
        if not prompt:
            return ResolveResult(None, False)
        _print("请重新输入，或直接回车跳过。")

    layouts = [
        layout
        for path in find_wukong_candidates()
        if (layout := resolve_game_layout(path)) is not None
    ]
    if prefer_installed:
        layouts.sort(
            key=lambda item: not (item.root / STATE_DIRNAME / STATE_FILENAME).is_file()
        )

    if len(layouts) == 1 and explicit is None:
        return ResolveResult(layouts[0], False)
    if len(layouts) > 1 and explicit is None:
        _print("检测到多个《黑神话：悟空》安装：")
        for index, layout in enumerate(layouts, 1):
            _print(f"  [{index}] {layout.root}")
        if not prompt:
            _print(f"[自动] 无人值守模式使用：{layouts[0].root}")
            return ResolveResult(layouts[0], False)
        try:
            choice = input(
                f"选择 [1-{len(layouts)}]，或输入完整路径；直接回车跳过: "
            ).strip().strip('"')
        except EOFError:
            choice = ""
        if not choice:
            _print("[跳过] 未安装黑神话相机插件。")
            return ResolveResult(None, True)
        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(layouts):
                return ResolveResult(layouts[index], False)
            _print("[错误] 无效选项。")
            return ResolveResult(None, False)
        selected = resolve_game_layout(Path(choice))
        if selected is not None:
            return ResolveResult(selected, False)
        _print(f"[错误] 目录无效（未找到 {GAME_EXE_REL}）：{choice}")
        return ResolveResult(None, False)

    if not prompt:
        _print(
            "[跳过] 未找到《黑神话：悟空》。"
            "可设置 WUKONG_DIR 或传入 --wukong-dir 后重试。"
        )
        return ResolveResult(None, True)

    _print("未自动找到《黑神话：悟空》。")
    while True:
        try:
            typed = input(
                "请输入游戏根目录或 b1\\Binaries\\Win64 路径"
                "（直接回车跳过）: "
            ).strip().strip('"')
        except EOFError:
            typed = ""
        if not typed:
            _print("[跳过] 未安装黑神话相机插件。")
            return ResolveResult(None, True)
        layout = resolve_game_layout(Path(typed))
        if layout is not None:
            return ResolveResult(layout, False)
        _print(f"[错误] 未找到 {GAME_EXE_REL}。请重试，或直接回车跳过。")


def load_and_verify_manifest() -> tuple[list[PayloadFile], str]:
    if not MANIFEST_PATH.is_file():
        raise InstallerError(f"缺少 payload manifest：{MANIFEST_PATH}")
    try:
        raw_bytes = MANIFEST_PATH.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError(f"无法读取 payload manifest：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise InstallerError("payload manifest 格式无效：缺少 files 数组")
    schema = data.get("schema")
    if not isinstance(schema, str) or not schema:
        raise InstallerError("payload manifest 格式无效：缺少 schema")

    payload_files: list[PayloadFile] = []
    seen_destinations: set[str] = set()
    for index, entry in enumerate(data["files"], 1):
        if not isinstance(entry, dict):
            raise InstallerError(f"manifest 第 {index} 项不是对象")
        manifest_path = entry.get("path")
        expected_bytes = entry.get("bytes")
        expected_sha = entry.get("sha256")
        if not isinstance(manifest_path, str) or "\\" in manifest_path:
            raise InstallerError(f"manifest 第 {index} 项 path 无效")
        pure_path = PurePosixPath(manifest_path)
        if (
            pure_path.is_absolute()
            or len(pure_path.parts) < 2
            or pure_path.parts[0] != PAYLOAD_PREFIX
            or any(part in ("", ".", "..") for part in pure_path.parts)
        ):
            raise InstallerError(
                f"manifest path 必须是 payload/ 下的安全相对路径：{manifest_path}"
            )
        if not isinstance(expected_bytes, int) or expected_bytes < 0:
            raise InstallerError(f"manifest bytes 无效：{manifest_path}")
        if not isinstance(expected_sha, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", expected_sha
        ):
            raise InstallerError(f"manifest SHA256 无效：{manifest_path}")

        source = (CAMERA_ROOT / Path(*pure_path.parts)).resolve()
        payload_root = (CAMERA_ROOT / PAYLOAD_PREFIX).resolve()
        if not _is_within(source, payload_root) or not source.is_file():
            raise InstallerError(f"payload 文件不存在或越界：{manifest_path}")
        destination_relative = Path(*pure_path.parts[1:])
        destination_key = os.path.normcase(str(destination_relative)).casefold()
        if destination_key in seen_destinations:
            raise InstallerError(f"manifest 目标路径重复：{manifest_path}")
        seen_destinations.add(destination_key)

        actual_bytes = source.stat().st_size
        actual_sha = _sha256(source)
        install_bytes: bytes | None = None
        if (
            actual_bytes != expected_bytes
            or actual_sha.casefold() != expected_sha.casefold()
        ):
            install_bytes = _canonical_payload_bytes(
                source,
                expected_bytes=expected_bytes,
                expected_sha=expected_sha,
            )
            if install_bytes is None:
                raise InstallerError(
                    f"payload 校验失败：{manifest_path}，"
                    f"期望 {expected_bytes} bytes / {expected_sha.lower()}，"
                    f"实际 {actual_bytes} bytes / {actual_sha}"
                )
            _print(f"[修复] 已规范化 payload 文本换行：{manifest_path}")
        payload_files.append(
            PayloadFile(
                manifest_path=manifest_path,
                source=source,
                destination_relative=destination_relative,
                byte_count=expected_bytes,
                sha256=expected_sha.lower(),
                install_bytes=install_bytes,
            )
        )

    manifest_digest = hashlib.sha256(raw_bytes).hexdigest()
    return payload_files, f"{schema}:{manifest_digest[:16]}"


def _merge_mods_txt(
    destination: Path,
    source: Path,
    install_bytes: bytes | None = None,
) -> None:
    if not destination.exists():
        if install_bytes is None:
            shutil.copy2(source, destination)
        else:
            destination.write_bytes(install_bytes)
        return
    try:
        lines = destination.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise InstallerError(f"无法读取现有 UE4SS Mods/mods.txt：{exc}") from exc
    kept = [
        line
        for line in lines
        if not re.match(r"^\s*CameraFrameLogger\s*:", line, flags=re.IGNORECASE)
    ]
    while kept and not kept[-1].strip():
        kept.pop()
    kept.append("CameraFrameLogger : 1")
    destination.write_text("\n".join(kept) + "\n", encoding="utf-8")


def verify_installed_payload(win64: Path, files: Iterable[PayloadFile]) -> None:
    for payload_file in files:
        destination = (win64 / payload_file.destination_relative).resolve()
        if not _is_within(destination, win64) or not destination.is_file():
            raise InstallerError(
                f"安装后文件不存在或越界：{payload_file.destination_relative}"
            )
        if payload_file.destination_relative == MODS_TXT_RELATIVE:
            try:
                mods_text = destination.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise InstallerError(f"无法校验安装后的 Mods/mods.txt：{exc}") from exc
            if not re.search(
                r"(?im)^\s*CameraFrameLogger\s*:\s*1\s*$",
                mods_text,
            ):
                raise InstallerError("安装后的 Mods/mods.txt 未启用 CameraFrameLogger")
            continue

        actual_bytes = destination.stat().st_size
        if actual_bytes != payload_file.byte_count:
            raise InstallerError(
                f"安装后字节数不符：{payload_file.destination_relative}，"
                f"期望 {payload_file.byte_count}，实际 {actual_bytes}"
            )
        actual_sha = _sha256(destination)
        if actual_sha != payload_file.sha256:
            raise InstallerError(
                f"安装后 SHA256 不符：{payload_file.destination_relative}"
            )


def running_game_processes() -> list[str]:
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
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallerError(f"无法检查游戏进程：{exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"tasklist exit {result.returncode}"
        raise InstallerError(f"无法检查游戏进程：{detail}")

    import csv
    import io

    running: list[str] = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if not row:
            continue
        image_name = row[0].strip()
        if image_name.casefold() in GAME_PROCESS_NAMES:
            running.append(image_name)
    return sorted(set(running), key=str.casefold)


def ensure_game_closed() -> None:
    running = running_game_processes()
    if running:
        raise InstallerError(
            "检测到游戏仍在运行（"
            + "、".join(running)
            + "）。请完全退出游戏后重试。"
        )


def confirm_game_version(layout: GameLayout, *, prompt: bool, force: bool) -> None:
    actual_bytes = layout.exe.stat().st_size
    if actual_bytes == EXPECTED_EXE_BYTES:
        return
    warning = (
        f"游戏 exe 版本不匹配：期望 {EXPECTED_EXE_BYTES} 字节，"
        f"实际 {actual_bytes} 字节。当前 payload 可能与该游戏版本不兼容。"
    )
    if force:
        _print(f"[警告] {warning}")
        _print("       已使用 --force-version，继续安装。")
        return
    if not prompt:
        raise InstallerError(warning + " 无人值守模式需显式传入 --force-version。")
    _print(f"[警告] {warning}")
    try:
        answer = input("确认仍要安装请输入大写 YES：").strip()
    except EOFError:
        answer = ""
    if answer != "YES":
        raise InstallerError("未收到明确的 YES，已取消安装。")


def _can_write_directory(directory: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".game_recorder_write_test_",
            dir=directory,
            delete=True,
        ):
            pass
        return True
    except OSError:
        return False


def _can_write_existing(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        with path.open("ab"):
            pass
        return True
    except (IsADirectoryError, OSError):
        return path.is_dir() and _can_write_directory(path)


def needs_elevation(layout: GameLayout) -> bool:
    if os.name != "nt":
        return False
    if not _can_write_directory(layout.root) or not _can_write_directory(layout.win64):
        return True
    for path in (
        layout.win64 / "dwmapi.dll",
        layout.win64 / LEGACY_LOADER_FILENAME,
        layout.win64 / "ue4ss",
        layout.root / STATE_DIRNAME,
    ):
        if not _can_write_existing(path):
            return True
    return False


def elevate_and_wait(
    script_path: Path,
    original_argv: list[str],
    layout: GameLayout,
) -> int:
    if os.name != "nt":
        raise InstallerError("UAC 提权仅适用于 Windows")
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
            str(script_path),
            *original_argv,
            "--wukong-dir",
            str(layout.root),
            "--skip-elevation",
        ]
    )
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = parameters
    info.lpDirectory = str(PROJECT_ROOT)
    info.nShow = 1

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell_execute = shell32.ShellExecuteExW
    shell_execute.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell_execute.restype = wintypes.BOOL
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD
    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_exit_code_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    if not shell_execute(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:
            raise InstallerError("用户取消了 UAC 提权，未进行任何安装变更。")
        raise InstallerError(f"无法启动 UAC 提权子进程（Windows 错误 {error}）")
    if not info.hProcess:
        raise InstallerError("UAC 子进程启动后未返回进程句柄")

    try:
        wait_result = wait_for_single_object(info.hProcess, 0xFFFFFFFF)
        if wait_result != 0:
            raise InstallerError(f"等待 UAC 子进程失败（代码 {wait_result}）")
        exit_code = wintypes.DWORD()
        if not get_exit_code_process(info.hProcess, ctypes.byref(exit_code)):
            raise InstallerError("无法读取 UAC 子进程退出码")
        return int(exit_code.value)
    finally:
        close_handle(info.hProcess)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _replace_atomic_write_through(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_atomic_write_through(source: Path, destination: Path) -> None:
    if os.name != "nt":
        os.replace(source, destination)
        return
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file.restype = wintypes.BOOL
    # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
    if not move_file(str(source), str(destination), 0x1 | 0x8):
        raise ctypes.WinError(ctypes.get_last_error())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows does not support FlushFileBuffers on directory handles.
        # Individual files are fsynced and metadata-bearing atomic replacements
        # use MOVEFILE_WRITE_THROUGH instead.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    # Windows rejects os.fsync() on a read-only CRT file descriptor. Opening
    # in append mode supplies the writable handle FlushFileBuffers requires
    # without truncating or otherwise changing the copied snapshot file.
    mode = "ab" if os.name == "nt" else "rb"
    with path.open(mode) as stream:
        os.fsync(stream.fileno())


def _fsync_path(path: Path) -> None:
    if path.is_file():
        _fsync_file(path)
        return
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_file():
                _fsync_file(child)
        _fsync_directory(path)


def _write_durable_marker(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="ascii", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_atomic_write_through(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _transaction_file_inventory(transaction_dir: Path) -> dict[str, dict[str, Any]]:
    excluded = {"PREPARED", "COMMITTED", "snapshot-manifest.json"}
    inventory: dict[str, dict[str, Any]] = {}
    for path in transaction_dir.rglob("*"):
        if path.is_symlink():
            raise InstallerError(f"事务快照含符号链接：{path}")
        if not path.is_file():
            continue
        relative = path.relative_to(transaction_dir).as_posix()
        if relative in excluded:
            continue
        _fsync_file(path)
        inventory[relative] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return inventory


def validate_transaction_snapshot(transaction_dir: Path) -> dict[str, Any]:
    manifest_path = transaction_dir / "snapshot-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError(f"事务快照清单缺失或损坏：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise InstallerError("事务快照清单版本无效")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise InstallerError("事务快照清单缺少 files")

    for path in transaction_dir.rglob("*"):
        if path.is_symlink():
            raise InstallerError(f"事务快照含符号链接：{path}")
    actual_paths = {
        path.relative_to(transaction_dir).as_posix()
        for path in transaction_dir.rglob("*")
        if path.is_file()
        and path.relative_to(transaction_dir).as_posix()
        not in {"PREPARED", "COMMITTED", "snapshot-manifest.json"}
    }
    if actual_paths != set(files):
        raise InstallerError("事务快照文件集合与清单不一致")
    for relative, expected in files.items():
        path = transaction_dir / relative
        if (
            not isinstance(expected, dict)
            or not isinstance(expected.get("bytes"), int)
            or not isinstance(expected.get("sha256"), str)
            or path.stat().st_size != expected["bytes"]
            or _sha256(path) != expected["sha256"]
        ):
            raise InstallerError(f"事务快照文件校验失败：{relative}")

    presence_checks = {
        "had_dwmapi": (transaction_dir / "dwmapi.dll").is_file(),
        "had_xinput": (transaction_dir / LEGACY_LOADER_FILENAME).is_file(),
        "had_ue4ss": (transaction_dir / "ue4ss").is_dir(),
        "had_state": (transaction_dir / "state").is_dir(),
        "had_control": (transaction_dir / "control.json").is_file(),
    }
    for key, actual in presence_checks.items():
        if not isinstance(manifest.get(key), bool) or manifest[key] != actual:
            raise InstallerError(f"事务快照存在性标记不一致：{key}")
    if not isinstance(manifest.get("had_control_parent"), bool):
        raise InstallerError("事务快照缺少控制目录存在性标记")
    return manifest


def _persist_restored_targets(
    layout: GameLayout,
    state_dir: Path,
    control_file: Path | None,
    manifest: dict[str, Any],
) -> None:
    if manifest["had_dwmapi"]:
        _fsync_path(layout.win64 / "dwmapi.dll")
    if manifest["had_xinput"]:
        _fsync_path(layout.win64 / LEGACY_LOADER_FILENAME)
    if manifest["had_ue4ss"]:
        _fsync_path(layout.win64 / "ue4ss")
    if manifest["had_state"]:
        _fsync_path(state_dir)
    if manifest["had_control"] and control_file is not None:
        _fsync_path(control_file)
        _fsync_directory(control_file.parent)
    elif control_file is not None:
        sync_parent = (
            control_file.parent
            if control_file.parent.exists()
            else control_file.parent.parent
        )
        if sync_parent.exists():
            _fsync_directory(sync_parent)
    _fsync_directory(layout.win64)
    _fsync_directory(layout.root)


def _lua_string(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def write_dynamic_config(win64: Path, control_file: Path) -> Path:
    config = win64 / "ue4ss" / "Mods" / "CameraFrameLogger" / "config.lua"
    if not _is_within(config, win64):
        raise InstallerError("动态配置目标越出 Win64 目录")
    config.parent.mkdir(parents=True, exist_ok=True)
    normalized_control = control_file.resolve().as_posix()
    content = (
        'return { control_file = "'
        + _lua_string(normalized_control)
        + '" }\n'
    )
    with config.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    return config


def seed_idle_control(control_file: Path) -> None:
    _write_json_atomic(
        control_file,
        {
            "status": "idle",
            "updated_at_ms": 0,
        },
    )


def _read_state(state_file: Path) -> dict[str, Any]:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError(f"无法读取现有安装状态：{exc}") from exc
    if not isinstance(data, dict):
        raise InstallerError("现有安装状态格式无效")
    return data


def validate_existing_state(
    state: dict[str, Any],
    layout: GameLayout,
    state_dir: Path,
) -> None:
    if state.get("state_schema") != STATE_SCHEMA:
        raise InstallerError("现有安装状态版本不受支持，不能安全升级")
    saved_root = state.get("game_root")
    if not isinstance(saved_root, str):
        raise InstallerError("现有安装状态缺少 game_root")
    try:
        if Path(saved_root).resolve() != layout.root:
            raise InstallerError("现有安装状态的游戏根目录与当前目标不一致")
    except OSError as exc:
        raise InstallerError(f"现有安装状态的 game_root 无效：{exc}") from exc

    original_root = (state_dir / "backups" / "original").resolve()
    backup_root = state.get("original_backup_path")
    if not isinstance(backup_root, str) or Path(backup_root).resolve() != original_root:
        raise InstallerError("现有安装状态的原始备份路径无效")

    expected_ue4ss = (original_root / "ue4ss").resolve()
    expected_dwmapi = (original_root / "dwmapi.dll").resolve()
    expected_xinput = (original_root / LEGACY_LOADER_FILENAME).resolve()
    ue4ss_path = state.get("original_ue4ss_backup_path")
    dwmapi_path = state.get("original_dwmapi_backup_path")
    xinput_path = state.get("original_xinput_backup_path")
    had_ue4ss = state.get("had_original_ue4ss")
    had_dwmapi = state.get("had_original_dwmapi")
    had_xinput = state.get("had_original_xinput")
    if (
        not isinstance(had_ue4ss, bool)
        or not isinstance(had_dwmapi, bool)
        or not isinstance(had_xinput, bool)
    ):
        raise InstallerError("现有安装状态缺少原始文件备份标记")
    if had_ue4ss:
        if (
            not isinstance(ue4ss_path, str)
            or Path(ue4ss_path).resolve() != expected_ue4ss
            or not expected_ue4ss.is_dir()
            or not _is_within(expected_ue4ss, state_dir)
        ):
            raise InstallerError("首次安装前的 UE4SS 完整备份缺失或路径无效")
    elif ue4ss_path is not None:
        raise InstallerError("现有安装状态的 UE4SS 备份标记矛盾")
    if had_dwmapi:
        if (
            not isinstance(dwmapi_path, str)
            or Path(dwmapi_path).resolve() != expected_dwmapi
            or not expected_dwmapi.is_file()
            or not _is_within(expected_dwmapi, state_dir)
        ):
            raise InstallerError("首次安装前的 dwmapi.dll 备份缺失或路径无效")
    elif dwmapi_path is not None:
        raise InstallerError("现有安装状态的 dwmapi.dll 备份标记矛盾")
    if had_xinput:
        if (
            not isinstance(xinput_path, str)
            or Path(xinput_path).resolve() != expected_xinput
            or not expected_xinput.is_file()
            or not _is_within(expected_xinput, state_dir)
        ):
            raise InstallerError(
                f"首次安装前的 {LEGACY_LOADER_FILENAME} 备份缺失或路径无效"
            )
    elif xinput_path is not None:
        raise InstallerError(
            f"现有安装状态的 {LEGACY_LOADER_FILENAME} 备份标记矛盾"
        )
    if not _is_within(original_root, state_dir):
        raise InstallerError("现有安装状态的原始备份越出状态目录")


def verify_upgrade_safe(state: dict[str, Any], layout: GameLayout) -> None:
    """Refuse to overwrite managed files changed since the last install."""
    current_xinput = layout.win64 / LEGACY_LOADER_FILENAME
    if current_xinput.exists():
        pending = state.get("pending_legacy_xinput_removal") is True
        backup_text = state.get("original_xinput_backup_path")
        backup = Path(backup_text).resolve() if isinstance(backup_text, str) else None
        if (
            not pending
            or backup is None
            or not backup.is_file()
            or not current_xinput.is_file()
            or _sha256(current_xinput) != _sha256(backup)
        ):
            raise InstallerError(
                f"安装后出现 {LEGACY_LOADER_FILENAME}；它会与当前 dwmapi 加载器冲突，"
                "请先确认其来源并移走后再升级"
            )
    managed_hashes = state.get("managed_hashes")
    if not isinstance(managed_hashes, dict):
        raise InstallerError("现有安装状态缺少受管文件哈希，不能安全升级")
    for relative_text, expected_hash in managed_hashes.items():
        if not isinstance(relative_text, str) or not isinstance(expected_hash, str):
            raise InstallerError("现有安装状态中的受管文件哈希无效")
        relative = Path(relative_text)
        # mods.txt is intentionally merged so user-added mod entries survive.
        if relative == MODS_TXT_RELATIVE:
            continue
        destination = (layout.win64 / relative).resolve()
        if not _is_within(destination, layout.win64) or not destination.is_file():
            raise InstallerError(f"受管文件已被移动或删除，拒绝升级：{relative_text}")
        if _sha256(destination) != expected_hash:
            raise InstallerError(f"受管文件安装后被修改，拒绝覆盖升级：{relative_text}")

    config_text = state.get("dynamic_config")
    config_hash = state.get("dynamic_config_sha256")
    expected_config = (
        layout.win64 / "ue4ss" / "Mods" / "CameraFrameLogger" / "config.lua"
    ).resolve()
    if (
        not isinstance(config_text, str)
        or not isinstance(config_hash, str)
        or Path(config_text).resolve() != expected_config
        or not expected_config.is_file()
        or _sha256(expected_config) != config_hash
    ):
        raise InstallerError("动态 config.lua 安装后被修改或缺失，拒绝覆盖升级")


class ChangeSnapshot:
    """A complete pre-change snapshot for install/uninstall rollback."""

    def __init__(
        self,
        layout: GameLayout,
        state_dir: Path,
        control_file: Path | None,
    ) -> None:
        self.layout = layout
        self.state_dir = state_dir
        self.control_file = control_file
        self.transaction_dir = layout.root / (
            f"{STATE_DIRNAME}.transaction-{uuid.uuid4().hex}"
        )
        self.had_dwmapi = (layout.win64 / "dwmapi.dll").exists()
        self.had_xinput = (layout.win64 / LEGACY_LOADER_FILENAME).exists()
        self.had_ue4ss = (layout.win64 / "ue4ss").exists()
        self.had_state = state_dir.exists()
        self.had_control = bool(control_file and control_file.exists())
        self.had_control_parent = bool(control_file and control_file.parent.exists())
        try:
            self.transaction_dir.mkdir(parents=False, exist_ok=False)
            if self.had_dwmapi:
                _copy_path(
                    layout.win64 / "dwmapi.dll",
                    self.transaction_dir / "dwmapi.dll",
                )
            if self.had_xinput:
                _copy_path(
                    layout.win64 / LEGACY_LOADER_FILENAME,
                    self.transaction_dir / LEGACY_LOADER_FILENAME,
                )
            if self.had_ue4ss:
                _copy_path(
                    layout.win64 / "ue4ss",
                    self.transaction_dir / "ue4ss",
                )
            if self.had_state:
                _copy_path(state_dir, self.transaction_dir / "state")
            if self.had_control and control_file is not None:
                _copy_path(control_file, self.transaction_dir / "control.json")
            snapshot_manifest = {
                "schema": 1,
                "had_dwmapi": self.had_dwmapi,
                "had_xinput": self.had_xinput,
                "had_ue4ss": self.had_ue4ss,
                "had_state": self.had_state,
                "had_control": self.had_control,
                "had_control_parent": self.had_control_parent,
                "files": _transaction_file_inventory(self.transaction_dir),
            }
            _write_json_atomic(
                self.transaction_dir / "snapshot-manifest.json",
                snapshot_manifest,
            )
            validate_transaction_snapshot(self.transaction_dir)
            _write_durable_marker(
                self.transaction_dir / "PREPARED",
                "snapshot complete\n",
            )
        except Exception:
            shutil.rmtree(self.transaction_dir, ignore_errors=True)
            raise

    def rollback(self) -> None:
        snapshot_manifest = validate_transaction_snapshot(self.transaction_dir)
        destination_dwmapi = self.layout.win64 / "dwmapi.dll"
        destination_xinput = self.layout.win64 / LEGACY_LOADER_FILENAME
        destination_ue4ss = self.layout.win64 / "ue4ss"
        _remove_path(destination_dwmapi)
        _remove_path(destination_xinput)
        _remove_path(destination_ue4ss)
        _remove_path(self.state_dir)
        if self.control_file is not None:
            _remove_path(self.control_file)
            if not self.had_control_parent:
                try:
                    self.control_file.parent.rmdir()
                except OSError:
                    pass

        if self.had_dwmapi:
            _copy_path(self.transaction_dir / "dwmapi.dll", destination_dwmapi)
        if self.had_xinput:
            _copy_path(
                self.transaction_dir / LEGACY_LOADER_FILENAME,
                destination_xinput,
            )
        if self.had_ue4ss:
            _copy_path(self.transaction_dir / "ue4ss", destination_ue4ss)
        if self.had_state:
            _copy_path(self.transaction_dir / "state", self.state_dir)
        if self.had_control and self.control_file is not None:
            _copy_path(self.transaction_dir / "control.json", self.control_file)
        _persist_restored_targets(
            self.layout,
            self.state_dir,
            self.control_file,
            snapshot_manifest,
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.transaction_dir)


def recover_interrupted_transaction(
    layout: GameLayout,
    state_dir: Path,
    control_file: Path | None,
) -> bool:
    """Restore a persisted pre-change snapshot after termination or power loss."""
    transactions = sorted(
        path
        for path in layout.root.glob(f"{STATE_DIRNAME}.transaction-*")
        if path.is_dir() and not path.is_symlink()
    )
    if not transactions:
        return False
    if len(transactions) > 1:
        raise InstallerError(
            "检测到多个未完成安装事务，无法判断恢复顺序："
            + "、".join(path.name for path in transactions)
        )
    transaction = transactions[0]
    if (transaction / "COMMITTED").is_file():
        shutil.rmtree(transaction)
        _print(f"[恢复] 已清理上次成功操作遗留的事务快照：{transaction.name}")
        return True
    if not (transaction / "PREPARED").is_file():
        shutil.rmtree(transaction)
        _print(f"[恢复] 已清理未完成且尚未生效的事务快照：{transaction.name}")
        return True
    snapshot_manifest = validate_transaction_snapshot(transaction)

    snapshot_state = transaction / "state"
    snapshot_control = transaction / "control.json"
    if control_file is None and (snapshot_state / STATE_FILENAME).is_file():
        try:
            previous_state = _read_state(snapshot_state / STATE_FILENAME)
            previous_control = previous_state.get("control_file")
            if isinstance(previous_control, str):
                control_file = Path(previous_control)
        except InstallerError:
            control_file = None

    _remove_path(layout.win64 / "dwmapi.dll")
    _remove_path(layout.win64 / LEGACY_LOADER_FILENAME)
    _remove_path(layout.win64 / "ue4ss")
    _remove_path(state_dir)
    if (transaction / "dwmapi.dll").is_file():
        _copy_path(transaction / "dwmapi.dll", layout.win64 / "dwmapi.dll")
    if (transaction / LEGACY_LOADER_FILENAME).is_file():
        _copy_path(
            transaction / LEGACY_LOADER_FILENAME,
            layout.win64 / LEGACY_LOADER_FILENAME,
        )
    if (transaction / "ue4ss").is_dir():
        _copy_path(transaction / "ue4ss", layout.win64 / "ue4ss")
    if snapshot_state.is_dir():
        _copy_path(snapshot_state, state_dir)
    if control_file is not None and snapshot_control.is_file():
        _remove_path(control_file)
        _copy_path(snapshot_control, control_file)
    elif control_file is not None and not snapshot_manifest["had_control"]:
        _remove_path(control_file)
        if not snapshot_manifest["had_control_parent"]:
            try:
                control_file.parent.rmdir()
            except OSError:
                pass
    _persist_restored_targets(
        layout,
        state_dir,
        control_file,
        snapshot_manifest,
    )
    shutil.rmtree(transaction)
    _print("[恢复] 已从上次中断操作的事务快照恢复游戏文件。")
    return True


def migrate_state_v2(
    layout: GameLayout,
    state_dir: Path,
    control_file: Path | None,
) -> dict[str, Any]:
    """Transactionally add legacy-loader ownership fields to schema 2 state."""
    state_file = state_dir / STATE_FILENAME
    state = _read_state(state_file)
    if state.get("state_schema") != 2:
        return state
    if control_file is None:
        saved_control = state.get("control_file")
        if isinstance(saved_control, str):
            control_file = Path(saved_control)

    try:
        snapshot = ChangeSnapshot(layout, state_dir, control_file)
    except Exception as exc:
        raise InstallerError(f"创建状态迁移快照失败：{exc}") from exc
    try:
        original_root = (state_dir / "backups" / "original").resolve()
        saved_backup = state.get("original_backup_path")
        saved_root = state.get("game_root")
        if (
            not isinstance(saved_backup, str)
            or Path(saved_backup).resolve() != original_root
            or not isinstance(saved_root, str)
            or Path(saved_root).resolve() != layout.root
            or not _is_within(original_root, state_dir)
        ):
            raise InstallerError("schema 2 状态的游戏根或原始备份路径无效")

        current_xinput = layout.win64 / LEGACY_LOADER_FILENAME
        backup_xinput = original_root / LEGACY_LOADER_FILENAME
        had_xinput = current_xinput.exists()
        if had_xinput:
            if not current_xinput.is_file() or backup_xinput.exists():
                raise InstallerError(
                    f"无法安全迁移现有 {LEGACY_LOADER_FILENAME}"
                )
            _copy_path(current_xinput, backup_xinput)
            _fsync_file(backup_xinput)

        state["state_schema"] = STATE_SCHEMA
        state["had_original_xinput"] = had_xinput
        state["original_xinput_backup_path"] = (
            str(backup_xinput.resolve()) if had_xinput else None
        )
        state["pending_legacy_xinput_removal"] = had_xinput
        _write_json_atomic(state_file, state)
        validate_existing_state(state, layout, state_dir)
        _write_durable_marker(
            snapshot.transaction_dir / "COMMITTED",
            "state migration committed\n",
        )
    except Exception as exc:
        try:
            snapshot.rollback()
        except Exception as rollback_exc:
            raise InstallerError(
                f"schema 2 状态迁移失败：{exc}；回滚未持久化，事务快照已保留："
                f"{rollback_exc}"
            ) from exc
        else:
            snapshot.cleanup()
        raise InstallerError(f"schema 2 状态迁移失败，已回滚：{exc}") from exc

    snapshot.cleanup()
    _print("[升级] 已将旧 schema 2 安装状态安全迁移到 schema 3。")
    return state


def _create_first_backup(layout: GameLayout, state_dir: Path) -> dict[str, Any]:
    original_root = state_dir / "backups" / "original"
    original_root.mkdir(parents=True, exist_ok=False)
    source_dwmapi = layout.win64 / "dwmapi.dll"
    source_xinput = layout.win64 / LEGACY_LOADER_FILENAME
    source_ue4ss = layout.win64 / "ue4ss"
    had_dwmapi = source_dwmapi.exists()
    had_xinput = source_xinput.exists()
    had_ue4ss = source_ue4ss.exists()
    backup_dwmapi = original_root / "dwmapi.dll"
    backup_xinput = original_root / LEGACY_LOADER_FILENAME
    backup_ue4ss = original_root / "ue4ss"
    if had_dwmapi:
        if not source_dwmapi.is_file():
            raise InstallerError("现有 Win64/dwmapi.dll 不是普通文件，无法安全备份")
        _copy_path(source_dwmapi, backup_dwmapi)
    if had_xinput:
        if not source_xinput.is_file():
            raise InstallerError(
                f"现有 Win64/{LEGACY_LOADER_FILENAME} 不是普通文件，无法安全备份"
            )
        _copy_path(source_xinput, backup_xinput)
    if had_ue4ss:
        if not source_ue4ss.is_dir():
            raise InstallerError("现有 Win64/ue4ss 不是目录，无法安全备份")
        _copy_path(source_ue4ss, backup_ue4ss)
    return {
        "original_backup_path": str(original_root.resolve()),
        "had_original_dwmapi": had_dwmapi,
        "original_dwmapi_backup_path": (
            str(backup_dwmapi.resolve()) if had_dwmapi else None
        ),
        "had_original_xinput": had_xinput,
        "original_xinput_backup_path": (
            str(backup_xinput.resolve()) if had_xinput else None
        ),
        "had_original_ue4ss": had_ue4ss,
        "original_ue4ss_backup_path": (
            str(backup_ue4ss.resolve()) if had_ue4ss else None
        ),
    }


def install(
    layout: GameLayout,
    recordings_dir: Path,
    payload_files: list[PayloadFile],
    payload_version: str,
) -> tuple[Path, Path, bool]:
    state_dir = layout.root / STATE_DIRNAME
    state_file = state_dir / STATE_FILENAME
    control_file = recordings_dir.resolve().parent / CONTROL_DIRNAME / CONTROL_FILENAME
    recover_interrupted_transaction(layout, state_dir, control_file)
    existing_state: dict[str, Any] | None = None
    if state_dir.is_symlink():
        raise InstallerError(f"状态目录不能是符号链接：{state_dir}")
    if state_file.is_file():
        existing_state = _read_state(state_file)
        if existing_state.get("state_schema") == 2:
            existing_state = migrate_state_v2(
                layout,
                state_dir,
                control_file,
            )
        validate_existing_state(existing_state, layout, state_dir)
        verify_upgrade_safe(existing_state, layout)
    elif state_dir.exists():
        raise InstallerError(
            f"状态目录存在但缺少 {STATE_FILENAME}，为避免覆盖备份已停止：{state_dir}"
        )

    try:
        snapshot = ChangeSnapshot(layout, state_dir, control_file)
    except Exception as exc:
        raise InstallerError(f"创建安装事务快照失败，未进行变更：{exc}") from exc

    upgraded = existing_state is not None
    try:
        if existing_state is None:
            state_dir.mkdir(parents=False, exist_ok=False)
            backup_fields = _create_first_backup(layout, state_dir)
            first_installed_at = datetime.now(timezone.utc).isoformat()
        else:
            backup_fields = {
                key: existing_state[key]
                for key in (
                    "original_backup_path",
                    "had_original_dwmapi",
                    "original_dwmapi_backup_path",
                    "had_original_xinput",
                    "original_xinput_backup_path",
                    "had_original_ue4ss",
                    "original_ue4ss_backup_path",
                )
            }
            first_installed_at = str(
                existing_state.get("first_installed_at")
                or existing_state.get("installed_at")
                or datetime.now(timezone.utc).isoformat()
            )

        # UE4SS 3.x uses dwmapi.dll. The legacy xinput proxy cannot coexist and
        # is restored on uninstall if it predated the first installation.
        _remove_path(layout.win64 / LEGACY_LOADER_FILENAME)

        for payload_file in payload_files:
            destination = (layout.win64 / payload_file.destination_relative).resolve()
            if not _is_within(destination, layout.win64):
                raise InstallerError(
                    f"payload 安装目标越出 Win64："
                    f"{payload_file.destination_relative}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if payload_file.destination_relative == MODS_TXT_RELATIVE:
                _merge_mods_txt(
                    destination,
                    payload_file.source,
                    payload_file.install_bytes,
                )
            elif payload_file.install_bytes is not None:
                destination.write_bytes(payload_file.install_bytes)
            else:
                shutil.copy2(payload_file.source, destination)

        config_file = write_dynamic_config(layout.win64, control_file)
        seed_idle_control(control_file)
        verify_installed_payload(layout.win64, payload_files)
        expected_config = (
            'return { control_file = "'
            + _lua_string(control_file.resolve().as_posix())
            + '" }\n'
        )
        if config_file.read_text(encoding="utf-8") != expected_config:
            raise InstallerError("动态 config.lua 安装后校验失败")
        for payload_file in payload_files:
            _fsync_file(layout.win64 / payload_file.destination_relative)
        _fsync_file(config_file)

        now = datetime.now(timezone.utc).isoformat()
        state: dict[str, Any] = {
            "state_schema": STATE_SCHEMA,
            "version": payload_version,
            "first_installed_at": first_installed_at,
            "installed_at": now,
            "game_root": str(layout.root),
            "win64_dir": str(layout.win64),
            "manifest": str(MANIFEST_PATH),
            "managed_files": [
                payload.destination_relative.as_posix()
                for payload in payload_files
            ],
            "managed_hashes": {
                payload.destination_relative.as_posix(): _sha256(
                    layout.win64 / payload.destination_relative
                )
                for payload in payload_files
            },
            "dynamic_config": str(config_file),
            "dynamic_config_sha256": _sha256(config_file),
            "control_file": str(control_file),
            **backup_fields,
        }
        _write_json_atomic(state_file, state)
        _write_durable_marker(
            snapshot.transaction_dir / "COMMITTED",
            "install committed\n",
        )
    except Exception as exc:
        rollback_error: Exception | None = None
        try:
            snapshot.rollback()
        except Exception as rollback_exc:
            rollback_error = rollback_exc
        else:
            try:
                snapshot.cleanup()
            except OSError:
                pass
        if rollback_error is not None:
            raise InstallerError(
                f"安装失败：{exc}；事务回滚也失败：{rollback_error}"
            ) from exc
        raise InstallerError(f"安装失败，已恢复变更前状态：{exc}") from exc

    try:
        snapshot.cleanup()
    except OSError as exc:
        _print(f"[警告] 安装成功，但事务临时目录清理失败：{exc}")
    return state_file, control_file, upgraded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安装《黑神话：悟空》UE4SS 相机同步插件"
    )
    parser.add_argument(
        "--wukong-dir",
        type=Path,
        default=None,
        help=(
            "游戏根目录或 b1/Binaries/Win64；"
            "也可设置环境变量 WUKONG_DIR"
        ),
    )
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=PROJECT_ROOT / "recordings",
        help="game-recorder 输出目录（默认：项目 recordings/）",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="无人值守；找不到游戏时以 exit 3 无害跳过",
    )
    parser.add_argument(
        "--force-version",
        action="store_true",
        help="游戏 exe 大小与参考版本不同时仍继续安装",
    )
    parser.add_argument(
        "--skip-elevation",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    original_argv = list(sys.argv[1:] if argv is None else argv)

    _print("============================================================")
    _print("  黑神话：悟空 相机插件安装")
    _print("============================================================")

    if os.name != "nt":
        _print("[错误] 黑神话相机插件安装器只能在 Windows 上执行。")
        return 1

    result = resolve_wukong_dir(args.wukong_dir, prompt=not args.no_prompt)
    if result.layout is None:
        return 3 if result.skipped else 1
    layout = result.layout
    _print(f"游戏根目录：{layout.root}")
    _print(f"Win64 目录 ：{layout.win64}")

    try:
        payload_files, payload_version = load_and_verify_manifest()
        _print(f"payload 校验通过：{len(payload_files)} 个文件")
        if not args.skip_elevation and needs_elevation(layout):
            _print("游戏目录需要管理员权限，正在请求 UAC 提权 …")
            return elevate_and_wait(Path(__file__).resolve(), original_argv, layout)

        ensure_game_closed()
        confirm_game_version(
            layout,
            prompt=not args.no_prompt,
            force=args.force_version,
        )
        state_file, control_file, upgraded = install(
            layout,
            Path(args.recordings_dir),
            payload_files,
            payload_version,
        )
    except Exception as exc:
        _print(f"[错误] {exc}")
        return 1

    _print()
    _print("[成功] 黑神话相机插件已" + ("升级" if upgraded else "安装") + "。")
    _print(f"  状态文件：{state_file}")
    _print(f"  同步信号：{control_file}")
    _print("  安装器不会启动游戏；请先启动录制器，再进入游戏录制。")
    _print("============================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
