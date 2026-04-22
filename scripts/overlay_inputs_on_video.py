#!/usr/bin/env python3
"""Burn input HUD onto a game-recorder segment (MP4 + JSONL).

Expects the sidecar ``.jsonl`` produced by ``ActionWriter`` (same basename as the
video). Segment filenames look ``YYYYMMDD_HHMMSS_<start>_<end>.mp4`` where
``<end>`` is the exclusive global frame index, matching ``Session`` output.

- Bottom-left: WASD in a cross (W=up, S=down, A=left, D=right); active while held.
- Bottom-right: view-movement arrows from mouse deltas (↑ / ← / → / ↓); lit when
  movement in that direction exceeds ``--mouse-threshold`` for that frame.

Run from repo root (uses project venv / ``uv run``):

  uv run python scripts/overlay_inputs_on_video.py path/to/20260421_022902_0_385.mp4

Spacing: ``--hud-cell``, ``--hud-gap``, ``--hud-margin``.

Event/video sync: ``meta.json`` has ``event_video_sync_offset`` (median wall−idx per
video frame). The script replays jsonl buckets before the first aligned frame, then
``--event-frame-lead`` can nudge a few frames if mux/capture still lags.

Audio: OpenCV only writes a video track; by default the script muxes the **original**
segment’s audio onto the HUD file using ``ffmpeg`` (same lookup as ``install.bat`` /
``GAME_RECORDER_FFMPEG`` / ``PATH``). Use ``--no-audio`` to skip muxing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

# Windows virtual-key codes (same as game_recorder.capture.input_hook)
VK_W = 0x57
VK_A = 0x41
VK_S = 0x53
VK_D = 0x44

SEGMENT_NAME_RE = re.compile(
    r"^(\d{8}_\d{6})_(\d+)_(\d+)\.(mp4|MP4)$",
)


def _repo_root() -> Path:
    """``scripts/overlay_inputs_on_video.py`` → repository root."""
    return Path(__file__).resolve().parent.parent


def find_ffmpeg() -> str | None:
    """Same resolution order as ``game_recorder.config.find_ffmpeg`` (no sys.exit)."""
    override = os.environ.get("GAME_RECORDER_FFMPEG", "").strip()
    if override:
        p = Path(override)
        if p.is_file():
            return str(p.resolve())
        print(
            f"WARN: GAME_RECORDER_FFMPEG is set but not a file: {override!r} — skipping audio mux.",
            file=sys.stderr,
        )
        return None
    root = _repo_root()
    for rel in (("ffmpeg", "bin", "ffmpeg.exe"), ("ffmpeg", "ffmpeg.exe")):
        candidate = root.joinpath(*rel)
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("ffmpeg")
    return found


def mux_audio_copy(
    ffmpeg_bin: str,
    *,
    video_only: Path,
    audio_source: Path,
    destination: Path,
) -> bool:
    """H.264/MPEG-4 video from ``video_only`` + audio copied from ``audio_source`` → ``destination``."""
    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(video_only),
        "-i",
        str(audio_source),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        str(destination),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(
            f"ERROR: ffmpeg audio mux failed (exit {proc.returncode}):\n{proc.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def parse_segment_from_filename(path: Path) -> tuple[int, int] | None:
    """Return ``(start_frame, end_frame_exclusive)`` if name matches segment pattern."""
    m = SEGMENT_NAME_RE.match(path.name)
    if not m:
        return None
    return int(m.group(2)), int(m.group(3))


def load_meta_fps_segment_sync(video: Path) -> tuple[int | None, int | None, int | None, int]:
    """Load fps, segment bounds, and event↔video frame offset from ``meta.json`` if present."""
    for parent in [video.parent, *video.parents]:
        meta = parent / "meta.json"
        if not meta.is_file():
            continue
        try:
            data: dict[str, Any] = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_fps = data.get("fps")
        fps_i: int | None = None
        if isinstance(raw_fps, (int, float)) and float(raw_fps) > 0:
            fps_i = int(round(float(raw_fps)))
        raw_sync = data.get("event_video_sync_offset")
        sync = int(raw_sync) if isinstance(raw_sync, (int, float)) else 0
        for seg in data.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            v = seg.get("video") or ""
            if Path(v).name == video.name:
                sf = seg.get("start_frame")
                ef = seg.get("end_frame")
                if isinstance(sf, int) and isinstance(ef, int):
                    return fps_i, sf, ef, sync
        if fps_i is not None:
            return fps_i, None, None, sync
    return None, None, None, 0


def load_events_by_frame(jsonl_path: Path) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    with jsonl_path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"{jsonl_path}:{line_no}: invalid JSON: {e}") from e
            frame = rec.get("frame")
            events = rec.get("events")
            if not isinstance(frame, int) or not isinstance(events, list):
                raise SystemExit(
                    f"{jsonl_path}:{line_no}: expected {{'frame': int, 'events': [...]}}"
                )
            out.setdefault(frame, []).extend(events)
    return out


def apply_input_bucket(
    evs: list[dict[str, Any]],
    vk_down: dict[int, bool],
    last_mouse_cell: list[tuple[int, int] | None],
) -> tuple[int, int, bool]:
    """Apply one jsonl frame-bucket; mutates ``vk_down`` and ``last_mouse_cell[0]``.

    Returns mouse move delta sum and whether any move occurred in this bucket.
    """
    mouse_dx, mouse_dy = 0, 0
    moved = False
    lm = last_mouse_cell[0]
    for ev in evs:
        et = ev.get("type")
        if et == "key":
            vk = ev.get("vk")
            action = ev.get("action")
            if isinstance(vk, int) and vk in vk_down and action in ("down", "up"):
                vk_down[vk] = action == "down"
        elif et == "mouse" and ev.get("action") == "move":
            x, y = ev.get("x"), ev.get("y")
            if isinstance(x, int) and isinstance(y, int):
                if lm is not None:
                    mouse_dx += x - lm[0]
                    mouse_dy += y - lm[1]
                    moved = True
                lm = (x, y)
    last_mouse_cell[0] = lm
    return mouse_dx, mouse_dy, moved


def draw_cell(
    frame: np.ndarray,
    cx: int,
    cy: int,
    half: int,
    label: str,
    active: bool,
) -> None:
    x1, y1 = cx - half, cy - half
    x2, y2 = cx + half, cy + half
    fill = (60, 200, 80) if active else (45, 45, 48)
    border = (120, 255, 140) if active else (80, 80, 85)
    cv2.rectangle(frame, (x1, y1), (x2, y2), fill, -1)
    cv2.rectangle(frame, (x1, y1), (x2, y2), border, 2)
    color = (20, 20, 20) if active else (200, 200, 200)
    # Scale down so glyphs + anti-alias stay inside the tile (avoids “overlap” with neighbors).
    font_scale = max(0.45, min(0.68, half * 0.0185))
    thickness = 2 if half >= 28 else 1
    (tw, th), _baseline = cv2.getTextSize(
        label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
    )
    tx = cx - tw // 2
    # ``putText`` y = baseline; match previous vertical placement (good enough in-tile).
    ty = cy + th // 2
    cv2.putText(
        frame,
        label,
        (tx, ty),
        cv2.FONT_HERSHEY_SIMPLEX,
        font_scale,
        color,
        thickness,
        cv2.LINE_AA,
    )


def draw_wasd_hud(
    frame: np.ndarray,
    *,
    margin: int,
    cell_half: int,
    stride: int,
    keys: dict[str, bool],
) -> None:
    """Cross layout: W up, S down, A left, D right.

    ``stride`` is hub→key-center distance; must be > ``cell_half`` so keys do not
    touch (face gap ≈ ``2 * (stride - cell_half)``).
    """
    h, w = frame.shape[:2]
    cx = margin + stride + cell_half
    cy = h - margin - stride - cell_half
    draw_cell(frame, cx, cy - stride, cell_half, "W", keys["up"])
    draw_cell(frame, cx, cy + stride, cell_half, "S", keys["down"])
    draw_cell(frame, cx - stride, cy, cell_half, "A", keys["left"])
    draw_cell(frame, cx + stride, cy, cell_half, "D", keys["right"])


def draw_mouse_hud(
    frame: np.ndarray,
    *,
    margin: int,
    cell_half: int,
    stride: int,
    dirs: dict[str, bool],
) -> None:
    """Same cross; arrows as ASCII (reliable with OpenCV font)."""
    h, w = frame.shape[:2]
    cx = w - margin - stride - cell_half
    cy = h - margin - stride - cell_half
    draw_cell(frame, cx, cy - stride, cell_half, "^", dirs["up"])
    draw_cell(frame, cx, cy + stride, cell_half, "v", dirs["down"])
    draw_cell(frame, cx - stride, cy, cell_half, "<", dirs["left"])
    draw_cell(frame, cx + stride, cy, cell_half, ">", dirs["right"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", type=Path, help="Segment .mp4 (e.g. 20260421_022902_0_385.mp4)")
    ap.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="Action log path (default: same stem as video with .jsonl)",
    )
    ap.add_argument("-o", "--output", type=Path, default=None, help="Output mp4 path")
    ap.add_argument(
        "--fps",
        type=int,
        default=None,
        help="Recording FPS used when tagging events (default: meta.json, else video FPS)",
    )
    ap.add_argument(
        "--segment-start",
        type=int,
        default=None,
        help="Global frame index of first video frame (default: parsed from filename)",
    )
    ap.add_argument(
        "--mouse-threshold",
        type=float,
        default=2.0,
        help="Min |dx| or |dy| in pixels per frame to light a mouse direction",
    )
    ap.add_argument(
        "--event-frame-offset",
        type=int,
        default=None,
        metavar="N",
        help="jsonl frame index = video_frame_index + N (default: meta.json event_video_sync_offset, else 0)",
    )
    ap.add_argument(
        "--event-frame-lead",
        type=int,
        default=0,
        metavar="K",
        help="Add K to jsonl lookup (positive = newer inputs; try 1–3 if HUD still lags)",
    )
    ap.add_argument(
        "--hud-cell",
        type=int,
        default=32,
        metavar="PX",
        help="Half-side of each key box in pixels (full tile = 2× this)",
    )
    ap.add_argument(
        "--hud-gap",
        type=int,
        default=80,
        metavar="PX",
        help="Clear pixels between neighboring tile edges (horizontal / vertical)",
    )
    ap.add_argument(
        "--hud-margin",
        type=int,
        default=48,
        metavar="PX",
        help="Padding from video edges to the outer HUD bbox",
    )
    ap.add_argument(
        "--no-audio",
        action="store_true",
        help="Do not mux audio from the source mp4 (OpenCV output is video-only)",
    )
    args = ap.parse_args()
    video: Path = args.video
    if not video.is_file():
        print(f"ERROR: video not found: {video}", file=sys.stderr)
        sys.exit(1)

    jsonl = args.jsonl or video.with_suffix(".jsonl")
    if not jsonl.is_file():
        print(f"ERROR: jsonl not found: {jsonl}", file=sys.stderr)
        sys.exit(1)

    meta_fps, meta_start, meta_end, meta_sync = load_meta_fps_segment_sync(video)
    parsed = parse_segment_from_filename(video)
    if args.segment_start is not None:
        seg_start = args.segment_start
    elif meta_start is not None:
        seg_start = meta_start
    elif parsed:
        seg_start = parsed[0]
    else:
        seg_start = 0
        print(
            "WARN: could not parse segment start from filename; assuming global frame 0.",
            file=sys.stderr,
        )

    if parsed and meta_start is not None and parsed[0] != meta_start:
        print(
            f"WARN: filename start_frame {parsed[0]} != meta.json start_frame {meta_start}; "
            f"using --segment-start / meta / CLI resolution.",
            file=sys.stderr,
        )

    event_offset = (
        int(args.event_frame_offset)
        if args.event_frame_offset is not None
        else meta_sync
    )
    event_lead = int(args.event_frame_lead)
    if args.event_frame_offset is None and meta_sync != 0:
        print(
            f"NOTE: aligning jsonl frame = video_index + {meta_sync} "
            f"(event_video_sync_offset from meta.json).",
            file=sys.stderr,
        )
    if event_lead != 0:
        print(
            f"NOTE: event frame lead {event_lead:+d} (jsonl lookup index shifted).",
            file=sys.stderr,
        )

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"ERROR: cannot open video: {video}", file=sys.stderr)
        sys.exit(1)

    vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    vfps = cap.get(cv2.CAP_PROP_FPS)
    vfps_i = int(round(vfps)) if vfps and vfps > 1e-3 else 30

    fps = args.fps or meta_fps or vfps_i
    if args.fps is None and meta_fps is not None and abs(meta_fps - vfps_i) > 1:
        print(
            f"NOTE: using meta/recording fps={meta_fps} for event alignment "
            f"(container reports {vfps_i}).",
            file=sys.stderr,
        )

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out_path = args.output or video.with_name(f"{video.stem}_inputs{video.suffix}")
    mux_audio = not args.no_audio
    ffmpeg_bin = find_ffmpeg() if mux_audio else None
    if mux_audio and not ffmpeg_bin:
        print(
            "WARN: ffmpeg not found — writing video-only; "
            "install ffmpeg (see install.bat) or set GAME_RECORDER_FFMPEG.",
            file=sys.stderr,
        )
        mux_audio = False

    hud_tmp = (
        out_path.with_name(f"{out_path.stem}._hud_tmp{out_path.suffix}")
        if mux_audio
        else out_path
    )
    writer = cv2.VideoWriter(str(hud_tmp), fourcc, float(fps), (vw, vh))
    if not writer.isOpened():
        print(f"ERROR: cannot open VideoWriter for {hud_tmp}", file=sys.stderr)
        sys.exit(1)

    events_by_frame = load_events_by_frame(jsonl)

    vk_down = {VK_W: False, VK_A: False, VK_S: False, VK_D: False}
    last_mouse_cell: list[tuple[int, int] | None] = [None]

    first_lookup = seg_start + event_offset + event_lead
    for f in range(0, max(0, first_lookup)):
        apply_input_bucket(events_by_frame.get(f, []), vk_down, last_mouse_cell)

    margin = max(8, int(args.hud_margin))
    ch = max(12, int(args.hud_cell))
    gap = max(0, int(args.hud_gap))
    # Hub→key-center distance: 2*stride − 2*ch == gap between opposite inner edges.
    stride = ch + (gap + 1) // 2
    if stride <= ch + 2:
        stride = ch + 3

    frame_i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        global_f = seg_start + frame_i + event_offset + event_lead
        mouse_dx, mouse_dy, moved = apply_input_bucket(
            events_by_frame.get(global_f, []),
            vk_down,
            last_mouse_cell,
        )

        keys_ui = {
            "up": vk_down[VK_W],
            "down": vk_down[VK_S],
            "left": vk_down[VK_A],
            "right": vk_down[VK_D],
        }
        th = float(args.mouse_threshold)
        mouse_ui = {
            "up": moved and mouse_dy < -th,
            "down": moved and mouse_dy > th,
            "left": moved and mouse_dx < -th,
            "right": moved and mouse_dx > th,
        }

        draw_wasd_hud(frame, margin=margin, cell_half=ch, stride=stride, keys=keys_ui)
        draw_mouse_hud(frame, margin=margin, cell_half=ch, stride=stride, dirs=mouse_ui)

        writer.write(frame)
        frame_i += 1

    cap.release()
    writer.release()

    if mux_audio:
        assert ffmpeg_bin is not None
        ok = mux_audio_copy(
            ffmpeg_bin, video_only=hud_tmp, audio_source=video, destination=out_path
        )
        if ok:
            try:
                hud_tmp.unlink(missing_ok=True)
            except OSError as e:
                print(f"WARN: could not remove temp {hud_tmp}: {e}", file=sys.stderr)
            print(f"Wrote {frame_i} frames + audio → {out_path.resolve()}")
        else:
            try:
                if out_path.is_file():
                    out_path.unlink()
            except OSError:
                pass
            try:
                hud_tmp.replace(out_path)
            except OSError as e:
                print(
                    f"ERROR: ffmpeg mux failed and could not move {hud_tmp} → {out_path}: {e}",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(
                f"ERROR: ffmpeg mux failed; saved video-only (no audio) to {out_path.resolve()}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        print(f"Wrote {frame_i} frames → {out_path.resolve()}")


if __name__ == "__main__":
    main()
