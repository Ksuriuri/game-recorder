"""Trigger detached OSS upload after a recording session is kept.

Runs ``s3-upload/upload_recordings.py`` in a background process so cold relaunch
is not blocked. Eligibility: video duration > threshold and ``camera.jsonl`` present.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from game_recorder.camera_sync import CAMERA_FILENAME

logger = logging.getLogger(__name__)

AUTO_UPLOADED_LOG = "auto_uploaded.jsonl"
DEFAULT_MIN_DURATION_S = 60.0


def auto_uploaded_log_path(output_dir: Path) -> Path:
    return Path(output_dir) / AUTO_UPLOADED_LOG


def append_auto_upload_log(
    output_dir: Path,
    *,
    session_dir: Path | None,
    status: str,
    detail: str,
) -> None:
    """Append one JSON line to recordings/auto_uploaded.jsonl."""
    log_path = auto_uploaded_log_path(output_dir)
    payload = {
        "session": session_dir.name if session_dir is not None else "",
        "path": str(session_dir.resolve()) if session_dir is not None else "",
        "status": status,
        "detail": detail,
        "uploaded_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("写入自动上传日志失败：%s", exc)


def _project_root_candidates(output_dir: Path) -> list[Path]:
    roots: list[Path] = []
    out = Path(output_dir).resolve()
    roots.append(out.parent)
    # src/game_recorder/storage/auto_upload.py → repo root
    try:
        roots.append(Path(__file__).resolve().parents[3])
    except IndexError:
        pass
    deduped: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        deduped.append(root)
    return deduped


def find_s3_upload_pack(output_dir: Path) -> Path | None:
    for root in _project_root_candidates(output_dir):
        pack = root / "s3-upload"
        if (pack / "upload_recordings.py").is_file():
            return pack
    return None


def session_upload_eligibility(
    session_dir: Path,
    *,
    min_duration_s: float = DEFAULT_MIN_DURATION_S,
) -> tuple[bool, str, float]:
    """Return (ok, reason, duration_s)."""
    session_dir = Path(session_dir)
    meta_path = session_dir / "meta.json"
    if not meta_path.is_file():
        return False, "缺少 meta.json", 0.0

    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"无法读取 meta.json：{exc}", 0.0

    duration_s = float(raw.get("duration_s") or 0.0)
    if min_duration_s > 0 and duration_s <= min_duration_s:
        return (
            False,
            f"视频时长 {duration_s:.1f}s ≤ {min_duration_s:g}s",
            duration_s,
        )

    camera_path = session_dir / CAMERA_FILENAME
    if not camera_path.is_file():
        return False, f"缺少 {CAMERA_FILENAME}", duration_s

    return True, f"时长 {duration_s:.1f}s 且含 {CAMERA_FILENAME}", duration_s


def _ensure_s3_upload_python(pack: Path) -> Path | None:
    python_exe = pack / ".venv" / "Scripts" / "python.exe"
    if python_exe.is_file():
        return python_exe

    install_bat = pack / "install.bat"
    if not install_bat.is_file():
        return None

    logger.info("正在准备 s3-upload 上传环境 …")
    env = os.environ.copy()
    env["S3_UPLOAD_QUIET"] = "1"
    env["S3_UPLOAD_SKIP_PAUSE"] = "1"
    try:
        completed = subprocess.run(
            ["cmd.exe", "/c", str(install_bat)],
            cwd=str(pack),
            env=env,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("安装 s3-upload 环境失败：%s", exc)
        return None

    if completed.returncode != 0 or not python_exe.is_file():
        logger.warning("s3-upload 环境未就绪（install.bat 退出码 %s）", completed.returncode)
        return None
    return python_exe


def _spawn_detached(command: list[str], *, cwd: Path, log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    stdout = open(log_file, "a", encoding="utf-8", errors="replace")
    try:
        stdout.write(f"\n--- auto-upload {' '.join(command)} ---\n")
        stdout.flush()
        kwargs: dict = {
            "args": command,
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": stdout,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform == "win32":
            # Survive parent cold-relaunch / os._exit.
            # close_fds must stay False on Windows when redirecting std handles.
            kwargs["close_fds"] = False
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        else:
            kwargs["close_fds"] = True
            kwargs["start_new_session"] = True
        subprocess.Popen(**kwargs)
    finally:
        # Child keeps the duplicated handle; close our copy.
        stdout.close()


def trigger_auto_upload(
    session_dir: Path,
    *,
    output_dir: Path,
    enabled: bool = True,
    min_duration_s: float = DEFAULT_MIN_DURATION_S,
) -> bool:
    """If eligible, spawn a detached OSS upload. Returns True when a job was started."""
    if not enabled:
        return False

    session_dir = Path(session_dir).resolve()
    output_dir = Path(output_dir)

    def _error(message: str) -> bool:
        logger.warning("%s", message)
        print(f">>> {message}")
        append_auto_upload_log(
            output_dir,
            session_dir=session_dir,
            status="error",
            detail=message,
        )
        return False

    ok, reason, _duration = session_upload_eligibility(
        session_dir, min_duration_s=min_duration_s
    )
    if not ok:
        logger.info("跳过自动上传 %s：%s", session_dir.name, reason)
        print(f">>> 跳过自动上传：{reason}")
        return False

    pack = find_s3_upload_pack(output_dir)
    if pack is None:
        return _error("自动上传失败：未找到 s3-upload 目录")

    cred = pack / "oss_credentials.json"
    if not cred.is_file():
        return _error(f"自动上传失败：缺少 {cred.name}")

    python_exe = _ensure_s3_upload_python(pack)
    if python_exe is None:
        return _error(
            "自动上传失败：s3-upload 环境未安装（请先运行 s3-upload\\install.bat）"
        )

    script = pack / "upload_recordings.py"
    success_log = auto_uploaded_log_path(output_dir)
    run_log = output_dir / ".auto_upload_run.log"
    command = [
        str(python_exe),
        str(script),
        "--session",
        str(session_dir),
        "--success-log",
        str(success_log.resolve()),
    ]
    try:
        _spawn_detached(command, cwd=pack, log_file=run_log)
    except OSError as exc:
        return _error(f"自动上传失败：无法启动上传进程：{exc}")

    logger.info(
        "已触发自动上传 %s（%s）→ 结果写入 %s",
        session_dir.name,
        reason,
        success_log.name,
    )
    print(
        f">>> 已触发自动上传：{session_dir.name}（{reason}）\n"
        f"    结果写入 {success_log}（成功或失败均会记录）"
    )
    return True
