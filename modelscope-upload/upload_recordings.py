#!/usr/bin/env python3
"""Upload session folders from the game-recorder recordings/ dir to ModelScope."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("MODELSCOPE_LOG_LEVEL", str(logging.ERROR))

DEFAULT_REPO_ID = "kusriri/world-game-data"
MODELSCOPE_TOKEN = "ms-54fac99a-5958-42d4-879d-b9445227cb51"
DEFAULT_SKIP_DIRS = frozenset({"overlay"})
# Session folders are stored under this path in the ModelScope dataset repo.
DATASET_RECORDINGS_DIR = "recordings"
DEFAULT_MIN_VIDEO_MB = 10


def _pack_root() -> Path:
    return Path(__file__).resolve().parent


def _game_recorder_root() -> Path:
    return _pack_root().parent


def iter_session_dirs(recordings: Path, *, skip_dirs: set[str]) -> list[Path]:
    if not recordings.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(recordings.iterdir()):
        if not path.is_dir():
            continue
        name = path.name
        if name in skip_dirs or name.startswith("."):
            continue
        out.append(path.resolve())
    return out


def _entry_name(item: dict) -> str | None:
    path = (item.get("Path") or item.get("Name") or "").strip().strip("/")
    if not path:
        return None
    return path.split("/")[0]


def verify_write_access(api, repo_id: str, token: str) -> None:
    import json

    import requests

    endpoint = getattr(api, "endpoint", None) or api._api._config.endpoint
    url = f"{endpoint.rstrip('/')}/api/v1/repos/datasets/{repo_id}/info/lfs/objects/batch"
    payload = {
        "operation": "upload",
        "objects": [{"oid": "0" * 64, "size": 1}],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Cookie": f"m_session_id={token}",
    }
    api_headers = getattr(api, "headers", None) or {}
    user_agent = api_headers.get("user-agent")
    if user_agent:
        headers["user-agent"] = user_agent
    response = requests.post(
        url,
        headers=headers,
        data=json.dumps(payload),
        timeout=60,
    )
    if response.status_code == 404:
        raise PermissionError(
            "Token 对该数据集没有写入权限。\n"
            "请用 kusriri 账号在 https://modelscope.cn/my/myaccesstoken 创建带写入权限的 Token。"
        )
    if response.status_code >= 400:
        try:
            body = response.json()
            msg = body.get("Message") or response.text
        except Exception:
            msg = response.text
        raise PermissionError(f"无法写入数据集（HTTP {response.status_code}）：{msg}")


def list_remote_session_folders(
    api, repo_id: str, token: str, *, dataset_dir: str = DATASET_RECORDINGS_DIR
) -> set[str]:
    from modelscope.utils.constant import DEFAULT_DATASET_REVISION

    remote: set[str] = set()
    page = 1
    page_size = 100
    root_path = f"/{dataset_dir.strip('/')}"
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
            name = _entry_name(item)
            if name:
                remote.add(name)
        if len(batch) < page_size:
            break
        page += 1
    return remote


def remote_path_for_session(session_name: str, *, dataset_dir: str = DATASET_RECORDINGS_DIR) -> str:
    return f"{dataset_dir.strip('/')}/{session_name}"


def session_mp4_total_bytes(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.glob("*.mp4") if path.is_file())


def format_mib(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.2f}MB"


@contextmanager
def _suppress_upload_report():
    stream = sys.stdout

    class FilteredStdout:
        __slots__ = ("_stream", "_in_report")

        def __init__(self, underlying):
            self._stream = underlying
            self._in_report = False

        def write(self, data: str) -> int:
            if self._in_report:
                stripped = data.strip()
                if stripped and len(stripped) >= 20 and set(stripped) == {"="}:
                    self._in_report = False
                return len(data)
            if "Upload Report" in data:
                self._in_report = True
                return len(data)
            return self._stream.write(data)

        def flush(self) -> None:
            self._stream.flush()

        def __getattr__(self, name: str):
            return getattr(self._stream, name)

    old = sys.stdout
    sys.stdout = FilteredStdout(old)
    try:
        yield
    finally:
        sys.stdout = old


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Upload session folders under recordings/ to ModelScope (skip if already on server)."
    )
    ap.add_argument(
        "recordings",
        type=Path,
        nargs="?",
        default=_game_recorder_root() / "recordings",
        help="recordings root (default: ../recordings relative to this pack)",
    )
    ap.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    ap.add_argument(
        "--dataset-dir",
        default=DATASET_RECORDINGS_DIR,
        help=f"remote subdirectory in the dataset repo (default: {DATASET_RECORDINGS_DIR})",
    )
    ap.add_argument("--skip-dir", action="append", default=[], metavar="NAME")
    ap.add_argument(
        "--min-video-mb",
        type=float,
        default=DEFAULT_MIN_VIDEO_MB,
        help=f"skip session when total mp4 size is below this threshold (default: {DEFAULT_MIN_VIDEO_MB})",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    recordings = args.recordings.resolve()
    if not recordings.is_dir():
        print(f"错误：找不到 recordings 目录：{recordings}", file=sys.stderr)
        sys.exit(1)

    skip_dirs = set(DEFAULT_SKIP_DIRS) | set(args.skip_dir)
    local_dirs = iter_session_dirs(recordings, skip_dirs=skip_dirs)
    if not local_dirs:
        print("没有可上传的 session 文件夹。")
        return

    token = MODELSCOPE_TOKEN

    try:
        from modelscope.hub.api import HubApi
    except ImportError:
        print("错误：未安装 modelscope，请先运行 install.bat。", file=sys.stderr)
        sys.exit(1)

    api = HubApi()
    api.login(token)

    try:
        verify_write_access(api, args.repo_id, token)
        remote_folders = list_remote_session_folders(
            api, args.repo_id, token, dataset_dir=args.dataset_dir
        )
    except PermissionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)

    to_upload = [d for d in local_dirs if d.name not in remote_folders]
    skipped_remote = [d for d in local_dirs if d.name in remote_folders]

    min_video_bytes = max(0, int(args.min_video_mb * 1024 * 1024))
    too_small: list[tuple[Path, int]] = []
    upload_candidates: list[Path] = []
    for folder in to_upload:
        mp4_bytes = session_mp4_total_bytes(folder)
        if mp4_bytes < min_video_bytes:
            too_small.append((folder, mp4_bytes))
        else:
            upload_candidates.append(folder)
    to_upload = upload_candidates

    print(
        f"{args.repo_id}/{args.dataset_dir}  "
        f"上传 {len(to_upload)}  "
        f"跳过已存在 {len(skipped_remote)}  "
        f"跳过过小 {len(too_small)}"
    )
    if skipped_remote:
        print("跳过(已存在):", ", ".join(d.name for d in skipped_remote))
    if too_small:
        threshold = format_mib(min_video_bytes)
        for folder, mp4_bytes in too_small:
            if mp4_bytes <= 0:
                print(f"跳过(无 mp4): {folder.name}")
            else:
                print(
                    f"跳过(视频过小 {format_mib(mp4_bytes)} < {threshold}): {folder.name}"
                )

    if not to_upload:
        return

    if args.dry_run:
        print("待传:", ", ".join(d.name for d in to_upload))
        return

    failed: list[str] = []
    for i, folder in enumerate(to_upload, start=1):
        name = folder.name
        remote_path = remote_path_for_session(name, dataset_dir=args.dataset_dir)
        print(f"[{i}/{len(to_upload)}] {remote_path}")
        try:
            with _suppress_upload_report():
                api.upload_folder(
                    repo_id=args.repo_id,
                    folder_path=folder,
                    path_in_repo=remote_path,
                    repo_type="dataset",
                    token=token,
                    commit_message=f"upload session {name}",
                )
        except Exception as exc:
            print(f"  失败: {exc}", file=sys.stderr)
            failed.append(name)

    if failed:
        print(f"完成，{len(failed)} 个失败: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)
    print(f"完成，已上传 {len(to_upload)} 个文件夹。")


if __name__ == "__main__":
    main()
