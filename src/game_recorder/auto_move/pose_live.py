"""Live-tail camera_raw_*.jsonl and normalize poses to a unified frame."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from game_recorder.camera_sync import (
    CP2077_CAMERA_SOURCE,
    GTA_CAMERA_SOURCE,
    RDR2_CAMERA_SOURCE,
    WUKONG_CAMERA_SOURCE,
    CameraSource,
    camera_control_dir,
)

logger = logging.getLogger(__name__)

# Unified world frame used by auto-move policies: +X right, +Y forward, +Z up (meters).
UNIFIED_AXES = "x_right_y_forward_z_up"


@dataclass(frozen=True)
class UnifiedPose:
    """Camera/player observation in the unified meter frame."""

    t_unix_ms: int
    x: float
    y: float
    z: float
    source_key: str
    forward_x: float = 0.0
    forward_y: float = 1.0
    forward_z: float = 0.0

    def horizontal_distance_to(self, other: UnifiedPose) -> float:
        dx = self.x - other.x
        dy = self.y - other.y
        return math.hypot(dx, dy)


def candidate_raw_paths(
    *,
    output_dir: Path,
    session_dir: Path,
    source: CameraSource,
) -> list[Path]:
    """Paths where a plugin may write its camera raw JSONL during a session."""
    filenames = (source.raw_filename, *source.legacy_raw_filenames)
    paths: list[Path] = []
    for name in filenames:
        paths.append(session_dir / name)
    if source.sandbox_install_filename is not None:
        try:
            sandbox = camera_control_dir(output_dir, source)
        except RuntimeError:
            sandbox = None
        if sandbox is not None:
            for name in filenames:
                paths.append(sandbox / name)
    # Deduplicate while preserving order
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _as_float_list(value: Any, *, n: int) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < n:
        return None
    out: list[float] = []
    for item in value[:n]:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            return None
    return out


def _translation_from_matrix(
    matrix: list[float],
    *,
    vector_convention: str,
) -> tuple[float, float, float] | None:
    if len(matrix) < 16:
        return None
    if vector_convention == "column_vector":
        return matrix[3], matrix[7], matrix[11]
    # Default: row-major row-vector (GTA / RDR2 / Wukong): translation in last row.
    return matrix[12], matrix[13], matrix[14]


def _forward_from_matrix(
    matrix: list[float],
    *,
    vector_convention: str,
    world_axes: str,
) -> tuple[float, float, float] | None:
    """Best-effort camera forward in native world axes."""
    if len(matrix) < 16:
        return None
    if vector_convention == "column_vector":
        # CP2077 C2W columns: right, down, forward, position
        return matrix[2], matrix[6], matrix[10]
    # Row-vector C2W rows are often right / forward / up (Rockstar / UE-ish).
    if "x_forward" in world_axes:
        # UE: row0 ≈ forward basis in world
        return matrix[0], matrix[1], matrix[2]
    # Rockstar: row1 ≈ forward
    return matrix[4], matrix[5], matrix[6]


def _to_unified(
    x: float,
    y: float,
    z: float,
    *,
    world_axes: str,
) -> tuple[float, float, float]:
    """Map native world axes into unified (right, forward, up)."""
    axes = (world_axes or "").lower()
    if "x_right_y_forward_z_up" in axes:
        return x, y, z
    if "x_forward_y_right_z_up" in axes:
        # UE: (forward, right, up) → (right, forward, up)
        return y, x, z
    if "x_right_y_down_z_forward" in axes:
        # OpenCV-ish: (right, down, forward) → (right, forward, up)
        return x, z, -y
    # Game-local axes with z_up (e.g. CP2077 x_game_y_game_z_up): keep as-is.
    return x, y, z


def extract_unified_pose(
    sample: dict[str, Any],
    header: dict[str, Any] | None,
    *,
    source_key: str,
) -> UnifiedPose | None:
    """Parse one camera sample into a unified meter pose."""
    header = header or {}
    t_raw = sample.get("t_unix_ms")
    try:
        t_unix_ms = int(t_raw)
    except (TypeError, ValueError):
        return None

    world_axes = str(header.get("world_axes") or "")
    vector_convention = str(header.get("matrix_vector_convention") or "row_vector")

    pos: tuple[float, float, float] | None = None
    for key in ("camera_position_world", "position_world"):
        vals = _as_float_list(sample.get(key), n=3)
        if vals is not None:
            pos = (vals[0], vals[1], vals[2])
            break
    if pos is None:
        matrix = _as_float_list(sample.get("camera_to_world"), n=16)
        if matrix is not None:
            pos = _translation_from_matrix(matrix, vector_convention=vector_convention)
    if pos is None:
        return None

    ux, uy, uz = _to_unified(*pos, world_axes=world_axes)

    fwd: tuple[float, float, float] | None = None
    for key in ("forward_world", "camera_forward_world"):
        vals = _as_float_list(sample.get(key), n=3)
        if vals is not None:
            fwd = (vals[0], vals[1], vals[2])
            break
    if fwd is None:
        matrix = _as_float_list(sample.get("camera_to_world"), n=16)
        if matrix is not None:
            fwd = _forward_from_matrix(
                matrix,
                vector_convention=vector_convention,
                world_axes=world_axes,
            )
    if fwd is None:
        fux, fuy, fuz = 0.0, 1.0, 0.0
    else:
        fux, fuy, fuz = _to_unified(*fwd, world_axes=world_axes)
        norm = math.sqrt(fux * fux + fuy * fuy + fuz * fuz)
        if norm > 1e-6:
            fux, fuy, fuz = fux / norm, fuy / norm, fuz / norm
        else:
            fux, fuy, fuz = 0.0, 1.0, 0.0

    return UnifiedPose(
        t_unix_ms=t_unix_ms,
        x=ux,
        y=uy,
        z=uz,
        source_key=source_key,
        forward_x=fux,
        forward_y=fuy,
        forward_z=fuz,
    )


class LivePoseReader:
    """Poll one or more camera raw JSONL files and expose the latest unified pose."""

    def __init__(
        self,
        *,
        output_dir: Path,
        session_dir: Path,
        sources: Iterable[CameraSource],
        poll_s: float = 0.05,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._session_dir = Path(session_dir)
        self._sources = tuple(sources)
        self._poll_s = max(0.01, float(poll_s))
        self._offsets: dict[Path, int] = {}
        self._headers: dict[str, dict[str, Any]] = {}
        self._latest: UnifiedPose | None = None
        self._active_path: Path | None = None
        self._active_key: str | None = None

    @property
    def latest(self) -> UnifiedPose | None:
        return self._latest

    def poll(self) -> UnifiedPose | None:
        """Read newly appended lines; return the newest pose if any."""
        if self._active_path is None:
            self._try_attach()
        if self._active_path is None or self._active_key is None:
            return self._latest

        path = self._active_path
        key = self._active_key
        try:
            size = path.stat().st_size
        except OSError:
            return self._latest

        offset = self._offsets.get(path, 0)
        if size < offset:
            # File rewritten (session restart) — resync from start.
            offset = 0
            self._headers.pop(key, None)
        if size == offset:
            return self._latest

        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(offset)
                chunk = stream.read()
                new_offset = stream.tell()
        except OSError as exc:
            logger.debug("pose live read failed for %s: %s", path, exc)
            return self._latest

        self._offsets[path] = new_offset
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            rtype = record.get("type")
            if rtype == "header":
                self._headers[key] = record
                continue
            if rtype != "sample":
                continue
            pose = extract_unified_pose(
                record,
                self._headers.get(key),
                source_key=key,
            )
            if pose is not None:
                self._latest = pose
        return self._latest

    def wait_for_pose(self, timeout_s: float = 5.0) -> UnifiedPose | None:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            pose = self.poll()
            if pose is not None:
                return pose
            time.sleep(self._poll_s)
        return None

    def _try_attach(self) -> None:
        for source in self._sources:
            for path in candidate_raw_paths(
                output_dir=self._output_dir,
                session_dir=self._session_dir,
                source=source,
            ):
                if not path.is_file():
                    continue
                self._active_path = path
                self._active_key = source.key
                self._offsets.setdefault(path, 0)
                logger.info("Live pose attached to %s (%s)", path, source.key)
                return


def default_auto_move_sources(
    *,
    gta: bool = True,
    rdr2: bool = True,
    wukong: bool = True,
    cp2077: bool = True,
) -> tuple[CameraSource, ...]:
    sources: list[CameraSource] = []
    if gta:
        sources.append(GTA_CAMERA_SOURCE)
    if rdr2:
        sources.append(RDR2_CAMERA_SOURCE)
    if wukong:
        sources.append(WUKONG_CAMERA_SOURCE)
    if cp2077:
        sources.append(CP2077_CAMERA_SOURCE)
    return tuple(sources)
