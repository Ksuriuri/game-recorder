"""Persist auto-stop notice across process restarts (scheme A session handoff)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

PENDING_NOTICE_FILENAME = ".pending_auto_stop.json"

AutoStopReason = Literal[
    "idle", "stuck", "forbidden_key", "violent", "focus_lost", "frame_drop", "encoder_failed"
]


@dataclass(frozen=True)
class PendingAutoStopNotice:
    reason: AutoStopReason
    saved: bool
    discarded_short: bool = False


def pending_notice_path(output_dir: Path) -> Path:
    return output_dir / PENDING_NOTICE_FILENAME


def write_pending_notice(output_dir: Path, notice: PendingAutoStopNotice) -> None:
    """Write notice for the next process to show after restart."""
    path = pending_notice_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(notice), f, indent=2, ensure_ascii=False)
    tmp.replace(path)
    logger.info(
        "已写入自动停止提示（reason=%s saved=%s），下轮进程启动后显示",
        notice.reason,
        notice.saved,
    )


def consume_pending_notice(output_dir: Path) -> PendingAutoStopNotice | None:
    """Read and delete pending notice, if any."""
    path = pending_notice_path(output_dir)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        reason = raw.get("reason")
        if reason not in (
            "idle",
            "stuck",
            "forbidden_key",
            "violent",
            "focus_lost",
            "frame_drop",
            "encoder_failed",
        ):
            logger.warning("忽略无效的 pending notice：%r", reason)
            return None
        notice = PendingAutoStopNotice(
            reason=reason,
            saved=bool(raw.get("saved", False)),
            discarded_short=bool(raw.get("discarded_short", False)),
        )
        return notice
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("读取 pending notice 失败：%s", exc)
        return None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("删除 pending notice 失败：%s", exc)
