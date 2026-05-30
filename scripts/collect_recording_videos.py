#!/usr/bin/env python3
"""List or randomly sample game-recorder segment videos (mp4 + matching jsonl)."""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path


def iter_videos(recordings: Path, *, exclude_dirs: set[str]) -> list[Path]:
    out: list[Path] = []
    for session in sorted(recordings.iterdir()):
        if not session.is_dir() or session.name in exclude_dirs:
            continue
        for mp4 in sorted(session.glob("*.mp4")):
            stem = mp4.stem
            if stem.endswith("_inputs") or stem.endswith("._hud_tmp"):
                continue
            if mp4.with_suffix(".jsonl").is_file():
                out.append(mp4.resolve())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="列出或随机抽样 recordings 中的分段视频。")
    ap.add_argument("recordings", type=Path, help="recordings 根目录")
    ap.add_argument(
        "--sample",
        type=int,
        default=None,
        metavar="N",
        help="随机抽取 N 条（不足 N 则全部输出）",
    )
    ap.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        metavar="NAME",
        help="跳过的子目录名（可重复）",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=None,
        help="随机种子（便于复现抽样结果）",
    )
    args = ap.parse_args()
    recordings = args.recordings
    if not recordings.is_dir():
        print(f"错误：找不到目录：{recordings}", file=sys.stderr)
        sys.exit(1)

    exclude = set(args.exclude_dir)
    videos = iter_videos(recordings, exclude_dirs=exclude)
    if args.sample is not None:
        n = min(max(0, args.sample), len(videos))
        if args.seed is not None:
            random.seed(args.seed)
        videos = random.sample(videos, n) if n else []

    for path in videos:
        print(path)


if __name__ == "__main__":
    main()
