#!/usr/bin/env python3
"""Analyze /game-data by downloading meta.json files, never video content."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from list_files import (
    FILES_URL,
    ROOT,
    list_directory,
    load_keys,
    load_token,
    request_json,
)


GAME_DATA_DIR = "/game-data"
FILE_META_URL = "https://pan.baidu.com/rest/2.0/xpan/multimedia"
REPORT_PATH = ROOT / "game_data_report.json"
DOWNLOAD_WORKERS = 6
LIST_WORKERS = 8
MAX_META_BYTES = 2 * 1024 * 1024
SESSION_RE = re.compile(
    r"^(?P<recording_id>.+)_session_(?P<date>\d{8})_(?P<time>\d{6})$"
)
DATED_RECORDING_ID_RE = re.compile(r"^(?P<recorder>.+)-(?P<date>\d{8})$")
DUPLICATED_DATE_DIGIT_RE = re.compile(
    r"^(?P<recorder>.+)-(?P<date>\d{8})(?P<extra>\d)$"
)
RECORDER_ALIASES = {
    "BPK2077-LZ02": "SBPK2077-LZ02",
    "SBOK2077-LZ02": "SBPK2077-LZ02",
}


def search_files(access_token: str, keyword: str) -> list[dict[str, Any]]:
    """Recursively search /game-data without reading file contents."""
    matches: list[dict[str, Any]] = []
    page = 1
    while True:
        result = request_json(
            FILES_URL,
            {
                "method": "search",
                "access_token": access_token,
                "dir": GAME_DATA_DIR,
                "key": keyword,
                "recursion": 1,
                "page": page,
                "num": 1000,
            },
        )
        if result.get("errno") != 0:
            raise RuntimeError(
                f"搜索 {keyword!r} 失败：errno={result.get('errno')}, "
                f"request_id={result.get('request_id')}"
            )
        batch = result.get("list", [])
        matches.extend(batch)
        if not result.get("has_more") or not batch:
            return matches
        page += 1


def file_download_links(
    access_token: str, files: list[dict[str, Any]]
) -> dict[int, str]:
    links: dict[int, str] = {}
    for start in range(0, len(files), 100):
        batch = files[start : start + 100]
        fsids = [int(item["fs_id"]) for item in batch]
        result = request_json(
            FILE_META_URL,
            {
                "method": "filemetas",
                "access_token": access_token,
                "fsids": json.dumps(fsids, separators=(",", ":")),
                "dlink": 1,
            },
        )
        if result.get("errno") != 0:
            raise RuntimeError(
                f"获取 meta.json 下载链接失败：errno={result.get('errno')}, "
                f"request_id={result.get('request_id')}"
            )
        for item in result.get("list", []):
            if item.get("dlink"):
                links[int(item["fs_id"])] = str(item["dlink"])
    return links


def download_meta(access_token: str, path: str, dlink: str) -> dict[str, Any]:
    separator = "&" if "?" in dlink else "?"
    url = f"{dlink}{separator}{urllib.parse.urlencode({'access_token': access_token})}"
    request = urllib.request.Request(url, headers={"User-Agent": "pan.baidu.com"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                content_length = int(
                    response.headers.get("Content-Length", "0") or 0
                )
                if content_length > MAX_META_BYTES:
                    raise RuntimeError(f"{path} 超过 meta.json 大小限制")
                payload = response.read(MAX_META_BYTES + 1)
            break
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(2**attempt)
    if len(payload) > MAX_META_BYTES:
        raise RuntimeError(f"{path} 超过 meta.json 大小限制")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} 不是 JSON 对象")
    return value


def download_all_meta(
    access_token: str,
    meta_files: list[dict[str, Any]],
    links: dict[int, str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    downloaded: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        pending = {}
        for item in meta_files:
            fs_id = int(item["fs_id"])
            path = str(item["path"])
            dlink = links.get(fs_id)
            if not dlink:
                failures.append({"path": path, "error": "缺少下载链接"})
                continue
            future = executor.submit(download_meta, access_token, path, dlink)
            pending[future] = path

        for future in as_completed(pending):
            path = pending[future]
            try:
                downloaded[path] = future.result()
            except Exception as error:
                failures.append({"path": path, "error": str(error)})
    return downloaded, failures


def load_cached_metadata(
    meta_files: list[dict[str, Any]],
) -> dict[str, dict[str, Any]] | None:
    if not REPORT_PATH.exists():
        return None
    try:
        report = json.loads(REPORT_PATH.read_text())
        sessions = report["sessions"]
    except (OSError, KeyError, TypeError, ValueError):
        return None

    expected_paths = {str(item["path"]) for item in meta_files}
    cached_paths = {str(item.get("meta_path", "")) for item in sessions}
    if cached_paths != expected_paths:
        return None

    metadata: dict[str, dict[str, Any]] = {}
    for session in sessions:
        match = SESSION_RE.fullmatch(str(session["session_id"]))
        if not match:
            return None
        metadata[str(session["meta_path"])] = {
            "session_id": session["session_id"],
            "session_timestamp": f"{match.group('date')}_{match.group('time')}",
            "duration_s": session["duration_s"],
            "auto_stop_reason": session.get("auto_stop_reason"),
            "fps": session.get("fps"),
            "total_frames": session.get("total_frames"),
        }
    return metadata


def list_session_videos(
    access_token: str, meta_files: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """List session folders and collect MP4 metadata without downloading videos."""
    parents = sorted(
        {str(PurePosixPath(str(item["path"])).parent) for item in meta_files}
    )
    videos: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=LIST_WORKERS) as executor:
        pending = {
            executor.submit(list_directory, access_token, parent): parent
            for parent in parents
        }
        for future in as_completed(pending):
            parent = pending[future]
            try:
                entries = future.result()
            except Exception as error:
                failures.append({"path": parent, "error": str(error)})
                continue
            videos.extend(
                item
                for item in entries
                if str(item.get("server_filename", "")).lower().endswith(".mp4")
            )
    return videos, failures


def discover_game_data_files(
    access_token: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """Recursively list /game-data; avoids the intermittently empty search API."""
    meta_files: list[dict[str, Any]] = []
    mp4_files: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    seen_directories = {GAME_DATA_DIR}

    with ThreadPoolExecutor(max_workers=LIST_WORKERS) as executor:
        pending = {
            executor.submit(list_directory, access_token, GAME_DATA_DIR): GAME_DATA_DIR
        }
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                directory = pending.pop(future)
                try:
                    entries = future.result()
                except Exception as error:
                    failures.append({"path": directory, "error": str(error)})
                    continue

                for item in entries:
                    path = str(item.get("path", ""))
                    if item.get("isdir"):
                        if path and path not in seen_directories:
                            seen_directories.add(path)
                            pending[
                                executor.submit(list_directory, access_token, path)
                            ] = path
                        continue

                    filename = str(item.get("server_filename", ""))
                    if filename == "meta.json":
                        meta_files.append(item)
                    elif filename.lower().endswith(".mp4"):
                        mp4_files.append(item)

    meta_files.sort(key=lambda item: str(item.get("path", "")))
    mp4_files.sort(key=lambda item: str(item.get("path", "")))
    failures.sort(key=lambda item: item["path"])
    return meta_files, mp4_files, failures


def session_identity(path: str, meta: dict[str, Any]) -> tuple[str, str, str, str]:
    parent_name = PurePosixPath(path).parent.name
    session_id = str(meta.get("session_id") or parent_name)
    match = SESSION_RE.fullmatch(session_id)
    if not match:
        raise ValueError(f"无法解析 session_id：{session_id}")

    recording_id = match.group("recording_id")
    timestamp = str(meta.get("session_timestamp") or "")
    date_compact = (
        timestamp[:8]
        if re.match(r"^\d{8}_\d{6}$", timestamp)
        else match.group("date")
    )

    recorder_match = DATED_RECORDING_ID_RE.fullmatch(recording_id)
    if recorder_match:
        recorder = recorder_match.group("recorder")
    else:
        duplicated_match = DUPLICATED_DATE_DIGIT_RE.fullmatch(recording_id)
        if (
            duplicated_match
            and duplicated_match.group("date") == date_compact
            and duplicated_match.group("extra") == date_compact[-1]
        ):
            recorder = duplicated_match.group("recorder")
        else:
            recorder = recording_id
    recorder = RECORDER_ALIASES.get(recorder, recorder)

    date = datetime.strptime(date_compact, "%Y%m%d").date().isoformat()
    return session_id, recording_id, recorder, date


def round_hours(seconds: float) -> float:
    return round(seconds / 3600, 3)


def build_report(
    meta_files: list[dict[str, Any]],
    mp4_files: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    failures: list[dict[str, str]],
    video_listing_failures: list[dict[str, str]],
) -> dict[str, Any]:
    meta_by_parent = {
        str(PurePosixPath(path).parent): (path, meta)
        for path, meta in metadata.items()
    }
    mp4_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in mp4_files:
        mp4_by_parent[str(PurePosixPath(str(item["path"])).parent)].append(item)

    sessions: list[dict[str, Any]] = []
    parse_failures: list[dict[str, str]] = []
    metadata_only: list[str] = []
    unknown_video_status: list[str] = []
    failed_video_parents = {item["path"] for item in video_listing_failures}

    for parent, (path, meta) in sorted(meta_by_parent.items()):
        try:
            session_id, recording_id, recorder, date = session_identity(path, meta)
            duration_s = float(meta["duration_s"])
            if duration_s < 0:
                raise ValueError("duration_s 不能为负数")
        except (KeyError, TypeError, ValueError) as error:
            parse_failures.append({"path": path, "error": str(error)})
            continue

        videos = mp4_by_parent.get(parent, [])
        video_status = (
            "unknown"
            if parent in failed_video_parents
            else "present"
            if videos
            else "missing"
        )
        has_video = video_status == "present"
        if video_status == "missing":
            metadata_only.append(parent)
        elif video_status == "unknown":
            unknown_video_status.append(parent)
        sessions.append(
            {
                "session_id": session_id,
                "recording_id": recording_id,
                "recorder": recorder,
                "date": date,
                "duration_s": round(duration_s, 2),
                "duration_hours": round_hours(duration_s),
                "video_files": len(videos),
                "video_bytes": sum(int(item.get("size", 0)) for item in videos),
                "has_video": has_video,
                "video_status": video_status,
                "auto_stop_reason": meta.get("auto_stop_reason"),
                "fps": meta.get("fps"),
                "total_frames": meta.get("total_frames"),
                "meta_path": path,
            }
        )

    video_sessions = [item for item in sessions if item["has_video"]]
    daily_acc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "duration_s": 0.0,
            "sessions": 0,
            "recorders": set(),
            "by_recorder": defaultdict(lambda: {"duration_s": 0.0, "sessions": 0}),
        }
    )
    recorder_acc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "duration_s": 0.0,
            "sessions": 0,
            "dates": set(),
            "recording_ids": set(),
        }
    )

    for session in video_sessions:
        day = daily_acc[session["date"]]
        day["duration_s"] += session["duration_s"]
        day["sessions"] += 1
        day["recorders"].add(session["recorder"])
        day_recorder = day["by_recorder"][session["recorder"]]
        day_recorder["duration_s"] += session["duration_s"]
        day_recorder["sessions"] += 1

        recorder = recorder_acc[session["recorder"]]
        recorder["duration_s"] += session["duration_s"]
        recorder["sessions"] += 1
        recorder["dates"].add(session["date"])
        recorder["recording_ids"].add(session["recording_id"])

    daily = []
    for date, value in sorted(daily_acc.items()):
        by_recorder = []
        for recorder, recorder_value in sorted(value["by_recorder"].items()):
            by_recorder.append(
                {
                    "recorder": recorder,
                    "sessions": recorder_value["sessions"],
                    "duration_s": round(recorder_value["duration_s"], 2),
                    "duration_hours": round_hours(recorder_value["duration_s"]),
                }
            )
        daily.append(
            {
                "date": date,
                "sessions": value["sessions"],
                "recorder_count": len(value["recorders"]),
                "duration_s": round(value["duration_s"], 2),
                "duration_hours": round_hours(value["duration_s"]),
                "by_recorder": by_recorder,
            }
        )

    recorders = []
    for recorder, value in sorted(recorder_acc.items()):
        recorders.append(
            {
                "recorder": recorder,
                "sessions": value["sessions"],
                "duration_s": round(value["duration_s"], 2),
                "duration_hours": round_hours(value["duration_s"]),
                "dates": sorted(value["dates"]),
                "recording_ids": sorted(value["recording_ids"]),
            }
        )

    video_without_meta = sorted(set(mp4_by_parent) - set(meta_by_parent))
    total_duration_s = sum(float(item["duration_s"]) for item in video_sessions)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": GAME_DATA_DIR,
        "methodology": {
            "duration": "meta.json.duration_s",
            "date": "meta.json.session_timestamp 的日期部分",
            "recorder": (
                "recording_id 去除末尾日期；修复重复日期尾数并应用已知拼写别名"
            ),
            "video_presence": "仅统计云端同一会话目录内至少存在一个 .mp4 的会话",
            "video_content_downloaded": False,
        },
        "totals": {
            "recorder_count": len(recorders),
            "video_session_count": len(video_sessions),
            "duration_s": round(total_duration_s, 2),
            "duration_hours": round_hours(total_duration_s),
            "meta_file_count": len(meta_files),
            "downloaded_meta_count": len(metadata),
            "mp4_file_count": len(mp4_files),
            "metadata_only_session_count": len(metadata_only),
            "unknown_video_status_session_count": len(unknown_video_status),
            "video_without_meta_session_count": len(video_without_meta),
        },
        "daily": daily,
        "recorders": recorders,
        "data_quality": {
            "metadata_only_sessions": metadata_only,
            "video_without_meta_sessions": video_without_meta,
            "unknown_video_status_sessions": unknown_video_status,
            "video_listing_failures": sorted(
                video_listing_failures, key=lambda item: item["path"]
            ),
            "download_failures": sorted(failures, key=lambda item: item["path"]),
            "parse_failures": parse_failures,
        },
        "sessions": sessions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="统计百度网盘 /game-data；只下载 meta.json，不下载视频"
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="复用上次报告中的 meta.json 数据（默认重新下载以确保最新）",
    )
    args = parser.parse_args()

    try:
        access_token = load_token(load_keys())["access_token"]
        meta_files, mp4_files, video_listing_failures = discover_game_data_files(
            access_token
        )
        if not meta_files:
            raise RuntimeError(
                "/game-data 中未发现 meta.json；为避免覆盖旧报告，已中止统计"
            )
        metadata = load_cached_metadata(meta_files) if args.use_cache else None
        failures: list[dict[str, str]] = []
        if metadata is None:
            links = file_download_links(access_token, meta_files)
            metadata, failures = download_all_meta(access_token, meta_files, links)
        report = build_report(
            meta_files,
            mp4_files,
            metadata,
            failures,
            video_listing_failures,
        )
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

        totals = report["totals"]
        print(
            f"录制员 {totals['recorder_count']} 人，"
            f"有视频会话 {totals['video_session_count']} 个，"
            f"总时长 {totals['duration_hours']:.3f} 小时"
        )
        for day in report["daily"]:
            print(
                f"{day['date']}：{day['duration_hours']:.3f} 小时，"
                f"{day['sessions']} 个会话，{day['recorder_count']} 名录制员"
            )
        print(f"报告：{REPORT_PATH}")
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
