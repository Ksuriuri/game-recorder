#!/usr/bin/env python3
"""Upload session folders from the game-recorder recordings/ dir to S3."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Callable, TypeVar

# ---------------------------------------------------------------------------
# Defaults for one-click distribution to recording operators.
# ---------------------------------------------------------------------------
S3_ENDPOINT = "http://117.145.189.131:3535"
S3_BUCKET = "noiz"
S3_PREFIX = "game-data-raw"
S3_ACCESS_KEY = "1WR2PL6P0CHO0US11ICG"
S3_SECRET_KEY = "0Kkz1aPFmDViKOAufYheWoM7GTHnx4pDkQ0oJTlL"
S3_REGION = "us-east-1"

DEFAULT_SKIP_DIRS = frozenset({"overlay"})
DEFAULT_MIN_VIDEO_MB = 10
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 5.0
UPLOAD_INTERNAL_FILES = frozenset({".ms_upload_cache", ".ms_upload_progress", ".s3_upload_cache"})
UPLOAD_IGNORED_DIRS = frozenset({".git", ".cache"})
BAIDU_GAME_DATA_DIR = "/game-data"

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

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
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
            print(f"    百度已完整，跳过上传 S3：{check.detail}", flush=True)
            skipped.append(folder)
        else:
            print(f"    百度不完整，继续走 S3：{check.detail}", flush=True)
            incomplete.append((folder, check.detail))
            remaining.append(folder)

    return remaining, skipped, incomplete


def upload_session_files(
    client,
    *,
    bucket: str,
    prefix: str,
    folder: Path,
) -> None:
    session_root = remote_session_prefix(folder.name, prefix=prefix)
    local_files = local_session_manifest(folder)
    total = len(local_files)
    for index, (relative, local_file) in enumerate(local_files.items(), start=1):
        key = f"{session_root}/{relative}"
        print(f"    ({index}/{total}) {relative}", flush=True)
        client.upload_file(str(local_file.path), bucket, key)


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
    remote_path = remote_session_prefix(name, prefix=prefix)
    last_detail = "未知错误"

    for attempt in range(1, max_attempts + 1):
        upload_error: Exception | None = None
        if attempt > 1:
            print(f"  开始第 {attempt}/{max_attempts} 次 session 上传尝试...", flush=True)
        try:
            upload_session_files(client, bucket=bucket, prefix=prefix, folder=folder)
        except Exception as exc:
            upload_error = exc

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
                eligible_dirs,
                access_token=access_token,
                game_data_dir=baidu_dir,
                verify_size=verify_size,
                max_attempts=args.max_attempts,
                retry_delay=args.retry_delay,
            )
        except Exception as exc:
            print(f"错误：百度完整性检查失败：{exc}", file=sys.stderr)
            sys.exit(1)

    skipped_remote: list[Path] = []
    to_upload: list[Path] = []
    incomplete_remote: list[tuple[Path, str]] = []
    existing_dirs = [folder for folder in candidates if folder.name in remote_folders]

    if existing_dirs:
        checks = "文件名和大小" if verify_size else "文件名"
        print(
            f"正在校验 {len(existing_dirs)} 个 S3 同名 session 的{checks}"
            "（只读取元数据，不下载远程视频）...",
            flush=True,
        )

    existing_index = 0
    for folder in candidates:
        if folder.name not in remote_folders:
            to_upload.append(folder)
            continue

        existing_index += 1
        print(f"  [S3 {existing_index}/{len(existing_dirs)}] {folder.name}", flush=True)
        try:
            remote_files = call_with_retries(
                lambda folder=folder: list_remote_session_files(
                    client, args.bucket, folder.name, prefix=prefix
                ),
                description=f"读取 S3 清单 {folder.name}",
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
            print(f"    S3 已完整，跳过：{check.detail}", flush=True)
            skipped_remote.append(folder)
        else:
            print(f"    S3 不完整，将重新上传：{check.detail}", flush=True)
            incomplete_remote.append((folder, check.detail))
            to_upload.append(folder)

    dest = f"s3://{args.bucket}/{prefix}"
    print(
        f"{dest}  "
        f"上传 {len(to_upload)}  "
        f"跳过百度完整 {len(skipped_baidu)}  "
        f"跳过 S3 完整 {len(skipped_remote)}  "
        f"跳过过小 {len(too_small)}"
    )
    if skipped_baidu:
        print("跳过(百度已完整):", ", ".join(d.name for d in skipped_baidu))
    if incomplete_baidu:
        for folder, detail in incomplete_baidu:
            print(f"百度不完整(继续 S3): {folder.name} - {detail}")
    if skipped_remote:
        print("跳过(S3 已完整):", ", ".join(d.name for d in skipped_remote))
    if incomplete_remote:
        for folder, detail in incomplete_remote:
            print(f"重传(S3 不完整): {folder.name} - {detail}")
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
        remote_path = remote_session_prefix(name, prefix=prefix)
        print(f"[{i}/{len(to_upload)}] {remote_path}")
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
