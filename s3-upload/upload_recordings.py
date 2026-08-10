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
CAMERA_FILENAME = "camera.jsonl"
UPLOAD_INTERNAL_FILES = frozenset({".ms_upload_cache", ".ms_upload_progress", ".s3_upload_cache"})
UPLOAD_IGNORED_DIRS = frozenset({".git", ".cache"})
# Baidu Netdisk client temp files (e.g. foo.mp4.baiduyun.uploading.cfg).
UPLOAD_IGNORED_NAME_SUFFIXES = (
    ".baiduyun.uploading.cfg",
    ".baiduyun.downloading.cfg",
)

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


def append_upload_log(
    log_path: Path,
    *,
    folder: Path | None,
    status: str,
    detail: str,
    session_name: str | None = None,
) -> None:
    """Append one JSON line for an upload result (success or error)."""
    from datetime import datetime, timezone

    name = session_name or (folder.name if folder is not None else "")
    path_text = str(folder.resolve()) if folder is not None else ""
    payload = {
        "session": name,
        "path": path_text,
        "status": status,
        "detail": detail,
        "uploaded_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def append_success_log(
    log_path: Path,
    *,
    folder: Path,
    status: str,
    detail: str,
) -> None:
    """Compatibility wrapper around ``append_upload_log``."""
    append_upload_log(log_path, folder=folder, status=status, detail=detail)


def _safe_log_error(
    log_path: Path | None,
    *,
    folder: Path | None,
    detail: str,
    session_name: str | None = None,
) -> None:
    if log_path is None:
        return
    try:
        append_upload_log(
            log_path,
            folder=folder,
            status="error",
            detail=detail,
            session_name=session_name,
        )
    except OSError as exc:
        print(f"警告：写入错误日志失败：{exc}", file=sys.stderr)


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
        "--session",
        type=Path,
        default=None,
        help="upload only this session folder (skips scanning recordings/)",
    )
    ap.add_argument(
        "--success-log",
        type=Path,
        default=None,
        help="append one JSON line per session result (uploaded / already_complete / error)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    success_log = args.success_log.resolve() if args.success_log is not None else None
    session_dir: Path | None = (
        args.session.resolve() if args.session is not None else None
    )

    def fail(message: str, *, code: int = 1) -> None:
        print(message, file=sys.stderr)
        _safe_log_error(
            success_log,
            folder=session_dir if session_dir is not None and session_dir.is_dir() else None,
            detail=message.strip(),
            session_name=session_dir.name if session_dir is not None else None,
        )
        sys.exit(code)

    if not args.access_key or not args.secret_key:
        cred_file = _pack_root() / OSS_CREDENTIALS_FILE
        fail(
            "错误：缺少 OSS AccessKey。\n"
            f"请将 {cred_file.name} 放进本目录后重新运行 upload.bat\n"
            f"（可参考 oss_credentials.example.json）。"
        )

    if args.max_attempts < 1:
        fail("错误：--max-attempts 必须至少为 1。")
    if args.retry_delay < 0:
        fail("错误：--retry-delay 不能小于 0。")

    if session_dir is not None:
        if not session_dir.is_dir():
            fail(f"错误：找不到 session 目录：{session_dir}")
        if not (session_dir / "meta.json").is_file():
            fail(f"错误：session 缺少 meta.json：{session_dir}")
        if not (session_dir / CAMERA_FILENAME).is_file():
            fail(f"错误：session 缺少 {CAMERA_FILENAME}：{session_dir}")
        local_dirs = [session_dir]
        # Single-session mode skips the mp4 size floor; duration/camera
        # eligibility is enforced by the recorder (and camera above).
        min_video_bytes = 0
        require_camera = False  # already validated above
    else:
        recordings = args.recordings.resolve()
        if not recordings.is_dir():
            fail(f"错误：找不到 recordings 目录：{recordings}")
        skip_dirs = set(DEFAULT_SKIP_DIRS) | set(args.skip_dir)
        local_dirs = iter_session_dirs(recordings, skip_dirs=skip_dirs)
        if not local_dirs:
            print("没有可上传的 session 文件夹。")
            return
        min_video_bytes = max(0, int(args.min_video_mb * 1024 * 1024))
        require_camera = True

    try:
        import boto3  # noqa: F401
    except ImportError:
        fail("错误：未安装 boto3，请先运行 install.bat。")

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
        fail(f"错误：{exc}")
    except Exception as exc:
        fail(f"错误：{exc}")

    too_small: list[tuple[Path, int]] = []
    missing_camera: list[Path] = []
    eligible_dirs: list[Path] = []
    for folder in local_dirs:
        mp4_bytes = session_mp4_total_bytes(folder)
        if min_video_bytes > 0 and mp4_bytes < min_video_bytes:
            too_small.append((folder, mp4_bytes))
        elif require_camera and not (folder / CAMERA_FILENAME).is_file():
            missing_camera.append(folder)
        else:
            eligible_dirs.append(folder)

    verify_size = not args.no_verify_size
    skipped_remote: list[Path] = []
    to_upload: list[Path] = []
    incomplete_remote: list[tuple[Path, str]] = []
    existing_dirs = [folder for folder in eligible_dirs if folder.name in remote_folders]

    if existing_dirs:
        checks = "文件名和大小" if verify_size else "文件名"
        print(
            f"正在校验 {len(existing_dirs)} 个 OSS 同名 session 的{checks}"
            "（只读取元数据，不下载远程视频）...",
            flush=True,
        )

    existing_index = 0
    for folder in eligible_dirs:
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
            fail(f"错误：{exc}")
        except Exception as exc:
            check = ManifestCheck(False, f"无法校验远程清单: {exc}")

        if check.complete:
            print(f"    OSS 已完整，跳过：{check.detail}", flush=True)
            skipped_remote.append(folder)
            if success_log is not None:
                try:
                    append_upload_log(
                        success_log,
                        folder=folder,
                        status="already_complete",
                        detail=check.detail,
                    )
                except OSError as exc:
                    print(f"警告：写入成功日志失败：{exc}", file=sys.stderr)
        else:
            print(f"    OSS 不完整，将重新上传：{check.detail}", flush=True)
            incomplete_remote.append((folder, check.detail))
            to_upload.append(folder)

    dest = f"s3://{args.bucket}/{prefix}"
    print(
        f"{dest}  "
        f"上传 {len(to_upload)}  "
        f"跳过 OSS 完整 {len(skipped_remote)}  "
        f"跳过过小 {len(too_small)}  "
        f"跳过无 {CAMERA_FILENAME} {len(missing_camera)}"
    )
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
    if missing_camera:
        for folder in missing_camera:
            print(f"跳过(缺少 {CAMERA_FILENAME}): {folder.name}")

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

    failed: list[tuple[str, str]] = []
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
                failed.append((name, detail))
                _safe_log_error(success_log, folder=folder, detail=detail)
            else:
                batch_uploaded += session_bytes
                if success_log is not None:
                    try:
                        append_upload_log(
                            success_log,
                            folder=folder,
                            status="uploaded",
                            detail=detail,
                        )
                    except OSError as exc:
                        print(f"警告：写入成功日志失败：{exc}", file=sys.stderr)
        except PermissionError as exc:
            print(f"  失败: {exc}", file=sys.stderr)
            failed.append((name, str(exc)))
            _safe_log_error(success_log, folder=folder, detail=str(exc))
        except Exception as exc:
            print(f"  失败: {exc}", file=sys.stderr)
            failed.append((name, str(exc)))
            _safe_log_error(success_log, folder=folder, detail=str(exc))

    batch_elapsed = max(time.perf_counter() - batch_started, 1e-6)
    if failed:
        summary = ", ".join(f"{name} ({detail})" for name, detail in failed)
        print(f"完成，{len(failed)} 个失败: {summary}", file=sys.stderr)
        sys.exit(1)
    print(
        f"完成，已上传 {len(to_upload)} 个文件夹，"
        f"合计 {format_mib(batch_uploaded)}，"
        f"用时 {format_duration(batch_elapsed)}，"
        f"平均 {format_speed(batch_uploaded / batch_elapsed)}。"
    )


if __name__ == "__main__":
    main()
