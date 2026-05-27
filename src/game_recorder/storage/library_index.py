"""Lightweight index of total recorded video duration under the output directory.

Reads ``session_*/meta.json`` (frame counts × fps) — never probes mp4 files.
Maintains ``library.json`` at the output root for fast overlay reads.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from game_recorder.storage.session_writer import SessionMeta

logger = logging.getLogger(__name__)

LIBRARY_FILENAME = "library.json"


@dataclass
class SessionLibraryEntry:
    duration_s: float = 0.0
    video_count: int = 0


@dataclass
class LibraryIndex:
    sessions: dict[str, SessionLibraryEntry] = field(default_factory=dict)

    @property
    def total_duration_s(self) -> float:
        return sum(e.duration_s for e in self.sessions.values())

    @property
    def video_count(self) -> int:
        return sum(e.video_count for e in self.sessions.values())

    def save(self, path: Path) -> None:
        payload = {
            "sessions": {
                sid: {"duration_s": e.duration_s, "video_count": e.video_count}
                for sid, e in self.sessions.items()
            }
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: Path) -> LibraryIndex | None:
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取 %s 失败：%s", path.name, exc)
            return None
        sessions: dict[str, SessionLibraryEntry] = {}
        for sid, entry in (raw.get("sessions") or {}).items():
            if not isinstance(entry, dict):
                continue
            sessions[sid] = SessionLibraryEntry(
                duration_s=float(entry.get("duration_s", 0.0)),
                video_count=int(entry.get("video_count", 0)),
            )
        return cls(sessions=sessions)


def session_video_duration_s(meta: SessionMeta) -> float:
    """Sum per-segment video length from frame counts (matches mp4 content)."""
    if meta.segments:
        fps = max(1, meta.fps)
        return sum(seg.frame_count / fps for seg in meta.segments)
    return meta.duration_s


def effective_duration_s(
    wall_duration_s: float,
    *,
    auto_stop_reason: str | None,
    idle_timeout_s: float,
    violent_duration_s: float = 0.0,
) -> float:
    """Wall/video duration minus auto-stop tail (idle wait or violent-input window)."""
    if auto_stop_reason == "idle" and idle_timeout_s > 0:
        return max(0.0, wall_duration_s - float(idle_timeout_s))
    if auto_stop_reason == "violent" and violent_duration_s > 0:
        return max(0.0, wall_duration_s - float(violent_duration_s))
    return wall_duration_s


def session_library_duration_s(meta: SessionMeta) -> float:
    """Duration credited toward cumulative library totals (overlay, library.json)."""
    wall = session_video_duration_s(meta)
    if meta.idle_tail_trim_frames > 0:
        return wall
    return effective_duration_s(
        wall,
        auto_stop_reason=meta.auto_stop_reason,
        idle_timeout_s=meta.idle_timeout_s,
        violent_duration_s=meta.violent_duration_s,
    )


def session_video_count(meta: SessionMeta) -> int:
    return len(meta.segments)


def library_path(output_dir: Path) -> Path:
    return Path(output_dir) / LIBRARY_FILENAME


def add_session(output_dir: Path, meta: SessionMeta) -> None:
    """Register or update one session in the library index."""
    path = library_path(output_dir)
    index = LibraryIndex.load(path) or LibraryIndex()
    index.sessions[meta.session_id] = SessionLibraryEntry(
        duration_s=round(session_library_duration_s(meta), 2),
        video_count=session_video_count(meta),
    )
    index.save(path)


def _totals_from_meta_raw(raw: dict, *, session_id_fallback: str) -> SessionLibraryEntry:
    fps = max(1, int(raw.get("fps", 30)))
    segments = raw.get("segments") or []
    if segments:
        wall_duration_s = sum(int(s.get("frame_count", 0)) / fps for s in segments)
        video_count = len(segments)
    else:
        wall_duration_s = float(raw.get("duration_s", 0.0))
        video_count = 0
    if int(raw.get("idle_tail_trim_frames", 0)) > 0:
        duration_s = wall_duration_s
    else:
        duration_s = effective_duration_s(
            wall_duration_s,
            auto_stop_reason=raw.get("auto_stop_reason"),
            idle_timeout_s=float(raw.get("idle_timeout_s", 0.0)),
            violent_duration_s=float(raw.get("violent_duration_s", 0.0)),
        )
    sid = raw.get("session_id") or session_id_fallback
    return SessionLibraryEntry(
        duration_s=round(duration_s, 2),
        video_count=video_count,
    ), sid


def rebuild_library_index(output_dir: Path) -> LibraryIndex:
    """Scan all session meta.json files and rewrite the index."""
    output_dir = Path(output_dir)
    index = LibraryIndex()
    for meta_path in sorted(output_dir.glob("session_*/meta.json")):
        try:
            with open(meta_path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("跳过 %s：%s", meta_path, exc)
            continue
        entry, sid = _totals_from_meta_raw(raw, session_id_fallback=meta_path.parent.name)
        index.sessions[sid] = entry
    index.save(library_path(output_dir))
    logger.info(
        "已重建库索引：%.1f 秒，%d 个视频，%d 个会话",
        index.total_duration_s,
        index.video_count,
        len(index.sessions),
    )
    return index


def read_totals(output_dir: Path) -> tuple[float, int]:
    """Return (total_duration_s, video_count); rebuild index if missing."""
    path = library_path(output_dir)
    index = LibraryIndex.load(path)
    if index is None:
        index = rebuild_library_index(output_dir)
    return index.total_duration_s, index.video_count
