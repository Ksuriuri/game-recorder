#!/usr/bin/env python3
"""Burn input HUD onto a game-recorder segment (MP4 + JSONL).

Expects the sidecar ``.jsonl`` produced by ``ActionWriter`` (same basename as the
video). Segment filenames look ``YYYYMMDD_HHMMSS_<start>_<end>.mp4`` where
``<end>`` is the exclusive global frame index, matching ``Session`` output.

- Bottom-left: WASD in a cross (W=up, S=down, A=left, D=right); active while held.
- Bottom-right: view-movement arrows from mouse deltas (↑ / ← / → / ↓). Per-frame
  deltas are **EMA-smoothed** (``--mouse-ema``); components inside ``--mouse-deadzone``
  are ignored; a direction lights only if the smoothed axis exceeds
  ``--mouse-threshold`` and wins ``--mouse-axis-ratio`` over the perpendicular axis
  (reduces diagonal flicker from noise).

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
            f"警告：已设置 GAME_RECORDER_FFMPEG 但不是有效文件：{override!r} — 跳过音频混流。",
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
            f"错误：ffmpeg 音频混流失败（退出码 {proc.returncode}）：\n{proc.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def finalize_output(
    ffmpeg_bin: str,
    *,
    video_only: Path,
    audio_source: Path | None,
    destination: Path,
    encode_crf: int | None,
    preset: str,
    audio_bitrate: str,
) -> bool:
    """Mux or re-encode the HUD video into the final output file."""
    if encode_crf is None:
        if audio_source is None:
            try:
                video_only.replace(destination)
                return True
            except OSError as e:
                print(f"错误：无法移动 {video_only} → {destination}：{e}", file=sys.stderr)
                return False
        return mux_audio_copy(
            ffmpeg_bin,
            video_only=video_only,
            audio_source=audio_source,
            destination=destination,
        )

    cmd: list[str] = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(video_only),
    ]
    if audio_source is not None:
        cmd += ["-i", str(audio_source)]
    cmd += [
        "-map",
        "0:v:0",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(encode_crf),
        "-pix_fmt",
        "yuv420p",
    ]
    if audio_source is not None:
        cmd += [
            "-map",
            "1:a?",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
        ]
    cmd.append(str(destination))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(
            f"错误：ffmpeg 编码/混流失败（退出码 {proc.returncode}）：\n{proc.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def scaled_output_size(
    width: int, height: int, max_width: int | None
) -> tuple[int, int, float]:
    """Return even (out_w, out_h) and scale factor applied to the source frame."""
    if max_width is None or width <= max_width or width <= 0:
        return width, height, 1.0
    scale = max_width / width
    out_w = max(2, int(round(width * scale)) // 2 * 2)
    out_h = max(2, int(round(height * scale)) // 2 * 2)
    return out_w, out_h, out_w / width


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


def _parse_frame_records_from_text(text: str) -> list[dict[str, Any]]:
    """Extract valid frame records from a possibly corrupted JSONL fragment."""
    dec = json.JSONDecoder()
    records: list[dict[str, Any]] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] != "{":
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if (
            isinstance(obj, dict)
            and isinstance(obj.get("frame"), int)
            and isinstance(obj.get("events"), list)
        ):
            records.append(obj)
        i = end if end > i else i + 1
    return records


def _ingest_event_record(
    out: dict[int, list[dict[str, Any]]],
    rec: dict[str, Any],
    *,
    jsonl_path: Path,
    line_no: int,
) -> None:
    frame = rec.get("frame")
    events = rec.get("events")
    if not isinstance(frame, int) or not isinstance(events, list):
        raise SystemExit(
            f"{jsonl_path}:{line_no}: expected {{'frame': int, 'events': [...]}}"
        )
    out.setdefault(frame, []).extend(events)


def load_events_by_frame(jsonl_path: Path) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    bad_lines = 0
    with jsonl_path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                recs = [json.loads(line)]
            except json.JSONDecodeError:
                recs = _parse_frame_records_from_text(line)
                if not recs:
                    print(
                        f"警告：{jsonl_path}:{line_no} JSON 损坏且无法恢复，已跳过。",
                        file=sys.stderr,
                    )
                    bad_lines += 1
                    continue
                print(
                    f"警告：{jsonl_path}:{line_no} JSON 损坏，已恢复 {len(recs)} 条记录。",
                    file=sys.stderr,
                )
                bad_lines += 1
            for rec in recs:
                _ingest_event_record(out, rec, jsonl_path=jsonl_path, line_no=line_no)
    if bad_lines:
        print(
            f"警告：{jsonl_path.name} 中有 {bad_lines} 行损坏（已尽量恢复）。",
            file=sys.stderr,
        )
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
            dx, dy = ev.get("dx"), ev.get("dy")
            if isinstance(dx, int) and isinstance(dy, int):
                mouse_dx += dx
                mouse_dy += dy
                moved = moved or dx != 0 or dy != 0
                continue

            # Older recordings stored absolute cursor coordinates from WH_MOUSE_LL.
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
    ap = argparse.ArgumentParser(
        description="将键鼠 HUD 叠加到 game-recorder 分段视频（MP4 + JSONL）。",
        add_help=False,
    )
    ap.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出")
    ap.add_argument("video", type=Path, help="分段 .mp4（例：20260421_022902_0_385.mp4）")
    ap.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="操作日志路径（默认：与视频同主文件名 .jsonl）",
    )
    ap.add_argument("-o", "--output", type=Path, default=None, help="输出 mp4 路径")
    ap.add_argument(
        "--fps",
        type=int,
        default=None,
        help="事件标注使用的录制 FPS（默认：meta.json，否则视频 FPS）",
    )
    ap.add_argument(
        "--segment-start",
        type=int,
        default=None,
        help="首帧的全局帧索引（默认：从文件名解析）",
    )
    ap.add_argument(
        "--mouse-threshold",
        type=float,
        default=0.8,
        help="点亮方向所需的最小 |平滑 dx| 或 |平滑 dy|（死区之后）",
    )
    ap.add_argument(
        "--mouse-ema",
        type=float,
        default=0.22,
        metavar="A",
        help="鼠标增量 EMA 混合系数（0–1）；越小越平滑、响应越慢",
    )
    ap.add_argument(
        "--mouse-deadzone",
        type=float,
        default=0.3,
        metavar="PX",
        help="平滑后 |dx|/|dy| 低于此值视为 0（箭头 HUD）",
    )
    ap.add_argument(
        "--mouse-axis-ratio",
        type=float,
        default=1.2,
        metavar="R",
        help="仅当 |平滑 dy| ≥ R×|平滑 dx| 时点亮垂直方向，水平同理；"
        "设为 1.0 可关闭此过滤",
    )
    ap.add_argument(
        "--event-frame-offset",
        type=int,
        default=None,
        metavar="N",
        help="jsonl 帧索引 = 视频帧索引 + N（默认：meta.json event_video_sync_offset，否则 0）",
    )
    ap.add_argument(
        "--event-frame-lead",
        type=int,
        default=0,
        metavar="K",
        help="jsonl 查找索引加 K（正数 = 更新输入；HUD 仍滞后可试 1–3）",
    )
    ap.add_argument(
        "--hud-cell",
        type=int,
        default=32,
        metavar="PX",
        help="每个按键框半宽像素（完整方块 = 2× 此值）",
    )
    ap.add_argument(
        "--hud-gap",
        type=int,
        default=80,
        metavar="PX",
        help="相邻方块内缘之间的空白像素（水平/垂直）",
    )
    ap.add_argument(
        "--hud-margin",
        type=int,
        default=48,
        metavar="PX",
        help="HUD 外框与视频边缘的内边距",
    )
    ap.add_argument(
        "--no-audio",
        action="store_true",
        help="不混流源 mp4 的音频（OpenCV 输出仅视频）",
    )
    ap.add_argument(
        "--max-width",
        type=int,
        default=None,
        metavar="PX",
        help="输出最大宽度（保持比例；例如 960 可显著减小体积）",
    )
    ap.add_argument(
        "--crf",
        type=int,
        default=None,
        metavar="N",
        help="libx264 CRF（18–32；越大文件越小，需 ffmpeg 重编码）",
    )
    ap.add_argument(
        "--preset",
        type=str,
        default="veryfast",
        help="libx264 preset（默认 veryfast；更慢 preset 略省体积）",
    )
    ap.add_argument(
        "--audio-bitrate",
        type=str,
        default="64k",
        metavar="RATE",
        help="重编码时的 AAC 码率（默认 64k；仅 --crf 时生效）",
    )
    args = ap.parse_args()
    video: Path = args.video
    if not video.is_file():
        print(f"错误：找不到视频：{video}", file=sys.stderr)
        sys.exit(1)

    jsonl = args.jsonl or video.with_suffix(".jsonl")
    if not jsonl.is_file():
        print(f"错误：找不到 jsonl：{jsonl}", file=sys.stderr)
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
            "警告：无法从文件名解析分段起始帧；假定全局帧 0。",
            file=sys.stderr,
        )

    if parsed and meta_start is not None and parsed[0] != meta_start:
        print(
            f"警告：文件名 start_frame {parsed[0]} != meta.json start_frame {meta_start}；"
            f"使用 --segment-start / meta / CLI 解析结果。",
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
            f"提示：jsonl 帧 = 视频索引 + {meta_sync} "
            f"（meta.json 中的 event_video_sync_offset）。",
            file=sys.stderr,
        )
    if event_lead != 0:
        print(
            f"提示：事件帧超前 {event_lead:+d}（jsonl 查找索引已偏移）。",
            file=sys.stderr,
        )

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"错误：无法打开视频：{video}", file=sys.stderr)
        sys.exit(1)

    vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    vfps = cap.get(cv2.CAP_PROP_FPS)
    vfps_i = int(round(vfps)) if vfps and vfps > 1e-3 else 30

    fps = args.fps or meta_fps or vfps_i
    if args.fps is None and meta_fps is not None and abs(meta_fps - vfps_i) > 1:
        print(
            f"提示：使用 meta/录制 fps={meta_fps} 对齐事件 "
            f"（容器报告 {vfps_i}）。",
            file=sys.stderr,
        )

    out_w, out_h, frame_scale = scaled_output_size(vw, vh, args.max_width)
    if frame_scale != 1.0:
        print(
            f"提示：输出分辨率 {out_w}x{out_h}（源 {vw}x{vh}，--max-width {args.max_width}）。",
            file=sys.stderr,
        )

    encode_crf = int(args.crf) if args.crf is not None else None
    if encode_crf is not None:
        encode_crf = max(0, min(51, encode_crf))
        print(
            f"提示：将用 libx264 CRF {encode_crf}（preset {args.preset}）重编码输出。",
            file=sys.stderr,
        )

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out_path = args.output or video.with_name(f"{video.stem}_inputs{video.suffix}")
    mux_audio = not args.no_audio
    needs_ffmpeg = mux_audio or encode_crf is not None
    ffmpeg_bin = find_ffmpeg() if needs_ffmpeg else None
    if needs_ffmpeg and not ffmpeg_bin:
        if encode_crf is not None:
            print(
                "错误：--crf 需要 ffmpeg；请安装 ffmpeg（见 install.bat）"
                "或设置 GAME_RECORDER_FFMPEG。",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            "警告：未找到 ffmpeg — 仅输出视频；"
            "请安装 ffmpeg（见 install.bat）或设置 GAME_RECORDER_FFMPEG。",
            file=sys.stderr,
        )
        mux_audio = False

    use_temp = mux_audio or encode_crf is not None
    hud_tmp = (
        out_path.with_name(f"{out_path.stem}._hud_tmp{out_path.suffix}")
        if use_temp
        else out_path
    )
    writer = cv2.VideoWriter(str(hud_tmp), fourcc, float(fps), (out_w, out_h))
    if not writer.isOpened():
        print(f"错误：无法打开 VideoWriter：{hud_tmp}", file=sys.stderr)
        sys.exit(1)

    events_by_frame = load_events_by_frame(jsonl)

    vk_down = {VK_W: False, VK_A: False, VK_S: False, VK_D: False}
    last_mouse_cell: list[tuple[int, int] | None] = [None]
    mouse_smooth_xy: list[float] = [0.0, 0.0]

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
        mouse_dx, mouse_dy, _moved = apply_input_bucket(
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
        ema = max(1e-6, min(1.0, float(args.mouse_ema)))
        sx = (1.0 - ema) * mouse_smooth_xy[0] + ema * float(mouse_dx)
        sy = (1.0 - ema) * mouse_smooth_xy[1] + ema * float(mouse_dy)
        mouse_smooth_xy[0], mouse_smooth_xy[1] = sx, sy

        dz = max(0.0, float(args.mouse_deadzone))
        if abs(sx) < dz:
            sx = 0.0
        if abs(sy) < dz:
            sy = 0.0

        th = float(args.mouse_threshold)
        ratio = max(1.0, float(args.mouse_axis_ratio))
        ax_x, ax_y = abs(sx), abs(sy)
        mouse_ui = {
            "up": sy < -th and ax_y >= ax_x * ratio,
            "down": sy > th and ax_y >= ax_x * ratio,
            "left": sx < -th and ax_x >= ax_y * ratio,
            "right": sx > th and ax_x >= ax_y * ratio,
        }

        draw_wasd_hud(frame, margin=margin, cell_half=ch, stride=stride, keys=keys_ui)
        draw_mouse_hud(frame, margin=margin, cell_half=ch, stride=stride, dirs=mouse_ui)

        if frame_scale != 1.0:
            frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)
        writer.write(frame)
        frame_i += 1

    cap.release()
    writer.release()

    if use_temp:
        assert ffmpeg_bin is not None
        ok = finalize_output(
            ffmpeg_bin,
            video_only=hud_tmp,
            audio_source=video if mux_audio else None,
            destination=out_path,
            encode_crf=encode_crf,
            preset=str(args.preset),
            audio_bitrate=str(args.audio_bitrate),
        )
        if ok:
            try:
                hud_tmp.unlink(missing_ok=True)
            except OSError as e:
                print(f"警告：无法删除临时文件 {hud_tmp}：{e}", file=sys.stderr)
            audio_note = " + 音频" if mux_audio else ""
            print(f"已写入 {frame_i} 帧{audio_note} → {out_path.resolve()}")
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
                    f"错误：ffmpeg 处理失败且无法移动 {hud_tmp} → {out_path}：{e}",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(
                f"错误：ffmpeg 处理失败；已保存中间视频至 {out_path.resolve()}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        print(f"已写入 {frame_i} 帧 → {out_path.resolve()}")


if __name__ == "__main__":
    main()
