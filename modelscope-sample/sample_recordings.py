#!/usr/bin/env python3
"""Sample session videos from ModelScope recordings/ for acceptance evaluation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("MODELSCOPE_LOG_LEVEL", str(logging.ERROR))
os.environ.setdefault("TQDM_DISABLE", "1")

DEFAULT_REPO_ID = "kusriri/world-game-data"
MODELSCOPE_TOKEN = "ms-54fac99a-5958-42d4-879d-b9445227cb51"
DATASET_RECORDINGS_DIR = "recordings"
DEFAULT_SAMPLES_PER_RECORDER = 5
MIN_SAMPLES_PER_RECORDER = 1
MAX_SAMPLES_PER_RECORDER = 20
DATE_IN_SESSION_RE = re.compile(r"_session_(\d{8})_")
DEFAULT_META_WORKERS = min(32, max(8, (os.cpu_count() or 4) * 4))
DEFAULT_DOWNLOAD_WORKERS = 6

_print_lock = threading.Lock()


def _pack_root() -> Path:
    return Path(__file__).resolve().parent


def _default_output_root() -> Path:
    return _pack_root() / "data"


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


def clear_cache() -> None:
    cache_root = _cache_root().resolve()
    pack_root = _pack_root().resolve()
    if cache_root.parent != pack_root or cache_root.name != ".cache":
        raise RuntimeError(f"拒绝清理异常缓存路径: {cache_root}")
    if not cache_root.exists():
        return
    shutil.rmtree(cache_root, ignore_errors=True)
    _configure_storage()


def status(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


@dataclass(frozen=True)
class SessionInfo:
    folder_name: str
    recorder_id: str
    date: str
    duration_s: float
    video_paths: tuple[str, ...]


@dataclass(frozen=True)
class DownloadTask:
    remote_path: str
    local_target: Path
    label: str


def parse_session_folder(name: str) -> tuple[str, str] | None:
    match = DATE_IN_SESSION_RE.search(name)
    if not match:
        return None
    return name.split("_", 1)[0], match.group(1)


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def format_size(num_bytes: int | float | None) -> str:
    if not num_bytes or num_bytes <= 0:
        return "大小未知"
    size = float(num_bytes)
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 ** 3):.2f} GB"
    if size >= 1024 * 1024:
        return f"{size / (1024 ** 2):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{int(size)} B"


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
            if not path:
                continue
            names.append(path.split("/")[-1])
        if len(batch) < page_size:
            break
        page += 1
    return sorted(names)


def load_session_meta(repo_id: str, token: str, folder_name: str) -> dict:
    from modelscope.hub.file_download import dataset_file_download
    from modelscope.utils.constant import DEFAULT_DATASET_REVISION

    remote_meta = f"{DATASET_RECORDINGS_DIR}/{folder_name}/meta.json"
    cached = dataset_file_download(
        dataset_id=repo_id,
        file_path=remote_meta,
        revision=DEFAULT_DATASET_REVISION,
        token=token,
        cache_dir=str(_cache_root()),
    )
    with open(cached, encoding="utf-8") as f:
        return json.load(f)


def session_from_meta(folder_name: str, recorder_id: str, date: str, meta: dict) -> SessionInfo:
    duration_s = float(meta.get("duration_s") or 0.0)
    if duration_s <= 0:
        fps = float(meta.get("fps") or 30)
        duration_s = sum(float(seg.get("frame_count") or 0) for seg in meta.get("segments") or []) / fps

    videos: list[str] = []
    for seg in meta.get("segments") or []:
        video = (seg.get("video") or "").strip()
        if video:
            videos.append(video)
    if not videos:
        raise ValueError("meta.json 中未找到视频文件")

    return SessionInfo(
        folder_name=folder_name,
        recorder_id=recorder_id,
        date=date,
        duration_s=duration_s,
        video_paths=tuple(videos),
    )


def fetch_session_info(
    folder_name: str,
    recorder_id: str,
    folder_date: str,
    repo_id: str,
    token: str,
) -> SessionInfo:
    meta = load_session_meta(repo_id, token, folder_name)
    return session_from_meta(folder_name, recorder_id, folder_date, meta)


def load_sessions_parallel(
    dated_folders: list[tuple[str, str, str]],
    repo_id: str,
    token: str,
    *,
    workers: int,
) -> tuple[list[SessionInfo], list[str]]:
    total = len(dated_folders)
    sessions: list[SessionInfo] = []
    failed: list[str] = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(
                fetch_session_info,
                folder_name,
                recorder_id,
                folder_date,
                repo_id,
                token,
            ): folder_name
            for folder_name, recorder_id, folder_date in dated_folders
        }
        for future in as_completed(future_map):
            folder_name = future_map[future]
            done += 1
            try:
                sessions.append(future.result())
                status(f"  meta.json [{done}/{total}] 完成 {folder_name}")
            except Exception as exc:
                failed.append(f"{folder_name}: {exc}")
                status(f"  meta.json [{done}/{total}] 失败 {folder_name}")

    sessions.sort(key=lambda s: s.folder_name)
    return sessions, failed


def sample_sessions(sessions: list[SessionInfo], count: int = DEFAULT_SAMPLES_PER_RECORDER) -> list[SessionInfo]:
    if len(sessions) <= count:
        return list(sessions)

    ranked = sorted(sessions, key=lambda s: s.duration_s, reverse=True)
    if count <= 1:
        return [ranked[len(ranked) // 2]]

    n = len(ranked)
    indices = [round(i * (n - 1) / (count - 1)) for i in range(count)]

    picked: list[SessionInfo] = []
    seen: set[int] = set()
    for idx in indices:
        if idx in seen:
            continue
        seen.add(idx)
        picked.append(ranked[idx])
    return picked


def build_download_tasks(sampled_sessions: list[SessionInfo], output_dir: Path) -> list[DownloadTask]:
    tasks: list[DownloadTask] = []
    for session in sampled_sessions:
        session_dir = output_dir / session.recorder_id / session.folder_name
        filenames = ["meta.json", *session.video_paths]
        for filename in filenames:
            remote_path = f"{DATASET_RECORDINGS_DIR}/{session.folder_name}/{filename}"
            tasks.append(
                DownloadTask(
                    remote_path=remote_path,
                    local_target=session_dir / filename,
                    label=f"{session.folder_name}/{filename}",
                )
            )
    return tasks


def download_one_file(repo_id: str, token: str, task: DownloadTask) -> DownloadTask:
    from modelscope.hub.file_download import dataset_file_download
    from modelscope.utils.constant import DEFAULT_DATASET_REVISION

    session_dir = task.local_target.parent
    session_dir.mkdir(parents=True, exist_ok=True)
    cached = dataset_file_download(
        dataset_id=repo_id,
        file_path=task.remote_path,
        revision=DEFAULT_DATASET_REVISION,
        token=token,
        cache_dir=str(_cache_root()),
    )
    cached_path = Path(cached)
    if not cached_path.is_file():
        raise FileNotFoundError(f"下载失败: {task.remote_path}")
    if cached_path.resolve() != task.local_target.resolve():
        shutil.copy2(cached_path, task.local_target)

    nested = session_dir / DATASET_RECORDINGS_DIR
    if nested.is_dir():
        shutil.rmtree(nested, ignore_errors=True)
    return task


def download_files_parallel(
    tasks: list[DownloadTask],
    repo_id: str,
    token: str,
    *,
    workers: int,
) -> tuple[list[DownloadTask], list[str]]:
    if not tasks:
        return [], []

    total = len(tasks)
    completed: list[DownloadTask] = []
    failed: list[str] = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(download_one_file, repo_id, token, task): task
            for task in tasks
        }
        for future in as_completed(future_map):
            task = future_map[future]
            done += 1
            try:
                finished = future.result()
                completed.append(finished)
                status(
                    f"  下载 [{done}/{total}] 完成 {finished.label} "
                    f"({format_size(finished.local_target.stat().st_size)})"
                )
            except Exception as exc:
                failed.append(f"{task.label}: {exc}")
                status(f"  下载 [{done}/{total}] 失败 {task.label}")

    return completed, failed


def prompt_date() -> str:
    while True:
        value = input("请输入日期 (YYYYMMDD): ").strip()
        if re.fullmatch(r"\d{8}", value):
            return value
        print("格式错误，请输入 8 位日期，例如 20260702。", file=sys.stderr)


def prompt_sample_count() -> int:
    while True:
        prompt = (
            f"请输入每位录制人抽样数量 "
            f"({MIN_SAMPLES_PER_RECORDER}-{MAX_SAMPLES_PER_RECORDER}，"
            f"回车默认 {DEFAULT_SAMPLES_PER_RECORDER}): "
        )
        value = input(prompt).strip()
        if not value:
            return DEFAULT_SAMPLES_PER_RECORDER
        if not value.isdigit():
            print("格式错误，请输入整数。", file=sys.stderr)
            continue
        count = int(value)
        if MIN_SAMPLES_PER_RECORDER <= count <= MAX_SAMPLES_PER_RECORDER:
            return count
        print(
            f"请输入 {MIN_SAMPLES_PER_RECORDER}-{MAX_SAMPLES_PER_RECORDER} 之间的整数。",
            file=sys.stderr,
        )


def resolve_sample_count(per_recorder: int | None) -> int:
    if per_recorder is None:
        return prompt_sample_count()
    if not MIN_SAMPLES_PER_RECORDER <= per_recorder <= MAX_SAMPLES_PER_RECORDER:
        print(
            f"错误：抽样数量须在 {MIN_SAMPLES_PER_RECORDER}-{MAX_SAMPLES_PER_RECORDER} 之间，"
            f"当前为 {per_recorder}。",
            file=sys.stderr,
        )
        sys.exit(1)
    return per_recorder


def main() -> None:
    ap = argparse.ArgumentParser(
        description="从 ModelScope 数据集 recordings/ 按日期、录制人抽样下载视频用于验收评测。"
    )
    ap.add_argument("date", nargs="?", help="日期 YYYYMMDD；省略则交互输入")
    ap.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    ap.add_argument(
        "--output",
        type=Path,
        default=_default_output_root(),
        help="下载输出根目录（默认: 本文件夹下的 data/）",
    )
    ap.add_argument(
        "--per-recorder",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"每位录制人抽样 session 数（{MIN_SAMPLES_PER_RECORDER}-{MAX_SAMPLES_PER_RECORDER}，"
            f"省略则交互输入，默认 {DEFAULT_SAMPLES_PER_RECORDER}）"
        ),
    )
    ap.add_argument(
        "--meta-workers",
        type=int,
        default=DEFAULT_META_WORKERS,
        help=f"读取 meta.json 的并行线程数（默认: {DEFAULT_META_WORKERS}）",
    )
    ap.add_argument(
        "--download-workers",
        type=int,
        default=DEFAULT_DOWNLOAD_WORKERS,
        help=f"下载视频文件的并行线程数（默认: {DEFAULT_DOWNLOAD_WORKERS}）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只显示抽样计划，不下载")
    args = ap.parse_args()

    if args.meta_workers < 1 or args.download_workers < 1:
        print("错误：--meta-workers 和 --download-workers 必须 >= 1。", file=sys.stderr)
        sys.exit(1)

    date = args.date or prompt_date()
    if not re.fullmatch(r"\d{8}", date):
        print(f"错误：无效日期 {date!r}，应为 YYYYMMDD。", file=sys.stderr)
        sys.exit(1)

    per_recorder = resolve_sample_count(args.per_recorder)

    try:
        from modelscope.hub.api import HubApi
    except ImportError:
        print("错误：未安装 modelscope，请先运行 install.bat。", file=sys.stderr)
        sys.exit(1)

    token = MODELSCOPE_TOKEN
    if not token:
        print("错误：未配置 MODELSCOPE_TOKEN。", file=sys.stderr)
        sys.exit(1)
    os.environ["MODELSCOPE_API_TOKEN"] = token
    meta_workers = args.meta_workers
    download_workers = args.download_workers

    status(f"缓存目录: {_cache_root()}")
    status(f"输出目录: {args.output.resolve()}")

    status("正在连接 ModelScope 数据集 ...")
    api = HubApi(token=token)
    status("连接成功。")

    status("正在列出远程 session 文件夹 ...")
    try:
        remote_folders = list_remote_session_folders(api, args.repo_id, token)
    except Exception as exc:
        msg = str(exc)
        if "Authentication" in type(exc).__name__ or "E3001" in msg or "token" in msg.lower():
            print(
                "错误：ModelScope Token 无效或已被服务器拒绝。\n"
                "请用数据集所有者账号在 https://modelscope.cn/my/myaccesstoken 重新生成 Token，\n"
                "更新 sample_recordings.py 里的 MODELSCOPE_TOKEN 后重新打包。",
                file=sys.stderr,
            )
        else:
            print(f"错误：无法列出远程 session：{exc}", file=sys.stderr)
        sys.exit(1)
    status(f"远程共有 {len(remote_folders)} 个 session 文件夹。")

    dated_folders: list[tuple[str, str, str]] = []
    for name in remote_folders:
        parsed = parse_session_folder(name)
        if not parsed:
            continue
        recorder_id, folder_date = parsed
        if folder_date == date:
            dated_folders.append((name, recorder_id, folder_date))

    if not dated_folders:
        status(f"未找到日期 {date} 的 session 文件夹。")
        available: dict[str, int] = {}
        for name in remote_folders:
            parsed = parse_session_folder(name)
            if parsed:
                available[parsed[1]] = available.get(parsed[1], 0) + 1
        if available:
            status("当前数据集可用日期：")
            for day in sorted(available):
                status(f"  - {day}: {available[day]} 个 session")
        return

    total_meta = len(dated_folders)
    status(f"找到 {total_meta} 个 session（日期 {date}），并行读取 meta.json（{meta_workers} 线程）...")

    sessions, failed = load_sessions_parallel(
        dated_folders,
        args.repo_id,
        token,
        workers=meta_workers,
    )

    if failed:
        print("以下 session 读取 meta.json 失败：", file=sys.stderr)
        for item in failed:
            print(f"  - {item}", file=sys.stderr)

    if not sessions:
        print("没有可用的 session 元数据。", file=sys.stderr)
        sys.exit(1)

    status(f"meta.json 读取完成，可用 {len(sessions)}/{total_meta} 个 session。")

    by_recorder: dict[str, list[SessionInfo]] = {}
    for session in sessions:
        by_recorder.setdefault(session.recorder_id, []).append(session)

    total_duration_s = sum(s.duration_s for s in sessions)
    output_dir = args.output / date

    print()
    status(f"日期: {date}")
    status(f"每位录制人抽样: {per_recorder} 条")
    status(f"数据集: {args.repo_id}/{DATASET_RECORDINGS_DIR}")
    status(f"session 总数: {len(sessions)}")
    status(f"录制人数: {len(by_recorder)}")
    status(f"当日视频总时长: {format_duration(total_duration_s)} ({total_duration_s:.1f}s)")
    print()

    sampled_sessions: list[SessionInfo] = []
    for recorder_id in sorted(by_recorder):
        recorder_sessions = by_recorder[recorder_id]
        picked = sample_sessions(recorder_sessions, count=per_recorder)
        sampled_sessions.extend(picked)

        recorder_total = sum(s.duration_s for s in recorder_sessions)
        status(
            f"[{recorder_id}] 共 {len(recorder_sessions)} 条，"
            f"总时长 {format_duration(recorder_total)}，抽样 {len(picked)} 条："
        )
        for session in sorted(picked, key=lambda s: s.duration_s, reverse=True):
            status(
                f"  - {session.folder_name}  "
                f"{format_duration(session.duration_s)}  "
                f"({len(session.video_paths)} 个视频文件)"
            )

    print()
    download_tasks = build_download_tasks(sampled_sessions, output_dir)
    if args.dry_run:
        status(
            f"dry-run：将下载 {len(sampled_sessions)} 个 session、"
            f"{len(download_tasks)} 个文件到 {output_dir}"
        )
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    status(
        f"开始并行下载 {len(download_tasks)} 个文件（{len(sampled_sessions)} 个 session，"
        f"{download_workers} 线程）到 {output_dir}"
    )
    _, download_failed = download_files_parallel(
        download_tasks,
        args.repo_id,
        token,
        workers=download_workers,
    )

    if download_failed:
        print("以下文件下载失败：", file=sys.stderr)
        for item in download_failed:
            print(f"  - {item}", file=sys.stderr)
        print(f"完成，{len(download_failed)} 个文件失败。", file=sys.stderr)
        sys.exit(1)

    status(f"完成，已下载 {len(sampled_sessions)} 个 session（{len(download_tasks)} 个文件）。")
    status("正在清理下载缓存 ...")
    clear_cache()
    status("缓存已清理。")


if __name__ == "__main__":
    main()
