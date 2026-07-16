"""Shared game-camera session control and video-frame alignment."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

ACTIVE_SESSION_FILENAME = "active_session.json"
CAMERA_FILENAME = "camera.jsonl"
FRAME_TIMESTAMPS_FILENAME = "frame_timestamps.jsonl"


@dataclass(frozen=True)
class CameraSource:
    key: str
    control_dirname: str
    raw_filename: str
    source: str
    schema: str
    legacy_raw_filenames: tuple[str, ...] = ()
    requires_windows_qpc: bool = False


GTA_CAMERA_SOURCE = CameraSource(
    key="gta",
    control_dirname=".gta_camera",
    raw_filename="camera_raw_gta.jsonl",
    legacy_raw_filenames=("camera_raw.jsonl",),
    source="gta_scripthook_gameplay_cam",
    schema="gta_camera_v2",
)

WUKONG_CAMERA_SOURCE = CameraSource(
    key="wukong",
    control_dirname=".wukong_camera",
    raw_filename="camera_raw_wukong.jsonl",
    source="wukong_ue4ss_camera_cache",
    schema="wukong_camera_v2",
    requires_windows_qpc=True,
)


@dataclass
class CameraSample:
    t_unix_ms: int
    payload: dict[str, Any]


@dataclass
class FrameCaptureTime:
    """Per-encoded-frame capture time from ``frame_timestamps.jsonl``."""

    frame: int
    t_capture_unix_ms: float


def camera_control_dir(output_dir: Path, source: CameraSource) -> Path:
    """Return a source's control directory next to the recordings root."""
    return Path(output_dir).resolve().parent / source.control_dirname


def active_session_path(output_dir: Path, source: CameraSource) -> Path:
    return camera_control_dir(output_dir, source) / ACTIVE_SESSION_FILENAME


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def windows_qpc_anchor() -> tuple[float, float]:
    """Return ``(raw QPC seconds, Unix milliseconds at that QPC sample)``.

    Unreal's Windows ``GetPlatformTimeSeconds`` and QueryPerformanceCounter use
    the same hardware clock. The wall-clock calls bracket the QPC read so their
    midpoint minimizes the epoch-mapping error.
    """
    if os.name != "nt":
        raise RuntimeError("Windows QPC is only available on Windows")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    frequency = ctypes.c_longlong()
    counter = ctypes.c_longlong()
    if not kernel32.QueryPerformanceFrequency(ctypes.byref(frequency)):
        raise ctypes.WinError(ctypes.get_last_error())

    wall_before_ns = time.time_ns()
    if not kernel32.QueryPerformanceCounter(ctypes.byref(counter)):
        raise ctypes.WinError(ctypes.get_last_error())
    wall_after_ns = time.time_ns()
    if frequency.value <= 0:
        raise RuntimeError("QueryPerformanceFrequency returned a non-positive value")

    qpc_seconds = counter.value / frequency.value
    unix_ms = (wall_before_ns + wall_after_ns) / 2_000_000.0
    return qpc_seconds, unix_ms


def publish_active_session(
    output_dir: Path,
    source: CameraSource,
    *,
    session_id: str,
    session_dir: Path,
    start_epoch_ms: int,
    fps: int,
    sample_hz: float | None = None,
) -> Path:
    """Atomically publish a recording session for one in-game camera plugin."""
    payload: dict[str, Any] = {
        "status": "recording",
        "session_id": session_id,
        "session_dir": Path(session_dir).resolve().as_posix(),
        "start_epoch_ms": int(start_epoch_ms),
        "fps": int(fps),
        "sample_hz": float(sample_hz if sample_hz is not None else fps),
        "raw_file": source.raw_filename,
        "updated_at_ms": int(time.time() * 1000),
    }
    if source.requires_windows_qpc:
        qpc_seconds, qpc_unix_ms = windows_qpc_anchor()
        payload["qpc_anchor_seconds"] = qpc_seconds
        payload["qpc_anchor_unix_ms"] = qpc_unix_ms

    path = active_session_path(output_dir, source)
    _write_json_atomic(path, payload)
    logger.info("已发布 %s 相机同步信号 → %s", source.key, path)
    return path


def clear_active_session(output_dir: Path, source: CameraSource) -> None:
    """Tell one in-game plugin to stop writing."""
    path = active_session_path(output_dir, source)
    _write_json_atomic(
        path,
        {
            "status": "idle",
            "updated_at_ms": int(time.time() * 1000),
        },
    )
    logger.info("已清除 %s 相机同步信号 → %s", source.key, path)


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
            try:
                timestamp = int(t)
            except (TypeError, ValueError, OverflowError):
                continue
            samples.append(CameraSample(t_unix_ms=timestamp, payload=obj))
    samples.sort(key=lambda sample: sample.t_unix_ms)
    return samples, header


def _nearest_sample(
    samples: list[CameraSample],
    t_ms: float,
) -> CameraSample | None:
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
        previous = samples[lo - 1]
        if abs(previous.t_unix_ms - t_ms) <= abs(best.t_unix_ms - t_ms):
            best = previous
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
                frame = int(obj["frame"])
                capture_ms = float(obj["t_capture_unix_ms"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError):
                continue
            frames.append(
                FrameCaptureTime(
                    frame=frame,
                    t_capture_unix_ms=capture_ms,
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
    """Return per-video-frame camera records, matched count and missing count."""
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
        payload = {key: value for key, value in nearest.payload.items() if key != "type"}
        payload["frame"] = frame
        payload["t_capture_unix_ms"] = round(t_ms, 3)
        payload["dt_ms"] = round(nearest.t_unix_ms - t_ms, 3)
        out.append(payload)
        matched += 1
    return out, matched, missing


def _raw_path_for_source(
    session_dir: Path,
    source: CameraSource,
) -> Path | None:
    for filename in (source.raw_filename, *source.legacy_raw_filenames):
        candidate = session_dir / filename
        if candidate.is_file():
            return candidate
    return None


def _save_meta(session_dir: Path, meta: dict[str, Any]) -> None:
    with open(session_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _geometry_contract(header: dict[str, Any]) -> dict[str, Any]:
    """Return self-describing camera coordinate metadata from a raw header."""
    keys = (
        "world_units",
        "camera_to_world_translation_units",
        "matrix_layout",
        "matrix_vector_convention",
        "world_axes",
        "camera_axes",
        "camera_to_world_source",
        "world_to_clip_source",
        "world_to_clip_input_units",
        "sample_policy",
        "fov_axis",
        "projection_source",
    )
    return {key: header[key] for key in keys if key in header}


def finalize_session_cameras(
    session_dir: Path,
    meta: dict[str, Any],
    sources: Iterable[CameraSource],
    *,
    max_dt_ms: float = 50.0,
    wait_raw_s: float = 0.5,
    keep_raw: bool = False,
) -> dict[str, Any] | None:
    """Select one camera source and align it to encoded video frames.

    Multiple source logs are never mixed. A conflict is persisted to meta and
    all raw logs are retained for diagnosis.
    """
    session_dir = Path(session_dir)
    source_list = tuple(sources)
    deadline = time.monotonic() + max(0.0, wait_raw_s)
    found: list[tuple[CameraSource, Path]] = []
    while True:
        found = [
            (source, path)
            for source in source_list
            if (path := _raw_path_for_source(session_dir, source)) is not None
        ]
        if found or time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    if not found:
        logger.info("会话无游戏相机 raw JSONL，跳过相机对齐：%s", session_dir)
        return None

    time.sleep(min(0.3, max(0.0, wait_raw_s)))
    # Re-scan after plugins have had time to observe idle. A second source may
    # have created its file during the flush grace period.
    found = [
        (source, path)
        for source in source_list
        if (path := _raw_path_for_source(session_dir, source)) is not None
    ]
    parsed = [
        (source, path, *iter_camera_samples(path))
        for source, path in found
    ]
    valid = [
        (source, path, samples, header)
        for source, path, samples, header in parsed
        if samples
    ]
    if not valid:
        logger.warning(
            "相机 raw 文件均无有效样本：%s",
            ", ".join(path.name for _, path, _, _ in parsed),
        )
        return None

    start_ms = int(meta["start_epoch_ms"])
    fps = int(meta.get("fps") or 30)
    sync_offset = int(meta.get("event_video_sync_offset") or 0)
    total_frames = int(meta.get("total_frames") or 0)
    if total_frames <= 0:
        duration_s = float(meta.get("duration_s") or 0.0)
        total_frames = max(0, int(round(duration_s * fps)))

    timestamp_name = meta.get("frame_timestamps_file") or FRAME_TIMESTAMPS_FILENAME
    timestamp_path = session_dir / timestamp_name
    frame_times: list[FrameCaptureTime] | None = None
    align_mode = "unix_ms_vs_start_epoch_ms_plus_sync_offset"
    if timestamp_path.is_file():
        frame_times = load_frame_capture_times(timestamp_path)
        if frame_times:
            align_mode = "t_capture_unix_ms_from_frame_timestamps"
            if total_frames <= 0:
                total_frames = max(item.frame for item in frame_times) + 1

    candidate_results = []
    for source, raw_path, samples, header in valid:
        records, matched, missing = align_samples_to_frames(
            samples,
            start_epoch_ms=start_ms,
            fps=fps,
            total_frames=total_frames,
            sync_offset=sync_offset,
            max_dt_ms=max_dt_ms,
            frame_times=frame_times,
        )
        candidate_results.append(
            (source, raw_path, samples, header, records, matched, missing)
        )

    matching = [candidate for candidate in candidate_results if candidate[5] > 0]
    if len(matching) > 1:
        summary: dict[str, Any] = {
            "status": "conflict",
            "sources": [source.source for source, _, _, _, _, _, _ in matching],
            "schemas": [
                header.get("schema") or source.schema
                for source, _, _, header, _, _, _ in matching
            ],
            "raw_files": [path.name for _, path, _, _, _, _, _ in matching],
            "file": None,
            "follow_recorder": True,
        }
        meta["camera"] = summary
        _save_meta(session_dir, meta)
        logger.error(
            "检测到多个可对齐相机来源，已拒绝混合并保留 raw 文件：%s",
            ", ".join(path.name for _, path, _, _, _, _, _ in matching),
        )
        return summary

    if not matching:
        if len(candidate_results) == 1:
            source, raw_path, samples, header, _, _, missing = candidate_results[0]
            summary = {
                "status": "alignment_failed",
                "source": source.source,
                "schema": header.get("schema") or source.schema,
                "file": None,
                "raw_file": raw_path.name,
                "sample_count_raw": len(samples),
                "frames_matched": 0,
                "frames_missing": missing,
                "max_dt_ms": max_dt_ms,
                "align": align_mode,
                "follow_recorder": True,
            }
        else:
            summary = {
                "status": "alignment_failed",
                "sources": [
                    source.source
                    for source, _, _, _, _, _, _ in candidate_results
                ],
                "file": None,
                "raw_files": [
                    path.name
                    for _, path, _, _, _, _, _ in candidate_results
                ],
                "frames_matched": 0,
                "max_dt_ms": max_dt_ms,
                "align": align_mode,
                "follow_recorder": True,
            }
        if frame_times:
            summary["frame_timestamps_file"] = timestamp_path.name
        meta["camera"] = summary
        _save_meta(session_dir, meta)
        logger.error(
            "相机样本均未落入 %.1fms 对齐窗口，已保留 raw 文件",
            max_dt_ms,
        )
        return summary

    source, raw_path, samples, header, records, matched, missing = matching[0]
    ignored_raw_files = [
        path.name
        for _, path, _, _, _, candidate_matched, _ in candidate_results
        if candidate_matched == 0
    ]

    output_path = session_dir / CAMERA_FILENAME
    with open(output_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")

    summary = {
        "status": "aligned",
        "source": source.source,
        "schema": header.get("schema") or source.schema,
        "file": CAMERA_FILENAME,
        "raw_file": raw_path.name,
        "sample_count_raw": len(samples),
        "frames_matched": matched,
        "frames_missing": missing,
        "max_dt_ms": max_dt_ms,
        "align": align_mode,
        "follow_recorder": True,
    }
    geometry = _geometry_contract(header)
    if geometry:
        summary["geometry"] = geometry
    if frame_times:
        summary["frame_timestamps_file"] = timestamp_path.name
    if ignored_raw_files:
        summary["ignored_raw_files"] = ignored_raw_files
    meta["camera"] = summary
    _save_meta(session_dir, meta)

    if not keep_raw:
        try:
            raw_path.unlink()
        except OSError:
            pass

    logger.info(
        "%s 相机已对齐：%d/%d 帧 → %s",
        source.key,
        matched,
        total_frames,
        output_path,
    )
    return summary
