"""GTA camera pose sync: control file + raw→aligned camera.jsonl.

game-recorder writes ``.gta_camera/active_session.json`` (sibling of
``recordings/``, not inside it) when a session starts. The in-game plugin
follows it and streams ``session_*/camera_raw.jsonl``. On stop we clear the
control file, wait briefly, then convert raw samples into per-frame
``camera.jsonl`` and delete the raw file.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Sibling of recordings/ (e.g. project_root/.gta_camera), never inside recordings/.
GTA_CAMERA_DIRNAME = ".gta_camera"
ACTIVE_SESSION_FILENAME = "active_session.json"
CAMERA_RAW_FILENAME = "camera_raw.jsonl"
CAMERA_FILENAME = "camera.jsonl"
FRAME_TIMESTAMPS_FILENAME = "frame_timestamps.jsonl"


@dataclass
class CameraSample:
    t_unix_ms: int
    payload: dict[str, Any]


@dataclass
class FrameCaptureTime:
    """Per-encoded-frame capture time from ``frame_timestamps.jsonl``."""

    frame: int
    t_capture_unix_ms: float


def gta_camera_dir(output_dir: Path) -> Path:
    """Return the GTA sync control directory (sibling of the recordings root)."""
    return Path(output_dir).resolve().parent / GTA_CAMERA_DIRNAME


def active_session_path(output_dir: Path) -> Path:
    return gta_camera_dir(output_dir) / ACTIVE_SESSION_FILENAME


def publish_active_session(
    output_dir: Path,
    *,
    session_id: str,
    session_dir: Path,
    start_epoch_ms: int,
    fps: int,
    sample_hz: float | None = None,
) -> Path:
    """Atomically publish the live-session signal for the GTA plugin."""
    cam_dir = gta_camera_dir(output_dir)
    cam_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "recording",
        "session_id": session_id,
        "session_dir": str(Path(session_dir).resolve()),
        "start_epoch_ms": int(start_epoch_ms),
        "fps": int(fps),
        "sample_hz": float(sample_hz if sample_hz is not None else fps),
        "raw_file": CAMERA_RAW_FILENAME,
        "updated_at_ms": int(time.time() * 1000),
    }
    path = active_session_path(output_dir)
    tmp = path.with_suffix(".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)
    logger.info("已发布 GTA 相机同步信号 → %s", path)
    return path


def clear_active_session(output_dir: Path) -> None:
    """Tell the GTA plugin to stop writing (idle)."""
    path = active_session_path(output_dir)
    cam_dir = gta_camera_dir(output_dir)
    cam_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "idle",
        "updated_at_ms": int(time.time() * 1000),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    logger.info("已清除 GTA 相机同步信号 → %s", path)


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def iter_camera_samples(path: Path) -> tuple[list[CameraSample], dict[str, Any]]:
    samples: list[CameraSample] = []
    header: dict[str, Any] = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = obj.get("type")
            if typ == "header":
                header = obj
                continue
            if typ == "footer":
                continue
            if typ is not None and typ != "sample":
                continue
            t = obj.get("t_unix_ms")
            if t is None:
                continue
            samples.append(CameraSample(t_unix_ms=int(t), payload=obj))
    samples.sort(key=lambda s: s.t_unix_ms)
    return samples, header


def _nearest_sample(samples: list[CameraSample], t_ms: float) -> CameraSample | None:
    if not samples:
        return None
    target = int(round(t_ms))
    lo, hi = 0, len(samples) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid].t_unix_ms < target:
            lo = mid + 1
        else:
            hi = mid
    best = samples[lo]
    if lo > 0:
        prev = samples[lo - 1]
        if abs(prev.t_unix_ms - t_ms) <= abs(best.t_unix_ms - t_ms):
            best = prev
    return best


def load_frame_capture_times(path: Path) -> list[FrameCaptureTime]:
    """Load per-frame capture times from ``frame_timestamps.jsonl``."""
    frames: list[FrameCaptureTime] = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "frame" not in obj or "t_capture_unix_ms" not in obj:
                continue
            frames.append(
                FrameCaptureTime(
                    frame=int(obj["frame"]),
                    t_capture_unix_ms=float(obj["t_capture_unix_ms"]),
                )
            )
    frames.sort(key=lambda item: item.frame)
    return frames


def _frame_times_from_sync(
    *,
    start_epoch_ms: int,
    fps: int,
    total_frames: int,
    sync_offset: int = 0,
) -> list[FrameCaptureTime]:
    """Fallback when ``frame_timestamps.jsonl`` is missing.

    ``event_video_sync_offset`` is median(wall_frame − video_idx). Video frame
    ``i`` was captured near wall time ``(i + sync_offset) / fps``.
    """
    fps = max(1, int(fps))
    return [
        FrameCaptureTime(
            frame=frame,
            t_capture_unix_ms=start_epoch_ms
            + (frame + sync_offset) * 1000.0 / fps,
        )
        for frame in range(total_frames)
    ]


def align_samples_to_frames(
    samples: list[CameraSample],
    *,
    start_epoch_ms: int,
    fps: int,
    total_frames: int,
    sync_offset: int = 0,
    max_dt_ms: float = 50.0,
    frame_times: list[FrameCaptureTime] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Return (frame records, matched, missing).

    Prefer ``frame_times`` from ``frame_timestamps.jsonl`` (exact capture unix
    ms per encoded MP4 frame). Fall back to ``start_epoch_ms`` +
    ``(frame + sync_offset) / fps``.
    """
    if frame_times is None:
        frame_times = _frame_times_from_sync(
            start_epoch_ms=start_epoch_ms,
            fps=fps,
            total_frames=total_frames,
            sync_offset=sync_offset,
        )
    by_frame = {item.frame: item for item in frame_times}
    out: list[dict[str, Any]] = []
    matched = 0
    missing = 0
    for frame in range(total_frames):
        item = by_frame.get(frame)
        if item is None:
            missing += 1
            continue
        t_ms = item.t_capture_unix_ms
        nearest = _nearest_sample(samples, t_ms)
        if nearest is None or abs(nearest.t_unix_ms - t_ms) > max_dt_ms:
            missing += 1
            continue
        payload = {k: v for k, v in nearest.payload.items() if k != "type"}
        payload["frame"] = frame
        payload["t_capture_unix_ms"] = round(t_ms, 3)
        payload["dt_ms"] = round(nearest.t_unix_ms - t_ms, 3)
        out.append(payload)
        matched += 1
    return out, matched, missing


def finalize_session_camera(
    session_dir: Path,
    meta: dict[str, Any],
    *,
    max_dt_ms: float = 50.0,
    wait_raw_s: float = 0.5,
    keep_raw: bool = False,
) -> dict[str, Any] | None:
    """Convert ``camera_raw.jsonl`` → ``camera.jsonl`` and patch ``meta`` dict.

    By default deletes ``camera_raw.jsonl`` after a successful align.
    Returns camera summary for meta, or None if no raw log was found.
    """
    session_dir = Path(session_dir)
    raw_path = session_dir / CAMERA_RAW_FILENAME
    deadline = time.monotonic() + max(0.0, wait_raw_s)
    while not raw_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)

    if not raw_path.is_file():
        logger.info("会话无 GTA camera_raw.jsonl，跳过相机对齐：%s", session_dir)
        return None

    # Allow the game plugin a moment to flush/close after idle signal.
    time.sleep(min(0.3, max(0.0, wait_raw_s)))

    samples, header = iter_camera_samples(raw_path)
    if not samples:
        logger.warning("camera_raw.jsonl 无样本：%s", raw_path)
        return None

    start_ms = int(meta["start_epoch_ms"])
    fps = int(meta.get("fps") or 30)
    sync_offset = int(meta.get("event_video_sync_offset") or 0)
    total_frames = int(meta.get("total_frames") or 0)
    if total_frames <= 0:
        duration_s = float(meta.get("duration_s") or 0.0)
        total_frames = max(0, int(round(duration_s * fps)))

    ts_name = meta.get("frame_timestamps_file") or FRAME_TIMESTAMPS_FILENAME
    ts_path = session_dir / ts_name
    frame_times: list[FrameCaptureTime] | None = None
    align_mode = "unix_ms_vs_start_epoch_ms_plus_sync_offset"
    if ts_path.is_file():
        frame_times = load_frame_capture_times(ts_path)
        if frame_times:
            align_mode = "t_capture_unix_ms_from_frame_timestamps"
            if total_frames <= 0:
                total_frames = max(item.frame for item in frame_times) + 1

    records, matched, missing = align_samples_to_frames(
        samples,
        start_epoch_ms=start_ms,
        fps=fps,
        total_frames=total_frames,
        sync_offset=sync_offset,
        max_dt_ms=max_dt_ms,
        frame_times=frame_times,
    )

    out_path = session_dir / CAMERA_FILENAME
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")

    summary = {
        "source": "gta_scripthook_gameplay_cam",
        "schema": header.get("schema") or "gta_camera_v1",
        "file": CAMERA_FILENAME,
        "raw_file": CAMERA_RAW_FILENAME,
        "sample_count_raw": len(samples),
        "frames_matched": matched,
        "frames_missing": missing,
        "max_dt_ms": max_dt_ms,
        "align": align_mode,
        "follow_recorder": True,
    }
    if frame_times:
        summary["frame_timestamps_file"] = ts_path.name
    meta["camera"] = summary

    meta_path = session_dir / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
        f.write("\n")

    if not keep_raw:
        try:
            raw_path.unlink()
        except OSError:
            pass

    logger.info(
        "GTA 相机已对齐：%d/%d 帧 → %s",
        matched,
        total_frames,
        out_path,
    )
    return summary
