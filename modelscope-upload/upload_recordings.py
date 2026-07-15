#!/usr/bin/env python3
"""Upload session folders from the game-recorder recordings/ dir to ModelScope."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Callable, TypeVar

os.environ.setdefault("MODELSCOPE_LOG_LEVEL", str(logging.ERROR))

DEFAULT_REPO_ID = "kusriri/world-game-data"
MODELSCOPE_TOKEN = "ms-54fac99a-5958-42d4-879d-b9445227cb51"
DEFAULT_SKIP_DIRS = frozenset({"overlay"})
# Session folders are stored under this path in the ModelScope dataset repo.
DATASET_RECORDINGS_DIR = "recordings"
DEFAULT_MIN_VIDEO_MB = 10
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 5.0
UPLOAD_INTERNAL_FILES = frozenset({".ms_upload_cache", ".ms_upload_progress"})
UPLOAD_IGNORED_DIRS = frozenset({".git", ".cache"})

T = TypeVar("T")


@dataclass(frozen=True)
class LocalFile:
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class RemoteFile:
    size: int
    sha256: str
    in_check: bool


@dataclass(frozen=True)
class ManifestCheck:
    complete: bool
    detail: str


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
        # Real sessions always have meta.json (skip unrelated folders).
        if not (path / "meta.json").is_file():
            continue
        out.append(path.resolve())
    return out


def _entry_name(item: dict) -> str | None:
    path = (item.get("Path") or item.get("Name") or "").strip().strip("/")
    if not path:
        return None
    return path.split("/")[-1]


def call_with_retries(
    operation: Callable[[], T],
    *,
    description: str,
    max_attempts: int,
    retry_delay: float,
) -> T:
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except PermissionError:
            raise
        except Exception as exc:
            if attempt >= max_attempts:
                raise
            delay = retry_delay * (2 ** (attempt - 1))
            print(
                f"  {description}失败（{attempt}/{max_attempts}）：{exc}\n"
                f"  {delay:g} 秒后重试...",
                file=sys.stderr,
                flush=True,
            )
            sleep(delay)
    raise RuntimeError(f"{description}失败")


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
    if response.status_code in {401, 403, 404}:
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
        raise RuntimeError(f"无法写入数据集（HTTP {response.status_code}）：{msg}")


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


def list_remote_session_files(
    api,
    repo_id: str,
    token: str,
    session_name: str,
    *,
    dataset_dir: str = DATASET_RECORDINGS_DIR,
) -> dict[str, RemoteFile]:
    from modelscope.utils.constant import DEFAULT_DATASET_REVISION

    dataset_dir = dataset_dir.strip("/")
    session_root = f"{dataset_dir}/{session_name}"
    root_path = f"/{session_root}"
    prefix = f"{session_root}/"
    remote: dict[str, RemoteFile] = {}
    page = 1
    page_size = 100

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
            path = (item.get("Path") or item.get("Name") or "").strip().strip("/")
            if not path.startswith(prefix):
                continue
            relative_path = path[len(prefix) :]
            if not relative_path:
                continue
            remote[relative_path] = RemoteFile(
                size=int(item.get("Size") or 0),
                sha256=str(item.get("Sha256") or "").strip().lower(),
                in_check=bool(item.get("InCheck")),
            )
        if len(batch) < page_size:
            break
        page += 1
    return remote


def local_session_manifest(folder: Path) -> dict[str, LocalFile]:
    manifest: dict[str, LocalFile] = {}
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(folder)
        if path.name in UPLOAD_INTERNAL_FILES:
            continue
        if any(part in UPLOAD_IGNORED_DIRS for part in relative.parts):
            continue
        stat = path.stat()
        manifest[relative.as_posix()] = LocalFile(
            path=path,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
    return manifest


def _file_sha256(
    local_file: LocalFile,
    cache: dict[tuple[Path, int, int], str],
) -> str:
    key = (local_file.path, local_file.size, local_file.mtime_ns)
    cached = cache.get(key)
    if cached is not None:
        return cached

    digest = hashlib.sha256()
    with local_file.path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    result = digest.hexdigest()
    cache[key] = result
    return result


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _short_path_list(paths: list[str], *, limit: int = 3) -> str:
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        shown += f" 等 {len(paths)} 个"
    return shown


def check_remote_manifest(
    local: dict[str, LocalFile],
    remote: dict[str, RemoteFile],
    *,
    verify_hashes: bool,
    hash_cache: dict[tuple[Path, int, int], str],
) -> ManifestCheck:
    if not local:
        return ManifestCheck(False, "本地文件夹为空")

    missing = sorted(set(local) - set(remote))
    pending: list[str] = []
    size_mismatches: list[str] = []
    unavailable_hashes: list[str] = []
    hash_mismatches: list[str] = []

    for relative_path, local_file in local.items():
        remote_file = remote.get(relative_path)
        if remote_file is None:
            continue
        if remote_file.in_check:
            pending.append(relative_path)
            continue
        if local_file.size != remote_file.size:
            size_mismatches.append(relative_path)
            continue
        if verify_hashes and not _is_sha256(remote_file.sha256):
            unavailable_hashes.append(relative_path)
            continue
        if verify_hashes and _file_sha256(local_file, hash_cache) != remote_file.sha256:
            hash_mismatches.append(relative_path)

    problems: list[str] = []
    if missing:
        problems.append(f"缺少文件: {_short_path_list(missing)}")
    if pending:
        problems.append(f"服务器仍在校验: {_short_path_list(sorted(pending))}")
    if size_mismatches:
        problems.append(f"大小不一致: {_short_path_list(sorted(size_mismatches))}")
    if unavailable_hashes:
        problems.append(
            f"服务器未提供 SHA-256: {_short_path_list(sorted(unavailable_hashes))}"
        )
    if hash_mismatches:
        problems.append(f"SHA-256 不一致: {_short_path_list(sorted(hash_mismatches))}")
    if problems:
        return ManifestCheck(False, "；".join(problems))

    hash_note = "、SHA-256" if verify_hashes else ""
    return ManifestCheck(True, f"{len(local)} 个文件的名称、大小{hash_note}一致")


def session_mp4_total_bytes(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.glob("*.mp4") if path.is_file())


def format_mib(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.2f}MB"


def upload_session_with_retries(
    api,
    *,
    repo_id: str,
    token: str,
    dataset_dir: str,
    folder: Path,
    max_attempts: int,
    retry_delay: float,
    verify_hashes: bool,
    hash_cache: dict[tuple[Path, int, int], str],
) -> tuple[bool, str]:
    name = folder.name
    remote_path = remote_path_for_session(name, dataset_dir=dataset_dir)
    last_detail = "未知错误"

    for attempt in range(1, max_attempts + 1):
        upload_error: Exception | None = None
        if attempt > 1:
            print(f"  开始第 {attempt}/{max_attempts} 次 session 上传尝试...", flush=True)
        try:
            with _suppress_upload_report():
                api.upload_folder(
                    repo_id=repo_id,
                    folder_path=folder,
                    path_in_repo=remote_path,
                    repo_type="dataset",
                    token=token,
                    commit_message=f"upload session {name}",
                    # The remote manifest is authoritative. A stale local SDK
                    # cache must not hide files missing from the repository.
                    use_cache=False,
                )
        except Exception as exc:
            upload_error = exc

        try:
            remote_files = call_with_retries(
                lambda: list_remote_session_files(
                    api,
                    repo_id,
                    token,
                    name,
                    dataset_dir=dataset_dir,
                ),
                description=f"校验远程 session {name}",
                max_attempts=max_attempts,
                retry_delay=retry_delay,
            )
            local_files = local_session_manifest(folder)
            check = check_remote_manifest(
                local_files,
                remote_files,
                verify_hashes=verify_hashes,
                hash_cache=hash_cache,
            )
        except PermissionError:
            raise
        except Exception as exc:
            check = ManifestCheck(False, f"无法读取远程清单: {exc}")

        if check.complete:
            if upload_error is not None:
                print(
                    f"  上传调用虽报错，但远程校验已完整，按成功处理：{upload_error}",
                    flush=True,
                )
            else:
                print(f"  远程校验通过：{check.detail}", flush=True)
            return True, check.detail

        if upload_error is not None:
            last_detail = f"{upload_error}；远程校验未通过（{check.detail}）"
        else:
            last_detail = f"上传结束但远程校验未通过（{check.detail}）"
        print(
            f"  第 {attempt}/{max_attempts} 次失败：{last_detail}",
            file=sys.stderr,
            flush=True,
        )
        if attempt < max_attempts:
            delay = retry_delay * (2 ** (attempt - 1))
            print(f"  {delay:g} 秒后重试整个 session...", flush=True)
            sleep(delay)

    return False, last_detail


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
        description="Upload recordings/ sessions to ModelScope and verify completeness before skipping."
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
    ap.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"maximum attempts for network operations and each session (default: {DEFAULT_MAX_ATTEMPTS})",
    )
    ap.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY_SECONDS,
        help=f"initial retry delay in seconds; doubles each attempt (default: {DEFAULT_RETRY_DELAY_SECONDS:g})",
    )
    ap.add_argument(
        "--no-verify-hash",
        action="store_true",
        help="only compare remote file names and sizes; do not hash local files",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    recordings = args.recordings.resolve()
    if not recordings.is_dir():
        print(f"错误：找不到 recordings 目录：{recordings}", file=sys.stderr)
        sys.exit(1)
    if args.max_attempts < 1:
        print("错误：--max-attempts 必须至少为 1。", file=sys.stderr)
        sys.exit(1)
    if args.retry_delay < 0:
        print("错误：--retry-delay 不能小于 0。", file=sys.stderr)
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
        call_with_retries(
            lambda: verify_write_access(api, args.repo_id, token),
            description="检查数据集写入权限",
            max_attempts=args.max_attempts,
            retry_delay=args.retry_delay,
        )
        remote_folders = call_with_retries(
            lambda: list_remote_session_folders(
                api, args.repo_id, token, dataset_dir=args.dataset_dir
            ),
            description="读取远程 session 列表",
            max_attempts=args.max_attempts,
            retry_delay=args.retry_delay,
        )
    except PermissionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)

    min_video_bytes = max(0, int(args.min_video_mb * 1024 * 1024))
    too_small: list[tuple[Path, int]] = []
    eligible_dirs: list[Path] = []
    for folder in local_dirs:
        mp4_bytes = session_mp4_total_bytes(folder)
        if mp4_bytes < min_video_bytes:
            too_small.append((folder, mp4_bytes))
        else:
            eligible_dirs.append(folder)

    verify_hashes = not args.no_verify_hash
    hash_cache: dict[tuple[Path, int, int], str] = {}
    skipped_remote: list[Path] = []
    to_upload: list[Path] = []
    incomplete_remote: list[tuple[Path, str]] = []
    existing_dirs = [folder for folder in eligible_dirs if folder.name in remote_folders]

    if existing_dirs:
        checks = "文件名、大小和 SHA-256" if verify_hashes else "文件名和大小"
        print(
            f"正在校验 {len(existing_dirs)} 个远程同名 session 的{checks}"
            "（只读取元数据，不下载远程视频）...",
            flush=True,
        )

    existing_index = 0
    for folder in eligible_dirs:
        if folder.name not in remote_folders:
            to_upload.append(folder)
            continue

        existing_index += 1
        print(f"  [{existing_index}/{len(existing_dirs)}] {folder.name}", flush=True)
        try:
            remote_files = call_with_retries(
                lambda folder=folder: list_remote_session_files(
                    api,
                    args.repo_id,
                    token,
                    folder.name,
                    dataset_dir=args.dataset_dir,
                ),
                description=f"读取远程清单 {folder.name}",
                max_attempts=args.max_attempts,
                retry_delay=args.retry_delay,
            )
            check = check_remote_manifest(
                local_session_manifest(folder),
                remote_files,
                verify_hashes=verify_hashes,
                hash_cache=hash_cache,
            )
        except PermissionError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            check = ManifestCheck(False, f"无法校验远程清单: {exc}")

        if check.complete:
            print(f"    完整，跳过：{check.detail}", flush=True)
            skipped_remote.append(folder)
        else:
            print(f"    不完整，将重新上传：{check.detail}", flush=True)
            incomplete_remote.append((folder, check.detail))
            to_upload.append(folder)

    print(
        f"{args.repo_id}/{args.dataset_dir}  "
        f"上传 {len(to_upload)}  "
        f"跳过已完整 {len(skipped_remote)}  "
        f"跳过过小 {len(too_small)}"
    )
    if skipped_remote:
        print("跳过(远程完整):", ", ".join(d.name for d in skipped_remote))
    if incomplete_remote:
        for folder, detail in incomplete_remote:
            print(f"重传(远程不完整): {folder.name} - {detail}")
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
            success, detail = upload_session_with_retries(
                api,
                repo_id=args.repo_id,
                token=token,
                dataset_dir=args.dataset_dir,
                folder=folder,
                max_attempts=args.max_attempts,
                retry_delay=args.retry_delay,
                verify_hashes=verify_hashes,
                hash_cache=hash_cache,
            )
            if not success:
                print(f"  最终失败: {detail}", file=sys.stderr)
                failed.append(name)
        except PermissionError as exc:
            print(f"  失败: {exc}", file=sys.stderr)
            failed.append(name)
        except Exception as exc:
            print(f"  失败: {exc}", file=sys.stderr)
            failed.append(name)

    if failed:
        print(f"完成，{len(failed)} 个失败: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)
    print(f"完成，已上传 {len(to_upload)} 个文件夹。")


if __name__ == "__main__":
    main()
