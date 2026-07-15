#!/usr/bin/env python3
"""Safely uninstall the Black Myth: Wukong camera payload on Windows."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from install_wukong_camera import (
        PROJECT_ROOT,
        LEGACY_LOADER_FILENAME,
        MODS_TXT_RELATIVE,
        STATE_DIRNAME,
        STATE_FILENAME,
        ChangeSnapshot,
        GameLayout,
        InstallerError,
        _copy_path,
        _fsync_directory,
        _fsync_path,
        _is_within,
        _print,
        _read_state,
        _remove_path,
        _sha256,
        _write_durable_marker,
        elevate_and_wait,
        ensure_game_closed,
        migrate_state_v2,
        needs_elevation,
        recover_interrupted_transaction,
        resolve_wukong_dir,
        validate_existing_state,
    )
except ImportError:  # Supports importing as scripts.uninstall_wukong_camera.
    from scripts.install_wukong_camera import (  # type: ignore[no-redef]
        PROJECT_ROOT,
        LEGACY_LOADER_FILENAME,
        MODS_TXT_RELATIVE,
        STATE_DIRNAME,
        STATE_FILENAME,
        ChangeSnapshot,
        GameLayout,
        InstallerError,
        _copy_path,
        _fsync_directory,
        _fsync_path,
        _is_within,
        _print,
        _read_state,
        _remove_path,
        _sha256,
        _write_durable_marker,
        elevate_and_wait,
        ensure_game_closed,
        migrate_state_v2,
        needs_elevation,
        recover_interrupted_transaction,
        resolve_wukong_dir,
        validate_existing_state,
    )


def _validated_backup_paths(
    state: dict[str, Any],
    layout: GameLayout,
    state_dir: Path,
) -> tuple[Path | None, Path | None, Path | None]:
    validate_existing_state(state, layout, state_dir)
    original_root = (state_dir / "backups" / "original").resolve()
    if not _is_within(original_root, state_dir):
        raise InstallerError("原始备份目录越出安装状态目录")

    backup_dwmapi: Path | None = None
    backup_xinput: Path | None = None
    backup_ue4ss: Path | None = None
    if state["had_original_dwmapi"]:
        backup_dwmapi = Path(state["original_dwmapi_backup_path"]).resolve()
        if (
            backup_dwmapi != original_root / "dwmapi.dll"
            or not _is_within(backup_dwmapi, state_dir)
        ):
            raise InstallerError("dwmapi.dll 原始备份路径不安全")
    if state["had_original_ue4ss"]:
        backup_ue4ss = Path(state["original_ue4ss_backup_path"]).resolve()
        if (
            backup_ue4ss != original_root / "ue4ss"
            or not _is_within(backup_ue4ss, state_dir)
        ):
            raise InstallerError("UE4SS 原始备份路径不安全")
    if state["had_original_xinput"]:
        backup_xinput = Path(state["original_xinput_backup_path"]).resolve()
        if (
            backup_xinput != original_root / LEGACY_LOADER_FILENAME
            or not _is_within(backup_xinput, state_dir)
        ):
            raise InstallerError(f"{LEGACY_LOADER_FILENAME} 原始备份路径不安全")
    return backup_dwmapi, backup_xinput, backup_ue4ss


def _tree_hashes(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise InstallerError(f"UE4SS 路径不是安全目录：{root}")
    hashes: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise InstallerError(f"UE4SS 目录含符号链接，拒绝删除：{path}")
        if path.is_file():
            hashes[path.relative_to(root).as_posix().casefold()] = _sha256(path)
        elif not path.is_dir():
            raise InstallerError(f"UE4SS 目录含未知文件类型，拒绝删除：{path}")
    return hashes


def verify_installed_ownership(
    state: dict[str, Any],
    layout: GameLayout,
    backup_ue4ss: Path | None,
) -> None:
    """Refuse destructive uninstall if files changed after installation."""
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
                f"安装后出现 {LEGACY_LOADER_FILENAME}，拒绝覆盖；请先确认并移走该文件"
            )
    managed_files = state.get("managed_files")
    managed_hashes = state.get("managed_hashes")
    if not isinstance(managed_files, list) or not isinstance(managed_hashes, dict):
        raise InstallerError("安装状态缺少受管文件哈希，不能安全卸载")

    managed_under_ue4ss: set[str] = set()
    for relative_text in managed_files:
        if not isinstance(relative_text, str):
            raise InstallerError("安装状态中的受管文件路径无效")
        relative = Path(relative_text)
        expected_hash = managed_hashes.get(relative_text)
        if not isinstance(expected_hash, str):
            raise InstallerError(f"安装状态缺少文件哈希：{relative_text}")
        destination = (layout.win64 / relative).resolve()
        if not _is_within(destination, layout.win64) or not destination.is_file():
            raise InstallerError(f"受管文件已被移动或删除，拒绝覆盖式卸载：{relative_text}")
        if relative != MODS_TXT_RELATIVE and _sha256(destination) != expected_hash:
            raise InstallerError(f"受管文件安装后被修改，拒绝覆盖式卸载：{relative_text}")
        parts = relative.parts
        if parts and parts[0].casefold() == "ue4ss":
            managed_under_ue4ss.add(
                Path(*parts[1:]).as_posix().casefold()
            )

    expected_config = (
        layout.win64 / "ue4ss" / "Mods" / "CameraFrameLogger" / "config.lua"
    ).resolve()
    config_text = state.get("dynamic_config")
    config_hash = state.get("dynamic_config_sha256")
    if (
        not isinstance(config_text, str)
        or not isinstance(config_hash, str)
        or Path(config_text).resolve() != expected_config
        or not expected_config.is_file()
        or _sha256(expected_config) != config_hash
    ):
        raise InstallerError("动态 config.lua 安装后被修改或缺失，拒绝覆盖式卸载")
    config_relative = expected_config.relative_to(
        (layout.win64 / "ue4ss").resolve()
    )
    managed_under_ue4ss.add(config_relative.as_posix().casefold())

    current_hashes = _tree_hashes(layout.win64 / "ue4ss")
    original_hashes = _tree_hashes(backup_ue4ss) if backup_ue4ss else {}
    allowed = set(original_hashes) | managed_under_ue4ss
    additions = sorted(set(current_hashes) - allowed)
    if additions:
        raise InstallerError(
            "检测到安装后新增的 UE4SS 文件，拒绝删除以免丢失："
            + "、".join(additions[:5])
            + ("…" if len(additions) > 5 else "")
        )

    changed_originals = sorted(
        relative
        for relative, original_hash in original_hashes.items()
        if relative not in managed_under_ue4ss
        and current_hashes.get(relative) != original_hash
    )
    if changed_originals:
        raise InstallerError(
            "检测到安装后修改的原有 UE4SS 文件，拒绝删除以免丢失："
            + "、".join(changed_originals[:5])
            + ("…" if len(changed_originals) > 5 else "")
        )


def _mods_text_after_uninstall(
    current_text: str,
    original_text: str | None,
) -> str:
    logger_pattern = r"^\s*CameraFrameLogger\s*:"
    current_lines = [
        line
        for line in current_text.splitlines()
        if not re.match(logger_pattern, line, flags=re.IGNORECASE)
    ]
    original_logger_lines = (
        [
            line
            for line in original_text.splitlines()
            if re.match(
                logger_pattern,
                line,
                flags=re.IGNORECASE,
            )
        ]
        if original_text is not None
        else []
    )
    lines = current_lines + original_logger_lines
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")


def uninstall(layout: GameLayout) -> tuple[bool, bool, bool]:
    state_dir = layout.root / STATE_DIRNAME
    state_file = state_dir / STATE_FILENAME
    recover_interrupted_transaction(layout, state_dir, None)
    if state_dir.is_symlink():
        raise InstallerError(f"状态目录不能是符号链接：{state_dir}")
    if not state_file.is_file():
        raise InstallerError(
            f"未找到安装状态 {state_file}，拒绝猜测并删除游戏文件。"
        )
    state = _read_state(state_file)
    if state.get("state_schema") == 2:
        state = migrate_state_v2(layout, state_dir, None)
    backup_dwmapi, backup_xinput, backup_ue4ss = _validated_backup_paths(
        state, layout, state_dir
    )
    verify_installed_ownership(state, layout, backup_ue4ss)
    current_mods_path = layout.win64 / "ue4ss" / "Mods" / "mods.txt"
    try:
        current_mods_text = current_mods_path.read_text(encoding="utf-8-sig")
        original_mods_path = (
            backup_ue4ss / "Mods" / "mods.txt"
            if backup_ue4ss is not None
            else None
        )
        original_mods_text = (
            original_mods_path.read_text(encoding="utf-8-sig")
            if original_mods_path is not None and original_mods_path.is_file()
            else None
        )
    except (OSError, UnicodeError) as exc:
        raise InstallerError(f"无法读取 Mods/mods.txt 以保留其他模组：{exc}") from exc
    restored_mods_text = _mods_text_after_uninstall(
        current_mods_text,
        original_mods_text,
    )

    managed_dwmapi = (layout.win64 / "dwmapi.dll").resolve()
    managed_xinput = (layout.win64 / LEGACY_LOADER_FILENAME).resolve()
    managed_ue4ss = (layout.win64 / "ue4ss").resolve()
    if (
        managed_dwmapi.parent != layout.win64
        or managed_xinput.parent != layout.win64
        or managed_ue4ss.parent != layout.win64
        or managed_dwmapi.name.casefold() != "dwmapi.dll"
        or managed_xinput.name.casefold() != LEGACY_LOADER_FILENAME
        or managed_ue4ss.name.casefold() != "ue4ss"
        or not _is_within(managed_dwmapi, layout.win64)
        or not _is_within(managed_xinput, layout.win64)
        or not _is_within(managed_ue4ss, layout.win64)
    ):
        raise InstallerError("Win64 安全校验失败，拒绝卸载")

    try:
        snapshot = ChangeSnapshot(layout, state_dir, None)
    except Exception as exc:
        raise InstallerError(f"创建卸载事务快照失败，未进行变更：{exc}") from exc

    try:
        _remove_path(managed_dwmapi)
        _remove_path(managed_xinput)
        _remove_path(managed_ue4ss)
        if backup_dwmapi is not None:
            _copy_path(backup_dwmapi, managed_dwmapi)
        if backup_xinput is not None:
            _copy_path(backup_xinput, managed_xinput)
        if backup_ue4ss is not None:
            _copy_path(backup_ue4ss, managed_ue4ss)
        restored_mods_path = managed_ue4ss / "Mods" / "mods.txt"
        if restored_mods_text or original_mods_text is not None:
            restored_mods_path.parent.mkdir(parents=True, exist_ok=True)
            restored_mods_path.write_text(restored_mods_text, encoding="utf-8")
        _remove_path(state_dir)
        if state_dir.exists():
            raise InstallerError(f"无法删除安装状态目录：{state_dir}")
        if backup_dwmapi is not None:
            _fsync_path(managed_dwmapi)
        if backup_xinput is not None:
            _fsync_path(managed_xinput)
        if backup_ue4ss is not None:
            _fsync_path(managed_ue4ss)
        _fsync_directory(layout.root)
        _write_durable_marker(
            snapshot.transaction_dir / "COMMITTED",
            "uninstall committed\n",
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
                f"卸载失败：{exc}；事务回滚也失败：{rollback_error}"
            ) from exc
        raise InstallerError(f"卸载失败，已恢复变更前状态：{exc}") from exc

    try:
        snapshot.cleanup()
    except OSError as exc:
        _print(f"[警告] 卸载成功，但事务临时目录清理失败：{exc}")
    return (
        backup_dwmapi is not None,
        backup_xinput is not None,
        backup_ue4ss is not None,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="卸载《黑神话：悟空》UE4SS 相机同步插件"
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
        "--no-prompt",
        action="store_true",
        help="无人值守；找不到游戏时以 exit 3 无害跳过",
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
    _print("  黑神话：悟空 相机插件卸载")
    _print("============================================================")

    if os.name != "nt":
        _print("[错误] 黑神话相机插件卸载器只能在 Windows 上执行。")
        return 1

    result = resolve_wukong_dir(
        args.wukong_dir,
        prompt=not args.no_prompt,
        prefer_installed=True,
    )
    if result.layout is None:
        return 3 if result.skipped else 1
    layout = result.layout
    _print(f"游戏根目录：{layout.root}")
    _print(f"Win64 目录 ：{layout.win64}")

    try:
        if not args.skip_elevation and needs_elevation(layout):
            _print("游戏目录需要管理员权限，正在请求 UAC 提权 …")
            return elevate_and_wait(Path(__file__).resolve(), original_argv, layout)
        ensure_game_closed()
        restored_dwmapi, restored_xinput, restored_ue4ss = uninstall(layout)
    except Exception as exc:
        _print(f"[错误] {exc}")
        return 1

    _print()
    _print("[成功] 黑神话相机插件已卸载。")
    if restored_dwmapi or restored_xinput or restored_ue4ss:
        restored: list[str] = []
        if restored_dwmapi:
            restored.append("dwmapi.dll")
        if restored_xinput:
            restored.append(LEGACY_LOADER_FILENAME)
        if restored_ue4ss:
            restored.append("完整 ue4ss 目录")
        _print("  已恢复首次安装前备份：" + "、".join(restored))
    else:
        _print(
            f"  首次安装前没有 dwmapi.dll/{LEGACY_LOADER_FILENAME}/ue4ss，无需恢复。"
        )
    _print("============================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
