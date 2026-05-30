"""Ensure only one game-recorder instance is active (restart replaces the old one)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger(__name__)


def replace_existing_instance(*, skip_if_continuing: bool = True) -> None:
    """Terminate other game-recorder python processes before this one starts."""
    if sys.platform != "win32":
        return
    if skip_if_continuing and "--continuing" in sys.argv:
        return
    pid = os.getpid()
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') "
        "-and $_.CommandLine -match 'game_recorder' "
        f"-and $_.ProcessId -ne {pid} }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if result.returncode != 0 and result.stderr.strip():
            logger.debug("replace existing instance: %s", result.stderr.strip())
        time.sleep(0.4)
    except Exception as exc:
        logger.debug("replace existing instance failed: %s", exc)
