"""Align asynchronous per-sample camera Z-depth files to encoded video frames."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from game_recorder.camera_sync import (
    FRAME_TIMESTAMPS_FILENAME,
    FrameCaptureTime,
    frame_alignment_window_ms,
    load_frame_capture_times,
)

logger = logging.getLogger(__name__)

CP2077_DEPTH_RAW_FILENAME = "depth_raw_cp2077.jsonl"
DEPTH_FILENAME = "depth.jsonl"


@dataclass(frozen=True)
class DepthSample:
    t_unix_ms: int
    payload: dict[str, Any]


def _read_depth_log(path: Path) -> tuple[list[DepthSample], dict[str, Any], bool]:
    samples: list[DepthSample] = []
    header: dict[str, Any] = {}
    has_footer = False
    with path.open(encoding="utf-8-sig") as source:
        for line in source:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "header":
                header = obj
                continue
            if obj.get("type") == "footer":
                has_footer = True
                continue
            if obj.get("type") != "sample":
                continue
            try:
                timestamp = int(obj["t_unix_ms"])
                relative_file = str(obj["file"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not relative_file:
                continue
            samples.append(DepthSample(timestamp, obj))
    samples.sort(key=lambda sample: sample.t_unix_ms)
    return samples, header, has_footer


def _wait_for_depth_log(
    path: Path,
    *,
    wait_s: float,
) -> tuple[list[DepthSample], dict[str, Any]] | None:
    deadline = time.monotonic() + max(0.0, wait_s)
    latest: tuple[list[DepthSample], dict[str, Any]] | None = None
    while True:
        if path.is_file():
            try:
                samples, header, has_footer = _read_depth_log(path)
            except OSError:
                samples, header, has_footer = [], {}, False
            if samples or header:
                latest = samples, header
            if has_footer:
                return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(0.05)


def _nearest_depth(samples: list[DepthSample], timestamp_ms: float) -> DepthSample | None:
    if not samples:
        return None
    lo, hi = 0, len(samples) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid].t_unix_ms < timestamp_ms:
            lo = mid + 1
        else:
            hi = mid
    best = samples[lo]
    if lo > 0:
        previous = samples[lo - 1]
        if abs(previous.t_unix_ms - timestamp_ms) <= abs(
            best.t_unix_ms - timestamp_ms
        ):
            best = previous
    return best


def _fallback_frame_times(meta: dict[str, Any]) -> list[FrameCaptureTime]:
    fps = max(1, int(meta.get("fps") or 30))
    start_ms = float(meta["start_epoch_ms"])
    sync_offset = int(meta.get("event_video_sync_offset") or 0)
    total_frames = int(meta.get("total_frames") or 0)
    return [
        FrameCaptureTime(
            frame=frame,
            t_capture_unix_ms=start_ms + (frame + sync_offset) * 1000.0 / fps,
        )
        for frame in range(total_frames)
    ]


def _save_meta(session_dir: Path, meta: dict[str, Any]) -> None:
    with (session_dir / "meta.json").open("w", encoding="utf-8") as output:
        json.dump(meta, output, indent=2, ensure_ascii=False)
        output.write("\n")


def finalize_cp2077_depth(
    session_dir: Path,
    meta: dict[str, Any],
    *,
    wait_raw_s: float = 2.5,
    max_dt_ms: float | None = None,
) -> dict[str, Any] | None:
    """Align CP2077 `Zc` NPY samples to recorder frame timestamps."""
    session_dir = Path(session_dir)
    raw_path = session_dir / CP2077_DEPTH_RAW_FILENAME
    parsed = _wait_for_depth_log(raw_path, wait_s=wait_raw_s)
    if parsed is None:
        logger.info("会话无 CP2077 深度 raw JSONL，跳过深度对齐：%s", session_dir)
        return None
    samples, header = parsed

    valid_samples = [
        sample
        for sample in samples
        if (session_dir / str(sample.payload.get("file", ""))).is_file()
    ]
    fps = max(1, int(meta.get("fps") or 30))
    if max_dt_ms is None:
        max_dt_ms = frame_alignment_window_ms(fps)

    timestamp_name = str(
        meta.get("frame_timestamps_file") or FRAME_TIMESTAMPS_FILENAME
    )
    timestamp_path = session_dir / timestamp_name
    frame_times = (
        load_frame_capture_times(timestamp_path) if timestamp_path.is_file() else []
    )
    align_mode = "t_capture_unix_ms_from_frame_timestamps"
    if not frame_times:
        frame_times = _fallback_frame_times(meta)
        align_mode = "start_epoch_ms_plus_frame_index"

    records: list[dict[str, Any]] = []
    for frame_time in frame_times:
        nearest = _nearest_depth(valid_samples, frame_time.t_capture_unix_ms)
        if nearest is None:
            continue
        dt_ms = nearest.t_unix_ms - frame_time.t_capture_unix_ms
        if abs(dt_ms) > max_dt_ms:
            continue
        payload = {
            key: value
            for key, value in nearest.payload.items()
            if key != "type"
        }
        payload["frame"] = frame_time.frame
        payload["t_capture_unix_ms"] = round(frame_time.t_capture_unix_ms, 3)
        payload["dt_ms"] = round(dt_ms, 3)
        records.append(payload)

    output_path = session_dir / DEPTH_FILENAME
    if records:
        with output_path.open("w", encoding="utf-8") as output:
            for record in records:
                output.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                )
                output.write("\n")

    total_frames = int(meta.get("total_frames") or len(frame_times))
    summary: dict[str, Any] = {
        "status": "aligned" if records else "alignment_failed",
        "schema": header.get("schema", "cp2077_depth_v1"),
        "definition": header.get(
            "definition", "OpenCV camera-coordinate optical-axis value Zc"
        ),
        "units": header.get("units", "m"),
        "dtype": header.get("dtype", "<f4"),
        "array_layout": header.get("array_layout", "H_W"),
        "source": header.get("source", "reshade_depth_semantic_via_r32_float"),
        "file": DEPTH_FILENAME if records else None,
        "raw_file": CP2077_DEPTH_RAW_FILENAME,
        "sample_count_raw": len(samples),
        "sample_count_files": len(valid_samples),
        "frames_matched": len(records),
        "frames_missing": max(0, total_frames - len(records)),
        "unique_samples_used": len(
            {record.get("seq", record.get("t_unix_ms")) for record in records}
        ),
        "reused_frame_records": len(records)
        - len(
            {record.get("seq", record.get("t_unix_ms")) for record in records}
        ),
        "max_dt_ms": round(max_dt_ms, 3),
        "alignment_policy": "nearest_sample",
        "align": align_mode,
        "frame_timestamps_file": timestamp_name if timestamp_path.is_file() else None,
        "follow_recorder": True,
    }
    if "camera_axes" in header:
        summary["camera_axes"] = header["camera_axes"]
    if "calibration" in header:
        summary["calibration"] = header["calibration"]
    meta["depth"] = summary
    _save_meta(session_dir, meta)

    if records:
        logger.info(
            "CP2077 Z-depth 已对齐：%d/%d 帧 → %s",
            len(records),
            total_frames,
            output_path,
        )
    else:
        logger.error("CP2077 深度样本未落入 %.1fms 对齐窗口", max_dt_ms)
    return summary
