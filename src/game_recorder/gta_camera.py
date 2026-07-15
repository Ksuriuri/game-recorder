"""Backward-compatible GTA wrappers around the shared camera-sync pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from game_recorder.camera_sync import (
    ACTIVE_SESSION_FILENAME,
    CAMERA_FILENAME,
    FRAME_TIMESTAMPS_FILENAME,
    GTA_CAMERA_SOURCE,
    CameraSample,
    FrameCaptureTime,
    align_samples_to_frames,
    camera_control_dir,
    clear_active_session as _clear_active_session,
    finalize_session_cameras,
    iter_camera_samples,
    load_frame_capture_times,
    publish_active_session as _publish_active_session,
)

GTA_CAMERA_DIRNAME = GTA_CAMERA_SOURCE.control_dirname
CAMERA_RAW_FILENAME = GTA_CAMERA_SOURCE.raw_filename


def gta_camera_dir(output_dir: Path) -> Path:
    return camera_control_dir(output_dir, GTA_CAMERA_SOURCE)


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
    return _publish_active_session(
        output_dir,
        GTA_CAMERA_SOURCE,
        session_id=session_id,
        session_dir=session_dir,
        start_epoch_ms=start_epoch_ms,
        fps=fps,
        sample_hz=sample_hz,
    )


def clear_active_session(output_dir: Path) -> None:
    _clear_active_session(output_dir, GTA_CAMERA_SOURCE)


def finalize_session_camera(
    session_dir: Path,
    meta: dict[str, Any],
    *,
    max_dt_ms: float = 50.0,
    wait_raw_s: float = 0.5,
    keep_raw: bool = False,
) -> dict[str, Any] | None:
    return finalize_session_cameras(
        session_dir,
        meta,
        (GTA_CAMERA_SOURCE,),
        max_dt_ms=max_dt_ms,
        wait_raw_s=wait_raw_s,
        keep_raw=keep_raw,
    )
