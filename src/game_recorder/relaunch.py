"""Cold-restart the recorder process after each session (scheme A)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

CONTINUING_ARG = "--continuing"


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
