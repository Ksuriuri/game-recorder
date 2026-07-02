#!/usr/bin/env python3
"""Scan ModelScope recordings/ and remove session folders with small mp4 files."""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("MODELSCOPE_LOG_LEVEL", str(logging.ERROR))
os.environ.setdefault("TQDM_DISABLE", "1")

DEFAULT_REPO_ID = "kusriri/world-game-data"
MODELSCOPE_TOKEN = "ms-54fac99a-5958-42d4-879d-b9445227cb51"
DATASET_RECORDINGS_DIR = "recordings"
DEFAULT_MIN_VIDEO_MB = 10.0
DATE_IN_SESSION_RE = re.compile(r"_session_(\d{8})_")

_print_lock = threading.Lock()


def _pack_root() -> Path:
    return Path(__file__).resolve().parent


def _cache_root() -> Path:
    return _pack_root() / ".cache"


def _configure_storage() -> None:
    cache_root = _cache_root()
    tmp_root = cache_root / "tmp"
    cache_root.mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    os.environ["MODELSCOPE_CACHE"] = str(cache_root)
    os.environ["TMP"] = str(tmp_root)
    os.environ["TEMP"] = str(tmp_root)


_configure_storage()


def status(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


@dataclass(frozen=True)
class SessionRecord:
    folder_name: str
    mp4_bytes: int
    date: str | None
    recorder_id: str | None


def format_mib(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.2f}MB"


def parse_session_folder(name: str) -> tuple[str, str] | None:
    match = DATE_IN_SESSION_RE.search(name)
    if not match:
        return None
    return name.split("_", 1)[0], match.group(1)


def list_remote_session_folders(api, repo_id: str, token: str) -> list[str]:
    from modelscope.utils.constant import DEFAULT_DATASET_REVISION

    root_path = f"/{DATASET_RECORDINGS_DIR.strip('/')}"
    names: list[str] = []
    page = 1
    page_size = 100
    while True:
        batch = api.get_dataset_files(
            repo_id=repo_id,
            revision=DEFAULT_DATASET_REVISION,
            root_path=root_path,
            recursive=False,
            page_number=page,
            page_size=page_size,
            token=token,
        )
        if not batch:
            break
        for item in batch:
            if item.get("Type") != "tree":
                continue
            path = (item.get("Path") or item.get("Name") or "").strip("/")
            if path:
                names.append(path.split("/")[-1])
        if len(batch) < page_size:
            break
        page += 1
    return sorted(names)


def list_remote_session_mp4_sizes(api, repo_id: str, token: str) -> dict[str, int]:
    from modelscope.utils.constant import DEFAULT_DATASET_REVISION

    root_path = f"/{DATASET_RECORDINGS_DIR.strip('/')}"
    sizes: dict[str, int] = defaultdict(int)
    page = 1
    page_size = 100
    dataset_dir = DATASET_RECORDINGS_DIR.strip("/")

    while True:
        batch = api.get_dataset_files(
            repo_id=repo_id,
            revision=DEFAULT_DATASET_REVISION,
            root_path=root_path,
            recursive=True,
            page_number=page,
            page_size=page_size,
            token=token,
        )
        if not batch:
            break
        for item in batch:
            if item.get("Type") != "blob":
                continue
            path = (item.get("Path") or item.get("Name") or "").strip("/")
            if not path.lower().endswith(".mp4"):
                continue
            parts = path.split("/")
            if len(parts) < 3 or parts[0] != dataset_dir:
                continue
            sizes[parts[1]] += int(item.get("Size") or 0)
        status(f"  已扫描远程文件页 {page}（累计 {len(sizes)} 个 session 有 mp4）")
        if len(batch) < page_size:
            break
        page += 1
    return dict(sizes)


def build_session_records(
    folder_names: list[str],
    mp4_sizes: dict[str, int],
) -> list[SessionRecord]:
    records: list[SessionRecord] = []
    for name in folder_names:
        parsed = parse_session_folder(name)
        recorder_id, date = parsed if parsed else (None, None)
        records.append(
            SessionRecord(
                folder_name=name,
                mp4_bytes=mp4_sizes.get(name, 0),
                date=date,
                recorder_id=recorder_id,
            )
        )
    return records


def repo_rel_paths(folder_name: str) -> list[Path]:
    return [Path(DATASET_RECORDINGS_DIR) / folder_name, Path(folder_name)]


def delete_sessions_via_git(
    repo_id: str,
    token: str,
    folder_names: list[str],
    *,
    min_video_mb: float,
) -> None:
    if not folder_names:
        status("没有需要删除的 session。")
        return

    if not shutil.which("git"):
        raise RuntimeError("未找到 git，请先安装 Git for Windows。")

    with TemporaryDirectory(prefix="ms-cleanup-") as tmp:
        repo_dir = Path(tmp) / "repo"
        clone_url = f"http://oauth2:{token}@www.modelscope.cn/datasets/{repo_id}.git"
        env = os.environ.copy()
        env["GIT_LFS_SKIP_SMUDGE"] = "1"

        status("正在克隆数据集仓库（跳过 LFS 下载）...")
        subprocess.run(["git", "lfs", "install"], check=True, capture_output=True)
        subprocess.run(["git", "clone", clone_url, str(repo_dir)], check=True, env=env)

        removed: list[str] = []
        for name in folder_names:
            target = None
            for rel in repo_rel_paths(name):
                path = repo_dir / rel
                if path.is_dir():
                    target = path
                    break
            if target is None:
                status(f"  跳过（本地未找到）: {name}")
                continue
            shutil.rmtree(target)
            removed.append(name)
            status(f"  已移除: {name}")

        if not removed:
            raise RuntimeError("克隆的仓库中未找到任何待删 session 文件夹。")

        subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
        diff = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        if not diff.stdout.strip():
            status("没有可提交的删除变更。")
            return

        commit_msg = f"remove {len(removed)} sessions with mp4 total size below {min_video_mb:g}MB"
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", commit_msg], check=True)
        status("正在推送到 ModelScope ...")
        subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "master"], check=True)
        status(f"已删除 {len(removed)} 个 session 文件夹。")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="扫描 ModelScope recordings/，删除 mp4 总大小低于阈值的 session 文件夹。"
    )
    ap.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    ap.add_argument(
        "--min-video-mb",
        type=float,
        default=DEFAULT_MIN_VIDEO_MB,
        help=f"mp4 总大小低于该值（MB）的 session 将被删除（默认: {DEFAULT_MIN_VIDEO_MB:g}）",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览待删除列表，不实际删除（默认会直接删除）",
    )
    args = ap.parse_args()

    if args.min_video_mb < 0:
        print("错误：--min-video-mb 不能为负数。", file=sys.stderr)
        sys.exit(1)

    min_video_bytes = max(0, int(args.min_video_mb * 1024 * 1024))
    threshold_label = format_mib(min_video_bytes)

    token = MODELSCOPE_TOKEN
    if not token:
        print("错误：未配置 MODELSCOPE_TOKEN。", file=sys.stderr)
        sys.exit(1)
    os.environ["MODELSCOPE_API_TOKEN"] = token

    try:
        from modelscope.hub.api import HubApi
    except ImportError:
        print("错误：未安装 modelscope，请先运行 install.bat。", file=sys.stderr)
        sys.exit(1)

    status(f"数据集: {args.repo_id}/{DATASET_RECORDINGS_DIR}")
    status(f"大小阈值: mp4 总大小 < {threshold_label}")
    status(f"缓存目录: {_cache_root()}")

    api = HubApi(token=token)
    status("正在列出远程 session 文件夹 ...")
    try:
        folder_names = list_remote_session_folders(api, args.repo_id, token)
    except Exception as exc:
        print(f"错误：无法列出远程 session：{exc}", file=sys.stderr)
        sys.exit(1)

    if not folder_names:
        status("recordings/ 下没有 session 文件夹。")
        return

    status(f"共 {len(folder_names)} 个 session，正在统计远程 mp4 大小 ...")
    try:
        mp4_sizes = list_remote_session_mp4_sizes(api, args.repo_id, token)
    except Exception as exc:
        print(f"错误：无法统计远程 mp4 大小：{exc}", file=sys.stderr)
        sys.exit(1)

    records = build_session_records(folder_names, mp4_sizes)
    small = [r for r in records if r.mp4_bytes < min_video_bytes]
    total_mp4_bytes = sum(r.mp4_bytes for r in records)
    small_mp4_bytes = sum(r.mp4_bytes for r in small)

    print()
    status(f"扫描完成: {len(records)} 个 session")
    status(f"全部 session mp4 总大小: {format_mib(total_mp4_bytes)}")
    status(
        f"小于 {threshold_label}: {len(small)} 个，"
        f"合计 {format_mib(small_mp4_bytes)}"
    )

    if small:
        print()
        for rec in sorted(small, key=lambda r: r.mp4_bytes):
            if rec.mp4_bytes <= 0:
                status(f"  - {rec.folder_name}  无 mp4")
            else:
                status(f"  - {rec.folder_name}  {format_mib(rec.mp4_bytes)}")
    else:
        status("没有需要删除的 session。")
        return

    print()
    if args.dry_run:
        status("dry-run：以上文件夹将被删除。确认无误后请去掉 --dry-run 重新运行。")
        return

    status(f"开始删除 {len(small)} 个 session ...")
    try:
        delete_sessions_via_git(
            args.repo_id,
            token,
            [r.folder_name for r in small],
            min_video_mb=args.min_video_mb,
        )
    except subprocess.CalledProcessError as exc:
        print(f"错误：Git 操作失败（exit {exc.returncode}）。", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
