#!/usr/bin/env python3
"""Install RivaTuner Statistics Server and register per-game framerate caps.

A stable frame pacing in the game makes the recorded video far more usable:
DXGI desktop duplication samples whatever the display shows, so a game running
at a wildly varying framerate produces duplicated and unevenly spaced frames.

RTSS keeps one profile file per executable in ``Profiles\\<exe>.cfg`` and scans
that directory at startup, so a game is "registered" by writing the file — the
GUI is never needed.  The on-screen display is disabled by default because RTSS
draws it into the game's back buffer *before* Present, which means it would be
baked into every recording.

The module imports on non-Windows systems so its pure helpers can be unit
tested there.  Only ``main`` enforces the Windows-only restriction.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

try:
    import winreg  # type: ignore[import-not-found]
except ImportError:
    winreg = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_CACHE = PROJECT_ROOT / ".tools" / "rtss"

RTSS_VERSION = "7.3.7"
RTSS_ARCHIVE_NAME = "[Guru3D]-RTSSSetup737Build28314.zip"
# Guru3D fronts its own download page with a Cloudflare challenge that a script
# cannot pass.  NLUUG is the mirror Microsoft's winget manifest for
# Guru3D.RTSS 7.3.7 points at, and the digest below is the one it pins.
RTSS_ARCHIVE_URL = (
    "https://ftp.nluug.nl/pub/games/PC/guru3d/afterburner/"
    "%5BGuru3D%5D-RTSSSetup737Build28314.zip"
)
RTSS_ARCHIVE_SHA256 = (
    "9b084a8cb3e53ec1a673894d0b66e22b16c9fd8785636b020b2d422f3f2a820e"
)
SETUP_NAME = "RTSSSetup737.exe"
# RTSS ships inside MSI Afterburner and is signed by MSI, not by Guru3D.
SETUP_SIGNER_FRAGMENT = "micro-star international"
SETUP_SILENT_ARGS = ("/S",)

INSTALL_DIR_NAME = "RivaTuner Statistics Server"
DEFAULT_INSTALL_DIRS = (
    Path(r"C:\Program Files (x86)") / INSTALL_DIR_NAME,
    Path(r"C:\Program Files") / INSTALL_DIR_NAME,
)
SERVER_EXE = "RTSS.exe"
PROFILES_DIRNAME = "Profiles"
# RTSS reads profiles through the Win32 private-profile API, which uses the
# system ANSI code page rather than UTF-8.
PROFILE_ENCODING = "mbcs" if os.name == "nt" else "utf-8"

DEFAULT_GAME_EXE = "RDR2.exe"
DEFAULT_FPS = 60
MAX_FPS = 1000

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ZIP_ENTRIES = 64
MAX_ZIP_MEMBER_BYTES = 96 * 1024 * 1024
INSTALL_POLL_SECONDS = 60


class InstallerError(RuntimeError):
    """Expected, user-facing installation failure."""


class InstallerSkipped(InstallerError):
    """The optional RTSS integration was intentionally skipped."""


def _print(message: str = "") -> None:
    print(message, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_pe_x86(path: Path, *, label: str) -> None:
    """Reject anything that is not a Windows executable before running it."""
    try:
        data = path.read_bytes()[:65536]
    except OSError as exc:
        raise InstallerError(f"无法读取 {label}：{exc}") from exc
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise InstallerError(f"{label} 不是有效 PE 文件")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 4 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise InstallerError(f"{label} 的 PE 头无效")


# --------------------------------------------------------------------------
# Profile files
# --------------------------------------------------------------------------


def normalize_exe_name(name: str) -> str:
    """Return a bare ``foo.exe`` file name; RTSS matches profiles by name."""
    cleaned = name.strip().strip('"').strip()
    if not cleaned:
        raise InstallerError("游戏可执行文件名为空")
    tail = PurePosixPath(cleaned.replace("\\", "/")).name
    if not tail or tail in (".", ".."):
        raise InstallerError(f"无法从 {name!r} 解析出可执行文件名")
    if not tail.casefold().endswith(".exe"):
        tail += ".exe"
    if re.search(r'[<>:"/\\|?*]', tail) or any(ord(ch) < 32 for ch in tail):
        raise InstallerError(f"可执行文件名含非法字符：{tail}")
    return tail


def profile_overrides(fps: int, *, osd: bool) -> dict[str, dict[str, str]]:
    """RTSS keys to force; everything else stays inherited from Global."""
    if fps < 0 or fps > MAX_FPS:
        raise InstallerError(f"限帧值超出范围（0-{MAX_FPS}）：{fps}")
    return {
        # Limit=0 disables the limiter, which is how a cap gets removed again.
        "Framerate": {"Limit": str(fps), "LimitDenominator": "1"},
        "OSD": {"EnableOSD": "1" if osd else "0"},
    }


def merge_profile(text: str, overrides: dict[str, dict[str, str]]) -> str:
    """Apply ``overrides`` to an RTSS profile, preserving unrelated settings."""
    lines = text.splitlines()
    section_of_line: list[str] = []
    current = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip().casefold()
        section_of_line.append(current)

    for section, values in overrides.items():
        key_of = section.casefold()
        for key, value in values.items():
            replacement = f"{key:<40}= {value}"
            for index, line in enumerate(lines):
                if section_of_line[index] != key_of:
                    continue
                head = line.split("=", 1)[0] if "=" in line else ""
                if head.strip().casefold() == key.casefold():
                    lines[index] = replacement
                    break
            else:
                last = max(
                    (i for i, name in enumerate(section_of_line) if name == key_of),
                    default=None,
                )
                if last is None:
                    if lines and lines[-1].strip():
                        lines.append("")
                        section_of_line.append(current)
                    lines.append(f"[{section}]")
                    section_of_line.append(key_of)
                    lines.append(replacement)
                    section_of_line.append(key_of)
                else:
                    lines.insert(last + 1, replacement)
                    section_of_line.insert(last + 1, key_of)
    return "\r\n".join(lines).rstrip("\r\n") + "\r\n"


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        # No BOM: the private-profile API would fold it into the first section
        # header and silently lose that section.
        with temporary.open("w", encoding=PROFILE_ENCODING, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_profile(
    install_dir: Path, exe_name: str, fps: int, *, osd: bool
) -> Path:
    profile = install_dir / PROFILES_DIRNAME / f"{normalize_exe_name(exe_name)}.cfg"
    existing = ""
    if profile.is_file():
        try:
            existing = profile.read_text(encoding=PROFILE_ENCODING)
        except (OSError, UnicodeDecodeError) as exc:
            raise InstallerError(f"无法读取现有配置 {profile}：{exc}") from exc
    _write_text_atomic(profile, merge_profile(existing, profile_overrides(fps, osd=osd)))
    return profile


# --------------------------------------------------------------------------
# Installed copy discovery
# --------------------------------------------------------------------------


def _registry_install_locations() -> list[Path]:
    if winreg is None:
        return []
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    views = [0]
    for attr in ("KEY_WOW64_32KEY", "KEY_WOW64_64KEY"):
        view = getattr(winreg, attr, 0)
        if view and view not in views:
            views.append(view)
    found: list[Path] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in views:
            try:
                with winreg.OpenKey(hive, base, 0, winreg.KEY_READ | view) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    names = [winreg.EnumKey(key, index) for index in range(count)]
            except OSError:
                continue
            for name in names:
                try:
                    with winreg.OpenKey(
                        hive, f"{base}\\{name}", 0, winreg.KEY_READ | view
                    ) as entry:
                        display = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                        if INSTALL_DIR_NAME.casefold() not in display.casefold():
                            continue
                        for value_name in ("InstallLocation", "InstallPath"):
                            try:
                                value = winreg.QueryValueEx(entry, value_name)[0]
                            except OSError:
                                continue
                            if value:
                                found.append(
                                    Path(os.path.expandvars(str(value)).strip('"'))
                                )
                except OSError:
                    continue
    return found


def is_rtss_dir(path: Path) -> bool:
    return (path / SERVER_EXE).is_file()


def find_rtss_install() -> Path | None:
    candidates: list[Path] = []
    value = os.environ.get("RTSS_DIR", "").strip().strip('"')
    if value:
        candidates.append(Path(value))
    candidates.extend(_registry_install_locations())
    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(env_name, "").strip()
        if root:
            candidates.append(Path(root) / INSTALL_DIR_NAME)
    candidates.extend(DEFAULT_INSTALL_DIRS)
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        if is_rtss_dir(candidate):
            return candidate.resolve()
    return None


# --------------------------------------------------------------------------
# Download, verify, install
# --------------------------------------------------------------------------


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    request = urllib.request.Request(
        url, headers={"User-Agent": "game-recorder-rtss-installer"}
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            with temporary.open("wb") as output:
                copied = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_ARCHIVE_BYTES:
                        raise InstallerError("下载内容超过安全大小限制")
                    output.write(chunk)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_archive(explicit: Path | None, *, allow_unknown: bool) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise InstallerError(f"RTSS 安装包不存在：{explicit}")
        archive = explicit.resolve()
    else:
        archive = DOWNLOAD_CACHE / RTSS_ARCHIVE_NAME
        if archive.is_file() and sha256_file(archive) != RTSS_ARCHIVE_SHA256:
            _print("[警告] 缓存的安装包摘要不符，重新下载。")
            archive.unlink()
        if not archive.is_file():
            _print(f"正在下载 RTSS {RTSS_VERSION}（约 18MB）…")
            _print(f"  {RTSS_ARCHIVE_URL}")
            try:
                _download(RTSS_ARCHIVE_URL, archive)
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                raise InstallerError(
                    f"RTSS 下载失败：{exc}\n"
                    "  可手动从 https://www.guru3d.com/download/"
                    "rtss-rivatuner-statistics-server-download/ 下载后用 "
                    "--installer-zip 指定"
                ) from exc

    digest = sha256_file(archive)
    if digest == RTSS_ARCHIVE_SHA256:
        _print(f"安装包 SHA-256 已匹配内置官方版本：{archive.name}")
        return archive
    if allow_unknown:
        _print(f"[警告] 使用未收录的安装包：{archive.name}\n  SHA-256: {digest}")
        return archive
    raise InstallerError(
        f"RTSS 安装包 SHA-256 与已知官方版本不符：{archive.name}\n"
        f"  expected: {RTSS_ARCHIVE_SHA256}\n  actual:   {digest}\n"
        "  确认它来自 guru3d.com 官方后可传入 --allow-unknown-zip"
    )


def extract_setup(archive: Path, destination: Path) -> Path:
    """Pull the single NSIS setup out of the Guru3D zip."""
    destination.mkdir(parents=True, exist_ok=True)
    try:
        data = archive.read_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
            infos = [info for info in zip_file.infolist() if not info.is_dir()]
            if len(infos) > MAX_ZIP_ENTRIES:
                raise InstallerError(f"ZIP 条目过多：{len(infos)}")
            match = next(
                (
                    info
                    for info in infos
                    if PurePosixPath(
                        info.filename.replace("\\", "/")
                    ).name.casefold()
                    == SETUP_NAME.casefold()
                ),
                None,
            )
            if match is None:
                raise InstallerError(f"ZIP 中未找到 {SETUP_NAME}")
            if match.file_size > MAX_ZIP_MEMBER_BYTES:
                raise InstallerError(f"ZIP 条目过大：{match.filename}")
            setup = destination / SETUP_NAME
            with zip_file.open(match) as source, setup.open("wb") as output:
                shutil.copyfileobj(source, output)
    except (OSError, zipfile.BadZipFile) as exc:
        if isinstance(exc, InstallerError):
            raise
        raise InstallerError(f"无法解压 {archive}：{exc}") from exc
    validate_pe_x86(setup, label=SETUP_NAME)
    return setup


def authenticode_signer(path: Path) -> tuple[str, str]:
    """Return ``(status, signer subject)`` as reported by Windows."""
    command = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "$s = Get-AuthenticodeSignature -LiteralPath $env:RTSS_SETUP_PATH;"
        "Write-Output $s.Status;"
        "Write-Output $s.SignerCertificate.Subject",
    ]
    environment = dict(os.environ, RTSS_SETUP_PATH=str(path))
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallerError(f"无法校验数字签名：{exc}") from exc
    if result.returncode:
        raise InstallerError(
            "无法校验数字签名：" + (result.stderr.strip() or str(result.returncode))
        )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise InstallerError("数字签名校验没有返回结果")
    return lines[0], " ".join(lines[1:])


def _common_name(subject: str) -> str:
    """Pull CN out of an X.500 subject, tolerating commas inside quotes."""
    match = re.search(r'CN=(?:"([^"]*)"|([^,]*))', subject)
    if match is None:
        return subject
    return (match.group(1) or match.group(2) or subject).strip()


def verify_setup_signature(setup: Path, *, allow_unsigned: bool) -> None:
    status, subject = authenticode_signer(setup)
    if status == "Valid" and SETUP_SIGNER_FRAGMENT in subject.casefold():
        _print(f"数字签名有效：{_common_name(subject)}")
        return
    message = f"RTSS 安装程序签名异常（状态 {status}）：{subject or '无签名'}"
    if allow_unsigned:
        _print(f"[警告] {message}")
        return
    raise InstallerError(message + "；确认来源可信后可传入 --allow-unsigned")


def run_setup(setup: Path) -> Path:
    _print(f"正在静默安装 RTSS {RTSS_VERSION} …")
    try:
        result = subprocess.run(
            [str(setup), *SETUP_SILENT_ARGS],
            check=False,
            cwd=str(setup.parent),
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallerError(f"RTSS 安装程序启动失败：{exc}") from exc
    if result.returncode:
        raise InstallerError(f"RTSS 安装失败（exit {result.returncode}）")
    # The NSIS installer returns before the last files land on slow disks.
    deadline = time.monotonic() + INSTALL_POLL_SECONDS
    while True:
        install_dir = find_rtss_install()
        if install_dir is not None:
            return install_dir
        if time.monotonic() >= deadline:
            raise InstallerError("安装程序已退出，但未找到 RTSS 安装目录")
        time.sleep(1.0)


# --------------------------------------------------------------------------
# Server process
# --------------------------------------------------------------------------


def _tasklist_names() -> set[str]:
    if os.name != "nt":
        return set()
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallerError(f"无法枚举进程：{exc}") from exc
    if result.returncode:
        raise InstallerError(
            "无法枚举进程：" + (result.stderr.strip() or str(result.returncode))
        )
    return {
        row[0].strip().casefold()
        for row in csv.reader(io.StringIO(result.stdout))
        if row and row[0].strip()
    }


def rtss_running() -> bool:
    return SERVER_EXE.casefold() in _tasklist_names()


def running_games(exe_names: Iterable[str]) -> list[str]:
    active = _tasklist_names()
    return sorted(name for name in exe_names if name.casefold() in active)


def stop_rtss() -> None:
    """RTSS rewrites every loaded profile on exit, so it must be down first."""
    subprocess.run(
        ["taskkill", "/IM", SERVER_EXE, "/F"],
        check=False,
        capture_output=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 30
    while rtss_running():
        if time.monotonic() >= deadline:
            raise InstallerError(f"无法结束 {SERVER_EXE}")
        time.sleep(0.5)


def start_rtss(install_dir: Path) -> None:
    server = install_dir / SERVER_EXE
    if not server.is_file():
        raise InstallerError(f"未找到 {server}")
    detached = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [str(server)],
            cwd=str(install_dir),
            close_fds=True,
            creationflags=detached,
        )
    except OSError as exc:
        raise InstallerError(f"无法启动 {SERVER_EXE}：{exc}") from exc
    deadline = time.monotonic() + 30
    while not rtss_running():
        if time.monotonic() >= deadline:
            raise InstallerError(f"{SERVER_EXE} 启动后未出现在进程列表中")
        time.sleep(0.5)


# --------------------------------------------------------------------------
# Elevation
# --------------------------------------------------------------------------


def _can_write(directory: Path) -> bool:
    probe = directory / f".rtss-write-probe-{uuid.uuid4().hex}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        return True
    except OSError:
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def is_elevated() -> bool:
    if os.name != "nt":
        return True
    import ctypes

    try:
        return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


def needs_elevation(install_dir: Path | None) -> bool:
    if os.name != "nt" or is_elevated():
        return False
    if install_dir is None:
        return True
    return not _can_write(install_dir / PROFILES_DIRNAME)


def elevate_and_wait(argv: list[str]) -> int:
    """Re-run this script as administrator, relaying its output back."""
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

    DOWNLOAD_CACHE.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    log_file = DOWNLOAD_CACHE / f"elevate-{token}.log"
    child = [str(Path(__file__).resolve()), *argv, "--skip-elevation"]
    # ShellExecute cannot inherit this console, so the child logs to a file.
    helper = DOWNLOAD_CACHE / f"elevate-{token}.cmd"
    helper.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "set PYTHONIOENCODING=utf-8\r\n"
        "set PYTHONUTF8=1\r\n"
        f'cd /d "{PROJECT_ROOT}"\r\n'
        f"{subprocess.list2cmdline([sys.executable, *child])}"
        f' > "{log_file}" 2>&1\r\n'
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8",
    )

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(helper)
    info.lpDirectory = str(PROJECT_ROOT)
    info.nShow = 1
    code = wintypes.DWORD()
    ctypes.set_last_error(0)
    try:
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            error = ctypes.get_last_error()
            if error == 1223:
                raise InstallerSkipped("用户取消了 UAC 提权")
            raise InstallerError(f"无法启动 UAC 子进程（Windows 错误 {error}）")
        if not info.hProcess:
            raise InstallerError("UAC 子进程未返回进程句柄")
        try:
            if kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF) != 0:
                raise InstallerError("等待 UAC 子进程失败")
            if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
                raise InstallerError("无法取得 UAC 子进程退出码")
        finally:
            kernel32.CloseHandle(info.hProcess)
    finally:
        helper.unlink(missing_ok=True)

    if log_file.is_file():
        text = log_file.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            _print("—— 管理员安装日志 ——")
            _print(text)
            _print("—— 日志结束 ——")
        log_file.unlink(missing_ok=True)
    return int(code.value)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def prompt_fps(default: int) -> int:
    answer = input(
        f"限帧 FPS [回车用 {default}，输入 n 跳过 RTSS]："
    ).strip()
    if not answer:
        return default
    if answer.casefold() in ("n", "no", "skip", "跳过"):
        raise InstallerSkipped("用户跳过 RTSS 限帧配置")
    if not answer.isdigit():
        raise InstallerError(f"限帧值必须是整数：{answer}")
    return int(answer)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安装 RTSS 并为游戏注册帧率上限（录制画面不会出现 OSD）"
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=DEFAULT_FPS,
        help=f"帧率上限，0 表示取消限制（默认 {DEFAULT_FPS}）",
    )
    parser.add_argument(
        "--game-exe",
        action="append",
        metavar="NAME.EXE",
        help=f"要限帧的游戏主程序名，可重复（默认 {DEFAULT_GAME_EXE}）",
    )
    parser.add_argument(
        "--osd",
        action="store_true",
        help="保留 RTSS 屏幕显示；注意它会被一并录进视频，仅用于验证限帧是否生效",
    )
    parser.add_argument(
        "--installer-zip", type=Path, help="本地 RTSS 官方 ZIP（跳过下载）"
    )
    parser.add_argument(
        "--allow-unknown-zip",
        action="store_true",
        help="允许使用未收录 SHA-256 的新版官方 ZIP",
    )
    parser.add_argument(
        "--allow-unsigned", action="store_true", help="允许签名校验不通过的安装程序"
    )
    parser.add_argument(
        "--no-start", action="store_true", help="配置完成后不启动 RTSS"
    )
    parser.add_argument("--no-prompt", action="store_true", help="禁止交互输入")
    parser.add_argument("--skip-elevation", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    original_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    _print("============================================================")
    _print("  RTSS 限帧工具自动安装")
    _print("============================================================")
    if os.name != "nt":
        _print("[错误] 此安装器只能在 Windows 上运行。")
        return 1
    try:
        games = [
            normalize_exe_name(name)
            for name in (args.game_exe or [DEFAULT_GAME_EXE])
        ]
        fps = args.fps if args.no_prompt else prompt_fps(args.fps)
        if fps < 0 or fps > MAX_FPS:
            raise InstallerError(f"限帧值超出范围（0-{MAX_FPS}）：{fps}")

        install_dir = find_rtss_install()
        if install_dir is not None:
            _print(f"RTSS 已安装：{install_dir}")

        if not args.skip_elevation and needs_elevation(install_dir):
            _print("安装 RTSS / 写入配置需要管理员权限，正在请求 UAC …")
            return elevate_and_wait(original_argv)

        if install_dir is None:
            archive = resolve_archive(
                args.installer_zip, allow_unknown=args.allow_unknown_zip
            )
            setup = extract_setup(archive, DOWNLOAD_CACHE / "setup")
            verify_setup_signature(setup, allow_unsigned=args.allow_unsigned)
            install_dir = run_setup(setup)
            _print(f"RTSS 已安装到：{install_dir}")

        active = running_games(games)
        if active:
            _print(f"[提示] 游戏正在运行（{'、'.join(active)}），新的限帧需重启游戏生效。")

        # A running server rewrites every loaded profile when it exits, which
        # would undo the file we are about to write.
        was_running = rtss_running()
        if was_running:
            stop_rtss()
        profiles = [
            write_profile(install_dir, name, fps, osd=args.osd) for name in games
        ]
        # --no-start means "do not newly launch it", not "leave it killed".
        running = was_running or not args.no_start
        if running:
            start_rtss(install_dir)
    except InstallerSkipped as exc:
        _print(f"[跳过] {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001 - surfaced as a user-facing message
        _print(f"[错误] {exc}")
        return 1

    _print()
    _print("[成功] RTSS 限帧已配置完成。")
    for profile in profiles:
        _print(f"  配置文件：{profile}")
    _print(f"  帧率上限：{'不限制' if fps == 0 else str(fps) + ' FPS'}")
    _print(f"  屏幕显示：{'开启（会被录进视频）' if args.osd else '关闭'}")
    if running:
        _print("  RTSS 已在后台运行；游戏须在 RTSS 启动之后再打开。")
    else:
        _print(f"  RTSS 未启动；开游戏前请先运行 {install_dir / SERVER_EXE}")
    _print("  游戏内请关闭垂直同步与三重缓冲，避免与 RTSS 限帧冲突。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
