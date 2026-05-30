"""Trim auto-stop tail from the last segment's mp4 + jsonl after idle/stuck/violent auto-stop."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from game_recorder.storage.session_writer import SegmentMeta

logger = logging.getLogger(__name__)


def idle_tail_trim_frames(tail_duration_s: float, fps: int) -> int:
    """Frames to remove from the end after idle, stuck, or violent auto-stop."""
    if tail_duration_s <= 0:
        return 0
    return max(0, int(round(float(tail_duration_s) * max(1, fps))))


def apply_idle_tail_trim(
    session_dir: Path,
    segments: list[SegmentMeta],
    *,
    trim_frames: int,
    fps: int,
    session_timestamp: str,
    ffmpeg_path: str,
) -> tuple[list[SegmentMeta], int]:
    """Trim the tail of the last segment on disk.

    Returns ``(updated_segments, frames_removed)``.
    """
    if trim_frames <= 0 or not segments:
        return segments, 0

    segs = list(segments)
    last = segs[-1]
    # Keep at least one frame in the last segment when possible.
    remove = min(trim_frames, max(0, last.frame_count - 1))
    if remove <= 0:
        return segments, 0

    new_end = last.end_frame - remove
    new_count = last.frame_count - remove
    video_path = session_dir / last.video
    actions_path = session_dir / last.actions

    if not video_path.is_file():
        logger.warning("空闲裁剪跳过：视频不存在 %s", video_path.name)
        return segments, 0

    try:
        _trim_video_end(video_path, keep_frames=new_count, fps=fps, ffmpeg_path=ffmpeg_path)
    except Exception as exc:
        logger.warning("裁剪视频尾部失败 %s：%s", video_path.name, exc)
        return segments, 0

    event_count = 0
    if actions_path.is_file():
        try:
            event_count = _trim_jsonl(actions_path, max_frame_exclusive=new_end)
        except Exception as exc:
            logger.warning("裁剪操作日志失败 %s：%s", actions_path.name, exc)

    new_video_name = f"{session_timestamp}_{last.start_frame}_{new_end}.mp4"
    new_actions_name = f"{session_timestamp}_{last.start_frame}_{new_end}.jsonl"
    new_video_path = session_dir / new_video_name
    new_actions_path = session_dir / new_actions_name

    for src, dst in (
        (video_path, new_video_path),
        (actions_path, new_actions_path),
    ):
        if src == dst or not src.exists():
            continue
        for attempt in range(6):
            try:
                src.rename(dst)
                break
            except OSError as exc:
                if attempt < 5:
                    time.sleep(0.15)
                    continue
                logger.warning("重命名裁剪文件失败 %s → %s：%s", src.name, dst.name, exc)

    segs[-1] = SegmentMeta(
        index=last.index,
        start_frame=last.start_frame,
        end_frame=new_end,
        frame_count=new_count,
        event_count=event_count,
        video=new_video_name,
        actions=new_actions_name,
    )

    logger.info(
        "已裁剪自动停止尾部：从最后分段移除 %d 帧（[%d, %d) → %s）",
        remove,
        last.end_frame - remove,
        last.end_frame,
        new_video_name,
    )
    return segs, remove


def _trim_video_end(path: Path, *, keep_frames: int, fps: int, ffmpeg_path: str) -> None:
    keep_s = keep_frames / max(1, fps)
    tmp = path.with_name(f"{path.stem}.trim{path.suffix}")
    try:
        result = subprocess.run(
            [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(path),
                "-t",
                f"{keep_s:.6f}",
                "-c",
                "copy",
                str(tmp),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"ffmpeg exit {result.returncode}")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _trim_jsonl(path: Path, *, max_frame_exclusive: int) -> int:
    kept_lines: list[str] = []
    event_total = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            frame = int(rec["frame"])
            if frame >= max_frame_exclusive:
                continue
            event_total += len(rec.get("events") or [])
            kept_lines.append(line)

    tmp = path.with_name(f"{path.stem}.trim{path.suffix}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            if kept_lines:
                f.write("\n".join(kept_lines) + "\n")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return event_total
