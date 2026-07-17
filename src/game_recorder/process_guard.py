"""Ensure only one game-recorder instance is active (restart replaces the old one)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

RECORDER_COMMAND_PATTERN = r"game[-_]recorder"


def _replacement_powershell(pid: int) -> str:
    """Build the process filter for editable and packaged entry-point names."""
    return (
        f"$current=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
        f"$excluded=@({int(pid)}); "
        "while ($null -ne $current -and $current.ParentProcessId -gt 0) { "
        "$parent=Get-CimInstance Win32_Process -Filter "
        "('ProcessId = ' + $current.ParentProcessId); "
        "if ($null -eq $parent) { break }; "
        "$excluded += $parent.ProcessId; $current=$parent }; "
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') "
        f"-and $_.CommandLine -match '{RECORDER_COMMAND_PATTERN}' "
        "-and ($excluded -notcontains $_.ProcessId) } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    )


def replace_existing_instance(*, skip_if_continuing: bool = True) -> None:
    """Terminate other game-recorder python processes before this one starts."""
    if sys.platform != "win32":
        return
    if skip_if_continuing and "--continuing" in sys.argv:
        return
    pid = os.getpid()
    ps = _replacement_powershell(pid)
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
