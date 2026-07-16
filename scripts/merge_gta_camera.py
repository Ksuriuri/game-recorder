#!/usr/bin/env python3
"""CLI for merging GTA camera logs into sessions.

Prefer the automatic path: with the ScriptHook plugin installed, game-recorder
publishes active_session.json and finalizes camera.jsonl on stop.

This script remains useful for re-aligning older raw logs::

    uv run python scripts/merge_gta_camera.py recordings/session_...
    uv run python scripts/merge_gta_camera.py recordings --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running without editable install
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from game_recorder.gta_camera import (  # noqa: E402
    CAMERA_RAW_FILENAME,
    FRAME_TIMESTAMPS_FILENAME,
    GTA_CAMERA_SOURCE,
    align_samples_to_frames,
    finalize_session_camera,
    gta_camera_dir,
    iter_camera_samples,
    load_frame_capture_times,
)
import json


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _discover_sessions(root: Path) -> list[Path]:
    root = Path(root)
    if (root / "meta.json").is_file():
        return [root]
    sessions = [p.parent for p in root.glob("session_*/meta.json")]
    sessions += [p.parent for p in root.glob("*_session_*/meta.json")]
    return sorted(set(sessions))


def _find_overlapping_logs(camera_dir: Path, start_ms: int, end_ms: int) -> list[Path]:
    if not camera_dir.is_dir():
        return []
    hits: list[tuple[int, Path]] = []
    for path in sorted(camera_dir.glob("gta_camera_*.jsonl")):
        samples, header = iter_camera_samples(path)
        if not samples:
            continue
        log_start = int(header.get("start_unix_ms") or samples[0].t_unix_ms)
        log_end = samples[-1].t_unix_ms
        if log_end < start_ms or log_start > end_ms:
            continue
        overlap = min(log_end, end_ms) - max(log_start, start_ms)
        hits.append((overlap, path))
    hits.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in hits]


def merge_legacy(
    session_dir: Path,
    *,
    camera_log: Path | None,
    camera_dir: Path | None,
    max_dt_ms: float,
    dry_run: bool,
) -> dict:
    """Fallback when camera_raw.jsonl is missing: use .gta_camera/*.jsonl."""
    session_dir = Path(session_dir)
    meta = _load_json(session_dir / "meta.json")
    start_ms = int(meta["start_epoch_ms"])
    duration_s = float(meta.get("duration_s") or 0.0)
    fps = int(meta.get("fps") or 30)
    if duration_s <= 0 and meta.get("total_frames"):
        duration_s = float(meta["total_frames"]) / float(fps)
    end_ms = start_ms + int(duration_s * 1000)
    sync_offset = int(meta.get("event_video_sync_offset") or 0)
    total_frames = int(meta.get("total_frames") or max(0, round(duration_s * fps)))

    if camera_dir is None:
        camera_dir = gta_camera_dir(session_dir.parent)

    raw_in_session = session_dir / CAMERA_RAW_FILENAME
    if not raw_in_session.is_file():
        legacy_raw = session_dir / "camera_raw.jsonl"
        if legacy_raw.is_file():
            raw_in_session = legacy_raw
    if camera_log is None and raw_in_session.is_file():
        if dry_run:
            return {
                "session_id": meta.get("session_id", session_dir.name),
                "frames_matched": "?",
                "frames_total": total_frames,
                "camera_sources": [raw_in_session.name],
                "output": str(session_dir / "camera.jsonl"),
            }
        summary = finalize_session_camera(
            session_dir,
            meta,
            max_dt_ms=max_dt_ms,
            wait_raw_s=0.0,
            keep_raw=True,
        )
        return {
            "session_id": meta.get("session_id", session_dir.name),
            "frames_matched": (summary or {}).get("frames_matched", 0),
            "frames_total": total_frames,
            "camera_sources": [raw_in_session.name],
            "align": (summary or {}).get("align"),
            "output": str(session_dir / "camera.jsonl"),
        }

    logs = [Path(camera_log)] if camera_log else _find_overlapping_logs(
        camera_dir, start_ms, end_ms
    )
    if not logs:
        raise FileNotFoundError(
            f"no camera_raw.jsonl and no overlapping logs in {camera_dir}"
        )

    all_samples = []
    sources = []
    raw_headers = []
    for path in logs:
        samples, header = iter_camera_samples(path)
        all_samples.extend(samples)
        sources.append(path.name)
        raw_headers.append(header)
    all_samples.sort(key=lambda s: s.t_unix_ms)

    ts_name = meta.get("frame_timestamps_file") or FRAME_TIMESTAMPS_FILENAME
    ts_path = session_dir / ts_name
    frame_times = None
    align_mode = "unix_ms_vs_start_epoch_ms_plus_sync_offset"
    if ts_path.is_file():
        frame_times = load_frame_capture_times(ts_path)
        if frame_times:
            align_mode = "t_capture_unix_ms_from_frame_timestamps"

    records, matched, missing = align_samples_to_frames(
        all_samples,
        start_epoch_ms=start_ms,
        fps=fps,
        total_frames=total_frames,
        sync_offset=sync_offset,
        max_dt_ms=max_dt_ms,
        frame_times=frame_times,
    )
    result = {
        "session_id": meta.get("session_id", session_dir.name),
        "camera_sources": sources,
        "frames_matched": matched,
        "frames_missing": missing,
        "frames_total": total_frames,
        "align": align_mode,
        "output": str(session_dir / "camera.jsonl"),
    }
    if dry_run:
        return result

    out_path = session_dir / "camera.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
    schemas = {header.get("schema") for header in raw_headers if header.get("schema")}
    cam_meta = {
        "source": "gta_scripthook_gameplay_cam",
        "schema": schemas.pop() if len(schemas) == 1 else GTA_CAMERA_SOURCE.schema,
        "file": "camera.jsonl",
        "raw_logs": sources,
        "sample_count_raw": len(all_samples),
        "frames_matched": matched,
        "frames_missing": missing,
        "max_dt_ms": max_dt_ms,
        "align": align_mode,
    }
    if len(raw_headers) == 1:
        header = raw_headers[0]
        geometry_keys = (
            "world_units",
            "matrix_layout",
            "matrix_vector_convention",
            "world_axes",
            "camera_axes",
            "camera_to_world_source",
            "sample_policy",
            "fov_axis",
            "projection_source",
        )
        geometry = {key: header[key] for key in geometry_keys if key in header}
        if geometry:
            cam_meta["geometry"] = geometry
    if frame_times:
        cam_meta["frame_timestamps_file"] = ts_path.name
    meta["camera"] = cam_meta
    with open(session_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge GTA camera JSONL into sessions")
    parser.add_argument("path", type=Path, help="session dir or recordings root")
    parser.add_argument("--camera-log", type=Path, default=None)
    parser.add_argument("--camera-dir", type=Path, default=None)
    parser.add_argument("--max-dt-ms", type=float, default=50.0)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    sessions = _discover_sessions(args.path)
    if not sessions:
        print(f"no session found at {args.path}", file=sys.stderr)
        return 2
    if len(sessions) > 1 and not args.all:
        print(
            f"found {len(sessions)} sessions; pass one session path or --all",
            file=sys.stderr,
        )
        return 2

    rc = 0
    for session_dir in sessions:
        try:
            info = merge_legacy(
                session_dir,
                camera_log=args.camera_log,
                camera_dir=args.camera_dir,
                max_dt_ms=args.max_dt_ms,
                dry_run=args.dry_run,
            )
        except FileNotFoundError as exc:
            print(f"[skip] {session_dir.name}: {exc}", file=sys.stderr)
            rc = 1
            continue
        tag = "dry-run" if args.dry_run else "ok"
        align = info.get("align") or "?"
        print(
            f"[{tag}] {info['session_id']}: "
            f"{info['frames_matched']}/{info['frames_total']} frames "
            f"from {info['camera_sources']} ({align}) → {info['output']}"
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
