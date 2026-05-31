"""Cold-restart the recorder process after each session (scheme A)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from game_recorder.capture.window_region import (
    is_game_window_foreground,
    restore_window_focus,
)

logger = logging.getLogger(__name__)

CONTINUING_ARG = "--continuing"
PENDING_FOCUS_FILENAME = ".pending_game_focus.json"


def relaunch_argv(argv: list[str] | None = None) -> list[str]:
    """Build argv for the replacement process (adds --continuing once)."""
    base = list(argv if argv is not None else sys.argv)
    if CONTINUING_ARG not in base:
        base.append(CONTINUING_ARG)
    return base


def _project_launch_env(cwd: Path) -> dict[str, str]:
    """Match run.bat / launch_background.vbs PATH and PYTHONPATH."""
    env = os.environ.copy()
    ffmpeg_bin = cwd / "ffmpeg" / "bin"
    venv_scripts = cwd / ".venv" / "Scripts"
    src_dir = cwd / "src"
    path_parts: list[str] = []
    if ffmpeg_bin.is_dir():
        path_parts.append(str(ffmpeg_bin))
    if venv_scripts.is_dir():
        path_parts.append(str(venv_scripts))
    if path_parts:
        env["PATH"] = ";".join(path_parts + [env.get("PATH", "")])
    if src_dir.is_dir():
        prefix = str(src_dir)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = prefix if not existing else f"{prefix}{os.pathsep}{existing}"
    return env


def _resolve_entrypoint(cmd: list[str], cwd: Path) -> list[str]:
    """Prefer .venv/Scripts/game-recorder.exe (same as launch_background.vbs)."""
    entry = cwd / ".venv" / "Scripts" / "game-recorder.exe"
    if not entry.is_file():
        return cmd
    return [str(entry), *cmd[1:]]


@dataclass(frozen=True)
class PendingGameFocus:
    hwnd: int | None
    title: str


def pending_focus_path(output_dir: Path) -> Path:
    return output_dir / PENDING_FOCUS_FILENAME


def write_pending_focus(
    output_dir: Path,
    *,
    hwnd: int | None,
    title: str,
) -> None:
    """Remember which window to refocus after the replacement process starts."""
    if not hwnd and not title:
        return
    path = pending_focus_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {"hwnd": hwnd, "title": title}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(path)
    logger.info("已保存游戏窗口焦点（hwnd=%s title=%r），重启后将自动切回", hwnd, title)


def consume_pending_focus(output_dir: Path) -> PendingGameFocus | None:
    """Read and delete pending focus target, if any."""
    path = pending_focus_path(output_dir)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        hwnd_raw = raw.get("hwnd")
        hwnd = int(hwnd_raw) if hwnd_raw is not None else None
        title = str(raw.get("title") or "")
        if not hwnd and not title:
            return None
        return PendingGameFocus(hwnd=hwnd, title=title)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("读取 pending game focus 失败：%s", exc)
        return None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("删除 pending game focus 失败：%s", exc)


_FOCUS_POLL_S = 0.1
_FOCUS_STABLE_POLLS = 5  # game foreground this many polls in a row → no switch needed
_FOCUS_WATCH_TIMEOUT_S = 5.0


def schedule_restore_game_focus(
    output_dir: Path,
    *,
    ui_settled: threading.Event | None = None,
) -> None:
    """Switch back to the game once, only if focus actually left it after restart."""
    focus = consume_pending_focus(output_dir)
    if focus is None:
        return

    def _worker() -> None:
        if ui_settled is not None:
            ui_settled.wait(timeout=5.0)

        deadline = time.monotonic() + _FOCUS_WATCH_TIMEOUT_S
        stable_on_game = 0

        while time.monotonic() < deadline:
            time.sleep(_FOCUS_POLL_S)
            if is_game_window_foreground(hwnd=focus.hwnd, title=focus.title):
                stable_on_game += 1
                if stable_on_game >= _FOCUS_STABLE_POLLS:
                    logger.debug("游戏窗口已在前台，无需切回")
                    return
                continue

            stable_on_game = 0
            restore_window_focus(hwnd=focus.hwnd, title=focus.title)
            return

        logger.debug("游戏焦点监控超时，未检测到失焦")

    threading.Thread(target=_worker, name="restore-game-focus", daemon=True).start()


def relaunch_process(argv: list[str] | None = None) -> None:
    """Spawn a fresh game-recorder process and return (caller should exit immediately)."""
    cwd = Path(os.getcwd())
    cmd = _resolve_entrypoint(relaunch_argv(argv), cwd)
    env = _project_launch_env(cwd)
    creationflags = 0
    if sys.platform == "win32":
        # Background / GUI-only parent: do not flash a console on relaunch.
        no_console = os.path.basename(sys.executable).lower() == "pythonw.exe"
        try:
            no_console = no_console or not sys.stdin.isatty()
        except (AttributeError, OSError):
            no_console = True
        if no_console:
            creationflags = 0x08000000  # CREATE_NO_WINDOW
    logger.info("正在冷重启录制进程：%s", " ".join(cmd))
    try:
        subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            creationflags=creationflags,
        )
    except OSError as exc:
        logger.error("冷重启失败：%s", exc)
        raise
