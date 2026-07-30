#!/usr/bin/env python3
"""Upload session folders from the game-recorder recordings/ dir to S3."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Callable, TypeVar

# ---------------------------------------------------------------------------
# Non-secret defaults. Access keys come from local oss_credentials.json
# (gitignored; copy from oss_credentials.example.json).
# ---------------------------------------------------------------------------
S3_ENDPOINT = "https://oss-cn-shenzhen.aliyuncs.com"
S3_BUCKET = "aws-kelei"
S3_PREFIX = "game-raw-data"
S3_ACCESS_KEY = ""
S3_SECRET_KEY = ""
S3_REGION = "cn-shenzhen"
OSS_CREDENTIALS_FILE = "oss_credentials.json"

DEFAULT_SKIP_DIRS = frozenset({"overlay"})
DEFAULT_MIN_VIDEO_MB = 10
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 5.0
UPLOAD_INTERNAL_FILES = frozenset({".ms_upload_cache", ".ms_upload_progress", ".s3_upload_cache"})
UPLOAD_IGNORED_DIRS = frozenset({".git", ".cache"})
# Baidu Netdisk client temp files (e.g. foo.mp4.baiduyun.uploading.cfg).
UPLOAD_IGNORED_NAME_SUFFIXES = (
    ".baiduyun.uploading.cfg",
    ".baiduyun.downloading.cfg",
)
BAIDU_GAME_DATA_DIR = "/game-data"
MODELSCOPE_REPO_ID = "kusriri/world-game-data"
MODELSCOPE_DATASET_DIR = "recordings"
MODELSCOPE_TOKEN = "ms-54fac99a-5958-42d4-879d-b9445227cb51"

T = TypeVar("T")


@dataclass(frozen=True)
class LocalFile:
    path: Path
    size: int


@dataclass(frozen=True)
class RemoteFile:
    size: int


@dataclass(frozen=True)
class ManifestCheck:
    complete: bool
    detail: str


def _pack_root() -> Path:
    return Path(__file__).resolve().parent


def _game_recorder_root() -> Path:
    return _pack_root().parent


def load_oss_credentials(cred_path: Path | None = None) -> dict[str, str]:
    """Load OSS settings from local oss_credentials.json."""
    path = cred_path or (_pack_root() / OSS_CREDENTIALS_FILE)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 {path.name}：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path.name} 格式错误：根节点必须是 JSON 对象")
    out: dict[str, str] = {}
    for key in (
        "endpoint",
        "bucket",
        "prefix",
        "access_key",
        "secret_key",
        "region",
    ):
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    return out


def apply_oss_credentials_defaults() -> None:
    """Fill empty module-level OSS defaults from oss_credentials.json if present."""
    global S3_ENDPOINT, S3_BUCKET, S3_PREFIX, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION
    try:
        data = load_oss_credentials()
    except RuntimeError:
        return
    S3_ENDPOINT = data.get("endpoint", S3_ENDPOINT)
    S3_BUCKET = data.get("bucket", S3_BUCKET)
    S3_PREFIX = data.get("prefix", S3_PREFIX)
    S3_ACCESS_KEY = data.get("access_key", S3_ACCESS_KEY)
    S3_SECRET_KEY = data.get("secret_key", S3_SECRET_KEY)
    S3_REGION = data.get("region", S3_REGION)


apply_oss_credentials_defaults()


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
                f"  {description}失败（第 {attempt}/{max_attempts} 次）：{exc}",
                file=sys.stderr,
                flush=True,
            )
            print(f"  {delay:g} 秒后重试...", flush=True)
            sleep(delay)
    raise RuntimeError(f"{description} failed")  # pragma: no cover


def make_s3_client(
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    region: str,
):
    import boto3
    from botocore.client import Config

    # Aliyun OSS needs virtual-hosted URLs and does not support newer AWS
    # default checksum / streaming trailer encodings used by recent botocore.
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def verify_write_access(client, bucket: str, prefix: str) -> None:
    """Probe bucket write access with a tiny put + delete under the prefix."""
    key = f"{prefix.strip('/')}/.s3_upload_write_probe"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=b"ok")
        client.delete_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise PermissionError(
            f"无法写入桶 {bucket}（prefix={prefix}）：{exc}"
        ) from exc


def remote_session_prefix(session_name: str, *, prefix: str = S3_PREFIX) -> str:
    return f"{prefix.strip('/')}/{session_name}"


def list_remote_session_folders(
    client, bucket: str, *, prefix: str = S3_PREFIX
) -> set[str]:
    root = f"{prefix.strip('/')}/"
    remote: set[str] = set()
    continuation: str | None = None
    while True:
        kwargs = {
            "Bucket": bucket,
            "Prefix": root,
            "Delimiter": "/",
        }
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = client.list_objects_v2(**kwargs)
        for entry in resp.get("CommonPrefixes", []):
            common = (entry.get("Prefix") or "").strip("/")
            if not common.startswith(root.strip("/")):
                continue
            # game-data-raw/{session_name}
            parts = common.split("/")
            if len(parts) >= 2 and parts[0] == prefix.strip("/"):
                remote.add(parts[1])
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
    return remote


def list_remote_session_files(
    client,
    bucket: str,
    session_name: str,
    *,
    prefix: str = S3_PREFIX,
) -> dict[str, RemoteFile]:
    session_root = remote_session_prefix(session_name, prefix=prefix)
    object_prefix = f"{session_root}/"
    remote: dict[str, RemoteFile] = {}
    continuation: str | None = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": object_prefix}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj.get("Key") or ""
            if not key.startswith(object_prefix):
                continue
            relative = key[len(object_prefix) :]
            if not relative or relative.endswith("/"):
                continue
            remote[relative] = RemoteFile(size=int(obj.get("Size") or 0))
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
    return remote


def _is_ignored_upload_file(name: str) -> bool:
    if name in UPLOAD_INTERNAL_FILES:
        return True
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in UPLOAD_IGNORED_NAME_SUFFIXES)


def local_session_manifest(folder: Path) -> dict[str, LocalFile]:
    manifest: dict[str, LocalFile] = {}
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(folder)
        if _is_ignored_upload_file(path.name):
            continue
        if any(part in UPLOAD_IGNORED_DIRS for part in relative.parts):
            continue
        stat = path.stat()
        manifest[relative.as_posix()] = LocalFile(path=path, size=stat.st_size)
    return manifest


def _short_path_list(paths: list[str], *, limit: int = 3) -> str:
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        shown += f" 等 {len(paths)} 个"
    return shown


def check_remote_manifest(
    local: dict[str, LocalFile],
    remote: dict[str, RemoteFile],
    *,
    verify_size: bool,
) -> ManifestCheck:
    if not local:
        return ManifestCheck(False, "本地文件夹为空")

    missing = sorted(set(local) - set(remote))
    size_mismatches: list[str] = []

    if verify_size:
        for relative_path, local_file in local.items():
            remote_file = remote.get(relative_path)
            if remote_file is None:
                continue
            if local_file.size != remote_file.size:
                size_mismatches.append(
                    f"{relative_path} (本地 {local_file.size} / 远程 {remote_file.size})"
                )

    problems: list[str] = []
    if missing:
        problems.append(f"缺少文件: {_short_path_list(missing)}")
    if size_mismatches:
        problems.append(f"大小不一致: {_short_path_list(sorted(size_mismatches))}")
    if problems:
        return ManifestCheck(False, "；".join(problems))

    size_note = "、大小" if verify_size else ""
    return ManifestCheck(True, f"{len(local)} 个文件的名称{size_note}一致")


def session_mp4_total_bytes(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.glob("*.mp4") if path.is_file())


def format_mib(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.2f}MB"


def format_speed(bytes_per_sec: float) -> str:
    return f"{bytes_per_sec / (1024 * 1024):.2f} MB/s"


def format_duration(seconds: float) -> str:
    if seconds != seconds or seconds < 0 or seconds == float("inf"):  # NaN / invalid
        return "--"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes > 0:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


class TransferProgress:
    """boto3 upload Callback: live speed (MB/s) and ETA on one console line."""

    def __init__(self, total_bytes: int) -> None:
        self.total_bytes = max(0, int(total_bytes))
        self.uploaded = 0
        self._lock = threading.Lock()
        self._started = time.perf_counter()
        self._last_print = 0.0
        self._current_name = ""

    def begin_file(self, name: str) -> None:
        with self._lock:
            self._current_name = name
            self._render(force=True)

    def __call__(self, bytes_amount: int) -> None:
        with self._lock:
            self.uploaded += int(bytes_amount)
            now = time.perf_counter()
            if now - self._last_print >= 0.25 or self.uploaded >= self.total_bytes:
                self._render(force=True)
                self._last_print = now

    def finish_file(self) -> None:
        with self._lock:
            self._render(force=True, newline=True)

    def summary_line(self) -> str:
        elapsed = max(time.perf_counter() - self._started, 1e-6)
        speed = self.uploaded / elapsed
        return (
            f"{format_mib(self.uploaded)} 用时 {format_duration(elapsed)}  "
            f"平均 {format_speed(speed)}"
        )

    def _render(self, *, force: bool = False, newline: bool = False) -> None:
        del force  # always called under lock when needed
        elapsed = max(time.perf_counter() - self._started, 1e-6)
        speed = self.uploaded / elapsed
        remaining = max(self.total_bytes - self.uploaded, 0)
        eta = remaining / speed if speed > 0 else float("inf")
        pct = (100.0 * self.uploaded / self.total_bytes) if self.total_bytes else 100.0
        name = self._current_name or "..."
        line = (
            f"    {name}  {format_mib(self.uploaded)}/{format_mib(self.total_bytes)} "
            f"({pct:5.1f}%)  {format_speed(speed)}  预计剩余 {format_duration(eta)}"
        )
        end = "\n" if newline else "\r"
        print(f"\r{line:<140}", end=end, flush=True)


def try_load_baidu_access_token(
    *,
    pack_root: Path,
    game_root: Path,
    cred_dir: Path | None,
) -> tuple[str, Path]:
    """Return (access_token, cred_dir). Uses bundled Baidu credentials by default."""
    from baidu_remote import get_access_token

    return get_access_token(pack_root=pack_root, game_root=game_root, cred_dir=cred_dir)


def filter_complete_on_baidu(
    folders: list[Path],
    *,
    access_token: str,
    game_data_dir: str,
    verify_size: bool,
    max_attempts: int,
    retry_delay: float,
) -> tuple[list[Path], list[Path], list[tuple[Path, str]]]:
    """Split folders into (remaining, skipped_complete, incomplete_on_baidu)."""
    from baidu_remote import list_session_files, list_session_folders

    baidu_folders = call_with_retries(
        lambda: list_session_folders(access_token, game_data_dir=game_data_dir),
        description="读取百度 /game-data session 列表",
        max_attempts=max_attempts,
        retry_delay=retry_delay,
    )

    remaining: list[Path] = []
    skipped: list[Path] = []
    incomplete: list[tuple[Path, str]] = []
    existing = [folder for folder in folders if folder.name in baidu_folders]
    if existing:
        checks = "文件名和大小" if verify_size else "文件名"
        print(
            f"正在校验 {len(existing)} 个百度同名 session 的{checks}"
            f"（目录 {game_data_dir}，只读取元数据）...",
            flush=True,
        )

    existing_index = 0
    for folder in folders:
        if folder.name not in baidu_folders:
            remaining.append(folder)
            continue

        existing_index += 1
        print(f"  [百度 {existing_index}/{len(existing)}] {folder.name}", flush=True)
        try:
            remote_sizes = call_with_retries(
                lambda folder=folder: list_session_files(
                    access_token,
                    folder.name,
                    game_data_dir=game_data_dir,
                ),
                description=f"读取百度清单 {folder.name}",
                max_attempts=max_attempts,
                retry_delay=retry_delay,
            )
            remote_files = {
                relative: RemoteFile(size=size) for relative, size in remote_sizes.items()
            }
            check = check_remote_manifest(
                local_session_manifest(folder),
                remote_files,
                verify_size=verify_size,
            )
        except Exception as exc:
            check = ManifestCheck(False, f"无法校验百度清单: {exc}")

        if check.complete:
            print(f"    百度已完整，跳过上传 OSS：{check.detail}", flush=True)
            skipped.append(folder)
        else:
            print(f"    百度不完整，继续走 OSS：{check.detail}", flush=True)
            incomplete.append((folder, check.detail))
            remaining.append(folder)

    return remaining, skipped, incomplete


def check_modelscope_manifest(
    local: dict[str, LocalFile],
    remote: dict[str, object],
    *,
    verify_size: bool,
) -> ManifestCheck:
    """Compare local session files to ModelScope metadata (names/sizes + CRLF)."""
    from modelscope_remote import RemoteFile, matches_after_crlf_normalize

    if not local:
        return ManifestCheck(False, "本地文件夹为空")

    missing = sorted(set(local) - set(remote))
    pending: list[str] = []
    size_mismatches: list[str] = []

    for relative_path, local_file in local.items():
        remote_file = remote.get(relative_path)
        if remote_file is None:
            continue
        assert isinstance(remote_file, RemoteFile)
        if remote_file.in_check:
            pending.append(relative_path)
            continue
        if not verify_size:
            continue
        if local_file.size == remote_file.size:
            continue
        if matches_after_crlf_normalize(
            local_file.path, local_file.size, remote_file.size
        ):
            continue
        size_mismatches.append(
            f"{relative_path} (本地 {local_file.size} / 远程 {remote_file.size})"
        )

    problems: list[str] = []
    if missing:
        problems.append(f"缺少文件: {_short_path_list(missing)}")
    if pending:
        problems.append(f"服务器仍在校验: {_short_path_list(sorted(pending))}")
    if size_mismatches:
        problems.append(f"大小不一致: {_short_path_list(sorted(size_mismatches))}")
    if problems:
        return ManifestCheck(False, "；".join(problems))

    size_note = "、大小" if verify_size else ""
    return ManifestCheck(True, f"{len(local)} 个文件的名称{size_note}一致")


def filter_complete_on_modelscope(
    folders: list[Path],
    *,
    api,
    repo_id: str,
    token: str,
    dataset_dir: str,
    verify_size: bool,
    max_attempts: int,
    retry_delay: float,
) -> tuple[list[Path], list[Path], list[tuple[Path, str]]]:
    """Split folders into (remaining, skipped_complete, incomplete_on_modelscope)."""
    from modelscope_remote import list_session_files, list_session_folders

    ms_folders = call_with_retries(
        lambda: list_session_folders(
            api, repo_id, token, dataset_dir=dataset_dir
        ),
        description="读取 ModelScope session 列表",
        max_attempts=max_attempts,
        retry_delay=retry_delay,
    )

    remaining: list[Path] = []
    skipped: list[Path] = []
    incomplete: list[tuple[Path, str]] = []
    existing = [folder for folder in folders if folder.name in ms_folders]

    if existing:
        checks = "文件名和大小" if verify_size else "文件名"
        print(
            f"正在校验 {len(existing)} 个 ModelScope 同名 session 的{checks}"
            "（只读取元数据，不下载远程视频）...",
            flush=True,
        )

    existing_index = 0
    for folder in folders:
        if folder.name not in ms_folders:
            remaining.append(folder)
            continue

        existing_index += 1
        print(
            f"  [ModelScope {existing_index}/{len(existing)}] {folder.name}",
            flush=True,
        )
        try:
            remote_files = call_with_retries(
                lambda folder=folder: list_session_files(
                    api,
                    repo_id,
                    token,
                    folder.name,
                    dataset_dir=dataset_dir,
                ),
                description=f"读取 ModelScope 清单 {folder.name}",
                max_attempts=max_attempts,
                retry_delay=retry_delay,
            )
            check = check_modelscope_manifest(
                local_session_manifest(folder),
                remote_files,
                verify_size=verify_size,
            )
        except Exception as exc:
            check = ManifestCheck(False, f"无法校验 ModelScope 清单: {exc}")

        if check.complete:
            print(f"    ModelScope 已完整，跳过上传 OSS：{check.detail}", flush=True)
            skipped.append(folder)
        else:
            print(f"    ModelScope 不完整，继续走 OSS：{check.detail}", flush=True)
            incomplete.append((folder, check.detail))
            remaining.append(folder)

    return remaining, skipped, incomplete


def upload_session_files(
    client,
    *,
    bucket: str,
    prefix: str,
    folder: Path,
    progress: TransferProgress | None = None,
) -> TransferProgress:
    session_root = remote_session_prefix(folder.name, prefix=prefix)
    local_files = local_session_manifest(folder)
    total_files = len(local_files)
    total_bytes = sum(item.size for item in local_files.values())
    tracker = progress or TransferProgress(total_bytes)
    print(
        f"    共 {total_files} 个文件，合计 {format_mib(total_bytes)}",
        flush=True,
    )
    for index, (relative, local_file) in enumerate(local_files.items(), start=1):
        key = f"{session_root}/{relative}"
        label = f"({index}/{total_files}) {relative}"
        tracker.begin_file(label)
        try:
            client.upload_file(
                str(local_file.path),
                bucket,
                key,
                Callback=tracker,
            )
        finally:
            tracker.finish_file()
    return tracker


def upload_session_with_retries(
    client,
    *,
    bucket: str,
    prefix: str,
    folder: Path,
    max_attempts: int,
    retry_delay: float,
    verify_size: bool,
) -> tuple[bool, str]:
    name = folder.name
    last_detail = "未知错误"

    for attempt in range(1, max_attempts + 1):
        upload_error: Exception | None = None
        if attempt > 1:
            print(f"  开始第 {attempt}/{max_attempts} 次 session 上传尝试...", flush=True)
        try:
            tracker = upload_session_files(
                client, bucket=bucket, prefix=prefix, folder=folder
            )
            print(f"  本 session 上传完成：{tracker.summary_line()}", flush=True)
        except Exception as exc:
            upload_error = exc
            print(file=sys.stderr)  # end progress line if interrupted

        try:
            remote_files = call_with_retries(
                lambda: list_remote_session_files(
                    client, bucket, name, prefix=prefix
                ),
                description=f"校验远程 session {name}",
                max_attempts=max_attempts,
                retry_delay=retry_delay,
            )
            check = check_remote_manifest(
                local_session_manifest(folder),
                remote_files,
                verify_size=verify_size,
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


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Upload recordings/ sessions to S3 and verify completeness before skipping."
    )
    ap.add_argument(
        "recordings",
        type=Path,
        nargs="?",
        default=_game_recorder_root() / "recordings",
        help="recordings root (default: ../recordings relative to this pack)",
    )
    ap.add_argument("--endpoint", default=S3_ENDPOINT)
    ap.add_argument("--bucket", default=S3_BUCKET)
    ap.add_argument(
        "--prefix",
        default=S3_PREFIX,
        help=f"top-level S3 prefix (default: {S3_PREFIX})",
    )
    ap.add_argument("--access-key", default=S3_ACCESS_KEY)
    ap.add_argument("--secret-key", default=S3_SECRET_KEY)
    ap.add_argument("--region", default=S3_REGION)
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
        "--no-verify-size",
        action="store_true",
        help="only compare remote file names; do not compare sizes",
    )
    ap.add_argument(
        "--skip-baidu-check",
        action="store_true",
        help="do not skip sessions that are already complete on Baidu /game-data",
    )
    ap.add_argument(
        "--baidu-dir",
        default=BAIDU_GAME_DATA_DIR,
        help=f"Baidu Netdisk game-data directory (default: {BAIDU_GAME_DATA_DIR})",
    )
    ap.add_argument(
        "--baidu-cred-dir",
        type=Path,
        default=None,
        help="optional override dir for Baidu keys.txt + token.json "
        "(default: bundled credentials in the script; refreshed token "
        "is written to s3-upload/)",
    )
    ap.add_argument(
        "--skip-modelscope-check",
        action="store_true",
        help="do not skip sessions that are already complete on ModelScope",
    )
    ap.add_argument(
        "--modelscope-repo-id",
        default=MODELSCOPE_REPO_ID,
        help=f"ModelScope dataset repo (default: {MODELSCOPE_REPO_ID})",
    )
    ap.add_argument(
        "--modelscope-dataset-dir",
        default=MODELSCOPE_DATASET_DIR,
        help=f"remote subdirectory in the ModelScope dataset (default: {MODELSCOPE_DATASET_DIR})",
    )
    ap.add_argument(
        "--modelscope-token",
        default=MODELSCOPE_TOKEN,
        help="ModelScope access token (default: bundled token)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.access_key or not args.secret_key:
        cred_file = _pack_root() / OSS_CREDENTIALS_FILE
        print(
            "错误：缺少 OSS AccessKey。\n"
            f"请将 {cred_file.name} 放进本目录后重新运行 upload.bat\n"
            f"（可参考 oss_credentials.example.json）。",
            file=sys.stderr,
        )
        sys.exit(1)

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

    try:
        import boto3  # noqa: F401
    except ImportError:
        print("错误：未安装 boto3，请先运行 install.bat。", file=sys.stderr)
        sys.exit(1)

    client = make_s3_client(
        endpoint=args.endpoint,
        access_key=args.access_key,
        secret_key=args.secret_key,
        region=args.region,
    )
    prefix = args.prefix.strip("/")

    try:
        call_with_retries(
            lambda: verify_write_access(client, args.bucket, prefix),
            description="检查桶写入权限",
            max_attempts=args.max_attempts,
            retry_delay=args.retry_delay,
        )
        remote_folders = call_with_retries(
            lambda: list_remote_session_folders(client, args.bucket, prefix=prefix),
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

    verify_size = not args.no_verify_size
    skipped_baidu: list[Path] = []
    incomplete_baidu: list[tuple[Path, str]] = []
    skipped_modelscope: list[Path] = []
    incomplete_modelscope: list[tuple[Path, str]] = []
    candidates = eligible_dirs

    if args.skip_baidu_check:
        print("已跳过百度完整性检查（--skip-baidu-check）。", flush=True)
    else:
        try:
            access_token, cred_dir = try_load_baidu_access_token(
                pack_root=_pack_root(),
                game_root=_game_recorder_root(),
                cred_dir=args.baidu_cred_dir,
            )
        except Exception as exc:
            print(f"错误：加载百度凭证失败：{exc}", file=sys.stderr)
            sys.exit(1)

        print(f"已启用百度完整性检查（凭证：内置或 {cred_dir}）", flush=True)
        baidu_dir = args.baidu_dir if args.baidu_dir.startswith("/") else f"/{args.baidu_dir}"
        try:
            candidates, skipped_baidu, incomplete_baidu = filter_complete_on_baidu(
                candidates,
                access_token=access_token,
                game_data_dir=baidu_dir,
                verify_size=verify_size,
                max_attempts=args.max_attempts,
                retry_delay=args.retry_delay,
            )
        except Exception as exc:
            print(f"错误：百度完整性检查失败：{exc}", file=sys.stderr)
            sys.exit(1)

    if args.skip_modelscope_check:
        print("已跳过 ModelScope 完整性检查（--skip-modelscope-check）。", flush=True)
    else:
        try:
            from modelscope.hub.api import HubApi  # noqa: F401
            from modelscope_remote import make_api
        except ImportError:
            print(
                "错误：未安装 modelscope，请重新运行 s3-upload\\install.bat。",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            ms_api = make_api(args.modelscope_token)
        except Exception as exc:
            print(f"错误：登录 ModelScope 失败：{exc}", file=sys.stderr)
            sys.exit(1)

        print(
            f"已启用 ModelScope 完整性检查（{args.modelscope_repo_id}/"
            f"{args.modelscope_dataset_dir.strip('/')}）",
            flush=True,
        )
        try:
            candidates, skipped_modelscope, incomplete_modelscope = (
                filter_complete_on_modelscope(
                    candidates,
                    api=ms_api,
                    repo_id=args.modelscope_repo_id,
                    token=args.modelscope_token,
                    dataset_dir=args.modelscope_dataset_dir,
                    verify_size=verify_size,
                    max_attempts=args.max_attempts,
                    retry_delay=args.retry_delay,
                )
            )
        except Exception as exc:
            print(f"错误：ModelScope 完整性检查失败：{exc}", file=sys.stderr)
            sys.exit(1)

    skipped_remote: list[Path] = []
    to_upload: list[Path] = []
    incomplete_remote: list[tuple[Path, str]] = []
    existing_dirs = [folder for folder in candidates if folder.name in remote_folders]

    if existing_dirs:
        checks = "文件名和大小" if verify_size else "文件名"
        print(
            f"正在校验 {len(existing_dirs)} 个 OSS 同名 session 的{checks}"
            "（只读取元数据，不下载远程视频）...",
            flush=True,
        )

    existing_index = 0
    for folder in candidates:
        if folder.name not in remote_folders:
            to_upload.append(folder)
            continue

        existing_index += 1
        print(f"  [OSS {existing_index}/{len(existing_dirs)}] {folder.name}", flush=True)
        try:
            remote_files = call_with_retries(
                lambda folder=folder: list_remote_session_files(
                    client, args.bucket, folder.name, prefix=prefix
                ),
                description=f"读取 OSS 清单 {folder.name}",
                max_attempts=args.max_attempts,
                retry_delay=args.retry_delay,
            )
            check = check_remote_manifest(
                local_session_manifest(folder),
                remote_files,
                verify_size=verify_size,
            )
        except PermissionError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            check = ManifestCheck(False, f"无法校验远程清单: {exc}")

        if check.complete:
            print(f"    OSS 已完整，跳过：{check.detail}", flush=True)
            skipped_remote.append(folder)
        else:
            print(f"    OSS 不完整，将重新上传：{check.detail}", flush=True)
            incomplete_remote.append((folder, check.detail))
            to_upload.append(folder)

    dest = f"s3://{args.bucket}/{prefix}"
    print(
        f"{dest}  "
        f"上传 {len(to_upload)}  "
        f"跳过百度完整 {len(skipped_baidu)}  "
        f"跳过 ModelScope 完整 {len(skipped_modelscope)}  "
        f"跳过 OSS 完整 {len(skipped_remote)}  "
        f"跳过过小 {len(too_small)}"
    )
    if skipped_baidu:
        print("跳过(百度已完整):", ", ".join(d.name for d in skipped_baidu))
    if incomplete_baidu:
        for folder, detail in incomplete_baidu:
            print(f"百度不完整(继续 OSS): {folder.name} - {detail}")
    if skipped_modelscope:
        print(
            "跳过(ModelScope 已完整):",
            ", ".join(d.name for d in skipped_modelscope),
        )
    if incomplete_modelscope:
        for folder, detail in incomplete_modelscope:
            print(f"ModelScope 不完整(继续 OSS): {folder.name} - {detail}")
    if skipped_remote:
        print("跳过(OSS 已完整):", ", ".join(d.name for d in skipped_remote))
    if incomplete_remote:
        for folder, detail in incomplete_remote:
            print(f"重传(OSS 不完整): {folder.name} - {detail}")
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

    batch_bytes = 0
    for folder in to_upload:
        batch_bytes += sum(item.size for item in local_session_manifest(folder).values())
    print(
        f"开始上传：{len(to_upload)} 个 session，合计 {format_mib(batch_bytes)}",
        flush=True,
    )

    failed: list[str] = []
    batch_uploaded = 0
    batch_started = time.perf_counter()
    for i, folder in enumerate(to_upload, start=1):
        name = folder.name
        remote_path = remote_session_prefix(name, prefix=prefix)
        session_bytes = sum(item.size for item in local_session_manifest(folder).values())
        remaining_bytes = max(batch_bytes - batch_uploaded, 0)
        elapsed = max(time.perf_counter() - batch_started, 1e-6)
        avg_speed = batch_uploaded / elapsed if batch_uploaded else 0.0
        batch_eta = remaining_bytes / avg_speed if avg_speed > 0 else float("inf")
        print(
            f"[{i}/{len(to_upload)}] {remote_path}  "
            f"({format_mib(session_bytes)})  "
            f"总剩余 {format_mib(remaining_bytes)}  "
            f"预计总剩余 {format_duration(batch_eta)}",
            flush=True,
        )
        try:
            success, detail = upload_session_with_retries(
                client,
                bucket=args.bucket,
                prefix=prefix,
                folder=folder,
                max_attempts=args.max_attempts,
                retry_delay=args.retry_delay,
                verify_size=verify_size,
            )
            if not success:
                print(f"  最终失败: {detail}", file=sys.stderr)
                failed.append(name)
            else:
                batch_uploaded += session_bytes
        except PermissionError as exc:
            print(f"  失败: {exc}", file=sys.stderr)
            failed.append(name)
        except Exception as exc:
            print(f"  失败: {exc}", file=sys.stderr)
            failed.append(name)

    batch_elapsed = max(time.perf_counter() - batch_started, 1e-6)
    if failed:
        print(f"完成，{len(failed)} 个失败: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)
    print(
        f"完成，已上传 {len(to_upload)} 个文件夹，"
        f"合计 {format_mib(batch_uploaded)}，"
        f"用时 {format_duration(batch_elapsed)}，"
        f"平均 {format_speed(batch_uploaded / batch_elapsed)}。"
    )


if __name__ == "__main__":
    main()
