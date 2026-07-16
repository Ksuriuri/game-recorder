#!/usr/bin/env python3
"""Install Cyberpunk 2077 camera logger: RED4ext + CET + in-game mod."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:
    import winreg  # type: ignore[import-not-found]
except ImportError:
    winreg = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAMERA_ROOT = PROJECT_ROOT / "cp2077-camera"
PAYLOAD_ROOT = CAMERA_ROOT / "payload" / "cyber_engine_tweaks" / "mods" / "CameraFrameLogger"
VENDOR_RED4EXT = CAMERA_ROOT / "vendor" / "RED4ext"
VENDOR_CET = CAMERA_ROOT / "vendor" / "CET"
CACHE_DIR = PROJECT_ROOT / ".tools" / "cp2077-camera-cache"
CONTROL_DIRNAME = ".cp2077_camera"
MOD_DEST_NAME = "CameraFrameLogger"
GAME_EXE_REL = Path("bin") / "x64" / "Cyberpunk2077.exe"
PLUGINS_REL = Path("bin") / "x64" / "plugins"
CET_REL = PLUGINS_REL / "cyber_engine_tweaks"
CP2077_STEAM_APP_ID = "1091500"
RED4EXT_RELEASE_URL = (
    "https://github.com/WopsS/RED4ext/releases/download/v1.30.0/red4ext-1.30.0.zip"
)
CET_RELEASE_URL = (
    "https://github.com/maximegmd/CyberEngineTweaks/releases/download/v1.37.1/cet_1.37.1.zip"
)


def _print(message: str = "") -> None:
    print(message, flush=True)


def is_cp2077_dir(path: Path) -> bool:
    return (path / GAME_EXE_REL).is_file()


def red4ext_installed(game: Path) -> bool:
    return any(
        path.is_file()
        for path in (
            game / "red4ext.toml",
            game / "red4ext" / "RED4ext.dll",
            game / "bin" / "x64" / "RED4ext.dll",
            game / "bin" / "x64" / "winmm.dll",
        )
    )


def cet_installed(game: Path) -> bool:
    plugins = game / PLUGINS_REL
    return (plugins / "cyber_engine_tweaks.asi").is_file() and (
        plugins / "cyber_engine_tweaks"
    ).is_dir()


def camera_mod_installed(game: Path) -> bool:
    mod = game / CET_REL / "mods" / MOD_DEST_NAME / "init.lua"
    return mod.is_file()


def _steam_library_roots() -> list[Path]:
    if winreg is None:
        return []
    roots: list[Path] = []
    for hive, subkey in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                install, _ = winreg.QueryValueEx(key, "InstallPath")
                if install:
                    roots.append(Path(install))
        except OSError:
            continue
    return roots


def _library_paths_from_vdf(steam_root: Path) -> list[Path]:
    libs = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        return libs
    text = vdf.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        if '"path"' not in line:
            continue
        parts = line.split('"')
        if len(parts) >= 4 and parts[3]:
            libs.append(Path(parts[3]))
    return libs


def _acf_install_dir(acf_path: Path) -> str | None:
    text = acf_path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r'"installdir"\s+"([^"]+)"', text)
    return match.group(1) if match else None


def find_cp2077_candidates() -> list[Path]:
    seen: set[Path] = set()
    candidates: list[Path] = []

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(resolved)

    env = os.environ.get("CP2077_DIR", "").strip()
    if env:
        add(Path(env))

    for steam in _steam_library_roots():
        for lib in _library_paths_from_vdf(steam):
            manifest = lib / "steamapps" / f"appmanifest_{CP2077_STEAM_APP_ID}.acf"
            if manifest.is_file():
                installdir = _acf_install_dir(manifest)
                if installdir:
                    add(lib / "steamapps" / "common" / installdir)
            add(lib / "steamapps" / "common" / "Cyberpunk 2077")

    for path in (
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\Cyberpunk 2077"),
        Path(r"D:\SteamLibrary\steamapps\common\Cyberpunk 2077"),
        Path(r"E:\SteamLibrary\steamapps\common\Cyberpunk 2077"),
        Path(r"I:\SteamLibrary\steamapps\common\Cyberpunk 2077"),
        Path(r"C:\Games\Cyberpunk 2077"),
        Path(r"D:\Games\Cyberpunk 2077"),
        Path(r"I:\Games\Cyberpunk 2077"),
        Path(r"C:\Program Files\GOG Galaxy\Games\Cyberpunk 2077"),
        Path(r"D:\GOG Games\Cyberpunk 2077"),
        Path(r"I:\GOG Games\Cyberpunk 2077"),
        Path(r"I:\Steam\steamapps\common\Cyberpunk 2077"),
        Path(r"I:\reocording_\Cyberpunk 2077"),
    ):
        add(path)

    return [path for path in candidates if is_cp2077_dir(path)]


def resolve_cp2077_dir(explicit: Path | None, *, prompt: bool) -> tuple[Path | None, bool]:
    if explicit is not None:
        if is_cp2077_dir(explicit):
            return explicit.resolve(), False
        _print(f"[错误] 不是有效的赛博朋克 2077 目录：{explicit}")
        if not prompt:
            return None, False

    cands = find_cp2077_candidates()
    if len(cands) == 1 and explicit is None:
        return cands[0], False
    if len(cands) > 1 and explicit is None:
        _print("检测到多个赛博朋克 2077 安装：")
        for i, path in enumerate(cands, 1):
            _print(f"  [{i}] {path}")
        if not prompt:
            _print("请用 --cp2077-dir 指定，或设置环境变量 CP2077_DIR。")
            return None, False
        try:
            choice = input(
                f"选择 [1-{len(cands)}]，或输入完整路径；直接回车跳过: "
            ).strip().strip('"')
        except EOFError:
            choice = ""
        if not choice:
            _print("[跳过] 未安装赛博朋克 2077 相机插件。")
            return None, True
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(cands):
                return cands[idx], False
            _print("[错误] 无效选项。")
            return None, False
        path = Path(choice)
        if is_cp2077_dir(path):
            return path.resolve(), False
        _print(f"[错误] 目录无效（需要含 {GAME_EXE_REL.as_posix()}）：{path}")
        return None, False

    if not prompt:
        _print("[错误] 未找到赛博朋克 2077。设置 CP2077_DIR 或传入 --cp2077-dir。")
        return None, False

    _print("未自动找到赛博朋克 2077。")
    while True:
        try:
            typed = input(
                "请输入游戏根目录（含 bin/x64/Cyberpunk2077.exe；直接回车跳过）: "
            ).strip().strip('"')
        except EOFError:
            typed = ""
        if not typed:
            _print("[跳过] 未安装赛博朋克 2077 相机插件。")
            return None, True
        path = Path(typed)
        if is_cp2077_dir(path):
            return path.resolve(), False
        _print(f"[错误] 不是有效目录（未找到 {GAME_EXE_REL.as_posix()}）：{path}")


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "game-recorder-cp2077-camera-installer"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _find_vendor_zip(vendor_dir: Path, pattern: str) -> Path | None:
    if not vendor_dir.is_dir():
        return None
    matches = sorted(vendor_dir.glob(pattern), key=lambda p: p.name)
    return matches[-1] if matches else None


def _resolve_zip(
    *,
    vendor_dir: Path,
    vendor_glob: str,
    cache_name: str,
    url: str,
    allow_download: bool,
) -> Path:
    vendored = _find_vendor_zip(vendor_dir, vendor_glob)
    if vendored is not None:
        _print(f"  使用内置 vendor：{vendored.name}")
        return vendored

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / cache_name
    if cached.is_file():
        _print(f"  使用缓存：{cached.name}")
        return cached

    if not allow_download:
        raise FileNotFoundError(
            f"缺少 {cache_name}。请联网重试，或将 zip 放入 {vendor_dir}"
        )

    _print(f"  正在下载 {cache_name} …")
    _download(url, cached)
    return cached


def _extract_zip_into_game(zip_path: Path, game: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            target = game / Path(member.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def install_red4ext(game: Path, *, allow_download: bool) -> None:
    if red4ext_installed(game):
        _print("RED4ext: 已安装，跳过。")
        return
    zip_path = _resolve_zip(
        vendor_dir=VENDOR_RED4EXT,
        vendor_glob="red4ext-*.zip",
        cache_name="red4ext-1.30.0.zip",
        url=RED4EXT_RELEASE_URL,
        allow_download=allow_download,
    )
    _print("正在安装 RED4ext …")
    _extract_zip_into_game(zip_path, game)
    if not red4ext_installed(game):
        raise RuntimeError("RED4ext 安装后校验失败")


def install_cet(game: Path, *, allow_download: bool) -> None:
    if cet_installed(game):
        _print("Cyber Engine Tweaks: 已安装，跳过。")
        return
    zip_path = _resolve_zip(
        vendor_dir=VENDOR_CET,
        vendor_glob="cet_*.zip",
        cache_name="cet_1.37.1.zip",
        url=CET_RELEASE_URL,
        allow_download=allow_download,
    )
    _print("正在安装 Cyber Engine Tweaks …")
    _extract_zip_into_game(zip_path, game)
    if not cet_installed(game):
        raise RuntimeError("Cyber Engine Tweaks 安装后校验失败")


def configure_mod_sandbox(mod_dir: Path, recordings_root: Path) -> Path:
    """Connect the recorder to CET's sandbox-local control/raw files."""
    control = mod_dir / "active_session.json"
    if not control.is_file():
        control.write_text(
            json.dumps({"status": "idle", "updated_at_ms": 0}, indent=2) + "\n",
            encoding="utf-8",
        )

    cfg = {
        "control_file": "active_session.json",
        "raw_file": "camera_raw_cp2077.jsonl",
        "io_scope": "cet_mod_sandbox",
    }
    config_path = mod_dir / "config.json"
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    state_dir = Path(recordings_root).resolve().parent / CONTROL_DIRNAME
    state_dir.mkdir(parents=True, exist_ok=True)
    install_state = {
        "schema": "cp2077_camera_install_v1",
        "mod_dir": str(mod_dir.resolve()),
        "control_file": "active_session.json",
        "raw_file": "camera_raw_cp2077.jsonl",
    }
    (state_dir / "install.json").write_text(
        json.dumps(install_state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return control


def install_mod(game: Path, recordings_root: Path) -> Path:
    if not PAYLOAD_ROOT.is_dir():
        raise FileNotFoundError(f"缺少插件 payload：{PAYLOAD_ROOT}")

    dest = game / CET_REL / "mods" / MOD_DEST_NAME
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PAYLOAD_ROOT, dest, dirs_exist_ok=True)
    configure_mod_sandbox(dest, recordings_root)
    return dest


def uninstall_mod(game: Path) -> bool:
    dest = game / CET_REL / "mods" / MOD_DEST_NAME
    if dest.exists():
        shutil.rmtree(dest)
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装赛博朋克 2077 相机位姿采集插件")
    parser.add_argument("--cp2077-dir", type=Path, default=None, help="赛博朋克 2077 安装目录")
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=PROJECT_ROOT / "recordings",
        help="game-recorder 输出目录（默认项目 recordings/）",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="无人值守：找不到游戏时跳过，不询问路径",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="不联网下载 RED4ext/CET（仅用 vendor 或缓存）",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="仅卸载 CameraFrameLogger 模组",
    )
    parser.add_argument(
        "--prefetch-deps",
        action="store_true",
        help="仅下载 RED4ext/CET 到 vendor 缓存（供离线包使用，不需要游戏目录）",
    )
    args = parser.parse_args(argv)

    _print("============================================================")
    _print("  赛博朋克 2077 相机轨迹插件")
    _print("============================================================")

    if args.prefetch_deps:
        try:
            _resolve_zip(
                vendor_dir=VENDOR_RED4EXT,
                vendor_glob="red4ext-*.zip",
                cache_name="red4ext-1.30.0.zip",
                url=RED4EXT_RELEASE_URL,
                allow_download=True,
            )
            _resolve_zip(
                vendor_dir=VENDOR_CET,
                vendor_glob="cet_*.zip",
                cache_name="cet_1.37.1.zip",
                url=CET_RELEASE_URL,
                allow_download=True,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            _print(f"[错误] 预下载失败：{exc}")
            return 1
        _print("依赖已缓存到 .tools\\cp2077-camera-cache\\")
        return 0

    game, skipped = resolve_cp2077_dir(args.cp2077_dir, prompt=not args.no_prompt)
    if game is None:
        return 3 if skipped else 1
    _print(f"游戏目录: {game}")

    if args.uninstall:
        removed = uninstall_mod(game)
        _print("已卸载 CameraFrameLogger。" if removed else "未找到已安装的 CameraFrameLogger。")
        return 0

    allow_download = not args.skip_download and os.environ.get("UV_OFFLINE") != "1"
    try:
        install_red4ext(game, allow_download=allow_download)
        install_cet(game, allow_download=allow_download)
        dest = install_mod(game, Path(args.recordings_dir))
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, FileNotFoundError) as exc:
        _print(f"[错误] {exc}")
        if not allow_download:
            _print("  离线模式需要先将 red4ext-*.zip / cet_*.zip 放入 cp2077-camera\\vendor\\")
        else:
            _print("  请检查网络后重试，或手动将 zip 放入 cp2077-camera\\vendor\\ 后加 --skip-download")
        return 1

    required = (
        dest / "init.lua",
        dest / "config.json",
        dest / "active_session.json",
        Path(args.recordings_dir).resolve().parent / CONTROL_DIRNAME / "install.json",
        game / GAME_EXE_REL,
        game / PLUGINS_REL / "cyber_engine_tweaks.asi",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        _print("[错误] 安装后校验失败，缺少：")
        for item in missing:
            _print(f"  - {item}")
        return 1

    control = dest / "active_session.json"
    _print()
    _print("安装完成（RED4ext + CET + CameraFrameLogger）。")
    _print(f"  游戏目录: {game}")
    _print(f"  CET 插件: {dest}")
    _print(f"  同步信号: {control}")
    _print("  下一步: 启动游戏 → run.bat 录制 → session 内应有 camera.jsonl")
    _print("============================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
