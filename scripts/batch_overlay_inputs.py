#!/usr/bin/env python3
"""Batch overlay input HUD onto recordings with overall progress and ETA."""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_recording_videos import iter_videos
from overlay_inputs_on_video import run_overlay
from progress_utils import ProgressWriter, eta_from_rate, format_duration, progress_bar


def count_frames(path: Path) -> int:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return 0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return max(0, n)


def output_is_up_to_date(video: Path, output: Path) -> bool:
    if not output.is_file():
        return False
    out_mtime = output.stat().st_mtime
    for src in (video, video.with_suffix(".jsonl")):
        if src.is_file() and src.stat().st_mtime > out_mtime:
            return False
    return True


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="批量叠加输入 HUD，显示总进度与 ETA。")
    ap.add_argument("recordings", type=Path, help="recordings 根目录")
    ap.add_argument("--output-dir", type=Path, required=True, help="输出目录")
    ap.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        metavar="NAME",
        help="跳过的子目录名（可重复）",
    )
    ap.add_argument("--sample", type=int, default=None, metavar="N", help="随机抽 N 条")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    ap.add_argument("--max-width", type=int, default=960)
    ap.add_argument("--crf", type=int, default=26)
    ap.add_argument("--preset", type=str, default="veryfast")
    ap.add_argument("--audio-bitrate", type=str, default="64k")
    ap.add_argument(
        "--event-frame-lead",
        type=int,
        default=1,
        metavar="K",
        help="jsonl 查找索引加 K（默认 1，补偿 HUD 相对画面的滞后）",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="即使输出已存在也重新处理",
    )
    return ap


def collect_videos(args: argparse.Namespace) -> list[Path]:
    exclude = set(args.exclude_dir)
    videos = iter_videos(args.recordings, exclude_dirs=exclude)
    if args.sample is not None:
        n = min(max(0, args.sample), len(videos))
        if args.seed is not None:
            random.seed(args.seed)
        videos = random.sample(videos, n) if n else []
    return videos


def make_overlay_args(args: argparse.Namespace, video: Path, output: Path) -> argparse.Namespace:
    return argparse.Namespace(
        video=video,
        jsonl=None,
        output=output,
        fps=None,
        segment_start=None,
        mouse_threshold=0.8,
        mouse_ema=0.5,
        mouse_deadzone=0.3,
        mouse_axis_ratio=1.2,
        event_frame_offset=None,
        event_frame_lead=args.event_frame_lead,
        hud_cell=32,
        hud_gap=80,
        hud_margin=48,
        no_audio=False,
        max_width=args.max_width,
        crf=args.crf,
        preset=args.preset,
        audio_bitrate=args.audio_bitrate,
        progress=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    recordings = args.recordings
    if not recordings.is_dir():
        print(f"错误：找不到目录：{recordings}", file=sys.stderr)
        sys.exit(1)

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    all_videos = collect_videos(args)
    if not all_videos:
        print(f"错误：{recordings} 中未找到带匹配 jsonl 的 mp4 文件", file=sys.stderr)
        sys.exit(1)

    skipped = 0
    if args.force:
        videos = all_videos
    else:
        videos = []
        for video in all_videos:
            if output_is_up_to_date(video, output_dir / video.name):
                skipped += 1
            else:
                videos.append(video)

    if skipped:
        print(f"跳过 {skipped} 个已处理视频", flush=True)
    if not videos:
        print(f"全部 {len(all_videos)} 个视频均已处理，无需重做。")
        print(f"输出：{output_dir.resolve()}")
        sys.exit(0)

    frame_counts = [count_frames(v) for v in videos]
    total_frames = sum(frame_counts) or None
    total_videos = len(videos)

    print(f"待处理 {total_videos} 个视频", end="", flush=True)
    if total_frames:
        print(f"，约 {total_frames} 帧", end="", flush=True)
    if skipped:
        print(f"（共 {len(all_videos)} 个，已跳过 {skipped} 个）", end="", flush=True)
    print(f"\n输出目录：{output_dir.resolve()}\n", flush=True)

    writer = ProgressWriter()
    batch_start = time.perf_counter()
    done_frames = 0
    failed = 0
    avg_sec_per_video: float | None = None

    for idx, video in enumerate(videos, 1):
        out_path = output_dir / video.name
        video_frames = frame_counts[idx - 1] or None
        video_start = time.perf_counter()

        def on_progress(
            current: int,
            current_total: int | None,
            phase: str,
            *,
            video_idx: int = idx,
            video_path: Path = video,
        ) -> None:
            if phase == "frames":
                overall_done = done_frames + current
                elapsed = time.perf_counter() - batch_start
                if total_frames:
                    overall_eta = eta_from_rate(overall_done, total_frames, elapsed)
                    overall_bar = progress_bar(overall_done, total_frames, width=14)
                    overall_pct = f"{overall_done}/{total_frames}"
                else:
                    overall_eta = "--:--"
                    overall_bar = progress_bar(video_idx - 1, total_videos, width=14)
                    overall_pct = f"{video_idx - 1}/{total_videos}"

                video_elapsed = time.perf_counter() - video_start
                if current_total:
                    video_eta = eta_from_rate(current, current_total, video_elapsed)
                    video_bar = progress_bar(current, current_total, width=14)
                    video_pct = f"{current}/{current_total}"
                else:
                    video_eta = "--:--"
                    video_bar = progress_bar(current, None, width=14)
                    video_pct = str(current)

                writer.update(
                    f"总 {video_idx}/{total_videos} {overall_bar} {overall_pct} "
                    f"已用 {format_duration(elapsed)} 剩 {overall_eta} | "
                    f"{video_path.name} {video_bar} {video_pct} "
                    f"已用 {format_duration(video_elapsed)} 剩 {video_eta}"
                )
            elif phase == "encode":
                elapsed = time.perf_counter() - batch_start
                writer.update(
                    f"总 {video_idx}/{total_videos} 已用 {format_duration(elapsed)} | "
                    f"{video_path.name} 编码/混流中...",
                    force=True,
                )

        result = run_overlay(make_overlay_args(args, video, out_path), on_progress=on_progress)
        if result <= 0:
            writer.finish(f"失败 {video.name}")
            failed += 1
            continue

        done_frames += result
        video_elapsed = time.perf_counter() - video_start
        completed = idx
        avg_sec_per_video = (
            (avg_sec_per_video * (completed - 1) + video_elapsed) / completed
            if avg_sec_per_video is not None
            else video_elapsed
        )
        remaining_videos = total_videos - completed
        batch_eta = (
            format_duration(avg_sec_per_video * remaining_videos)
            if avg_sec_per_video is not None and remaining_videos > 0
            else "0:00"
        )
        writer.finish(
            f"[{completed}/{total_videos}] 完成 {video.name}  "
            f"总已用 {format_duration(time.perf_counter() - batch_start)}  "
            f"预计剩余 {batch_eta}"
        )

    total_elapsed = time.perf_counter() - batch_start
    if failed:
        print(
            f"\n完成，但有 {failed} 个失败。成功 {total_videos - failed}/{total_videos}，"
            f"总耗时 {format_duration(total_elapsed)}。",
            file=sys.stderr,
        )
        sys.exit(1)

    summary = f"\n全部完成：处理 {total_videos} 个视频"
    if skipped:
        summary += f"，跳过 {skipped} 个"
    summary += f"，总耗时 {format_duration(total_elapsed)}。\n输出：{output_dir.resolve()}"
    print(summary, flush=True)


if __name__ == "__main__":
    main()
