"""Discrete 81-bin action space aligned with human action_dist CSV.

translation (9) x rotation (9) = 81 classes. Sampling weights are inverse to
``dense_pct`` so rare human actions are preferred during auto-move.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from game_recorder.auto_move.input_inject import VK_A, VK_D, VK_S, VK_W

TRANSLATIONS: tuple[str, ...] = (
    "none",
    "forward",
    "backward",
    "right",
    "left",
    "forward_right",
    "forward_left",
    "backward_right",
    "backward_left",
)

ROTATIONS: tuple[str, ...] = (
    "none",
    "yaw_right",
    "yaw_left",
    "pitch_up",
    "pitch_down",
    "yaw_right_pitch_up",
    "yaw_right_pitch_down",
    "yaw_left_pitch_up",
    "yaw_left_pitch_down",
)

# Camera-frame velocity (right, forward) for each translation bin.
TRANSLATION_CAM_VEL: dict[str, tuple[float, float]] = {
    "none": (0.0, 0.0),
    "forward": (0.0, 1.0),
    "backward": (0.0, -1.0),
    "right": (1.0, 0.0),
    "left": (-1.0, 0.0),
    "forward_right": (1.0, 1.0),
    "forward_left": (-1.0, 1.0),
    "backward_right": (1.0, -1.0),
    "backward_left": (-1.0, -1.0),
}

TRANSLATION_KEYS: dict[str, frozenset[int]] = {
    "none": frozenset(),
    "forward": frozenset({VK_W}),
    "backward": frozenset({VK_S}),
    "right": frozenset({VK_D}),
    "left": frozenset({VK_A}),
    "forward_right": frozenset({VK_W, VK_D}),
    "forward_left": frozenset({VK_W, VK_A}),
    "backward_right": frozenset({VK_S, VK_D}),
    "backward_left": frozenset({VK_S, VK_A}),
}

# Escape-friendly translations when stuck.
STUCK_TRANSLATIONS: frozenset[str] = frozenset(
    {
        "backward",
        "left",
        "right",
        "backward_left",
        "backward_right",
        "forward_left",
        "forward_right",
    }
)

_DEFAULT_CSV = Path(__file__).resolve().parent / "data" / "action_dist_full.csv"
_EPS = 1e-6
_DEFAULT_MAX_WEIGHT_RATIO = 50.0


@dataclass(frozen=True)
class DiscreteAction:
    action_id: int
    translation: str
    rotation: str
    dense_pct: float
    weight: float


@dataclass(frozen=True)
class ActionCatalog:
    actions: tuple[DiscreteAction, ...]
    by_id: dict[int, DiscreteAction]
    by_pair: dict[tuple[str, str], DiscreteAction]
    # Parallel arrays for weighted sampling (same order as ``actions``).
    weights: tuple[float, ...]

    def __len__(self) -> int:
        return len(self.actions)


def translation_keys(translation: str) -> frozenset[int]:
    try:
        return TRANSLATION_KEYS[translation]
    except KeyError as exc:
        raise KeyError(f"unknown translation {translation!r}") from exc


def rotation_rates(
    rotation: str,
    *,
    yaw_deg_s: float = 25.0,
    pitch_deg_s: float = 10.0,
) -> tuple[float, float]:
    """Map a discrete rotation label to (yaw_deg_s, pitch_deg_s).

    Positive yaw = look right; positive pitch = look down (mouse +y), matching
    typical FPS mouse conventions used by ``apply_action``.
    """
    if rotation not in ROTATIONS:
        raise KeyError(f"unknown rotation {rotation!r}")
    yaw = 0.0
    pitch = 0.0
    if "yaw_right" in rotation:
        yaw = abs(yaw_deg_s)
    elif "yaw_left" in rotation:
        yaw = -abs(yaw_deg_s)
    if "pitch_up" in rotation:
        pitch = -abs(pitch_deg_s)
    elif "pitch_down" in rotation:
        pitch = abs(pitch_deg_s)
    return yaw, pitch


def _normalize_cam_vel(vx: float, vy: float) -> tuple[float, float]:
    norm = math.hypot(vx, vy)
    if norm < 1e-9:
        return 0.0, 0.0
    return vx / norm, vy / norm


def cam_basis_horizontal(
    forward_x: float, forward_y: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return (right_xy, forward_xy) on the horizontal plane."""
    fx, fy = forward_x, forward_y
    norm = math.hypot(fx, fy)
    if norm < 1e-6:
        fx, fy = 0.0, 1.0
    else:
        fx, fy = fx / norm, fy / norm
    # right = rotate forward by -90° around +Z: (fy, -fx)
    rx, ry = fy, -fx
    return (rx, ry), (fx, fy)


def world_vel_for_translation(
    translation: str,
    *,
    forward_x: float,
    forward_y: float,
) -> tuple[float, float]:
    """World-frame horizontal velocity unit for a translation given camera forward."""
    vx_cam, vy_cam = TRANSLATION_CAM_VEL[translation]
    vx_cam, vy_cam = _normalize_cam_vel(vx_cam, vy_cam)
    if vx_cam == 0.0 and vy_cam == 0.0:
        return 0.0, 0.0
    (rx, ry), (fx, fy) = cam_basis_horizontal(forward_x, forward_y)
    return rx * vx_cam + fx * vy_cam, ry * vx_cam + fy * vy_cam


def translation_inward_score(
    translation: str,
    *,
    pos_x: float,
    pos_y: float,
    anchor_x: float,
    anchor_y: float,
    forward_x: float,
    forward_y: float,
) -> float:
    """Dot product of translation world velocity with direction toward anchor.

    Positive => moves closer to the anchor on average.
    """
    to_ax = anchor_x - pos_x
    to_ay = anchor_y - pos_y
    to_norm = math.hypot(to_ax, to_ay)
    if to_norm < 1e-6:
        return 0.0
    to_ax /= to_norm
    to_ay /= to_norm
    wx, wy = world_vel_for_translation(
        translation, forward_x=forward_x, forward_y=forward_y
    )
    return wx * to_ax + wy * to_ay


def nearest_inward_translation(
    *,
    pos_x: float,
    pos_y: float,
    anchor_x: float,
    anchor_y: float,
    forward_x: float,
    forward_y: float,
) -> str:
    """Pick the non-idle translation that best moves toward the anchor."""
    best = "forward"
    best_score = -1e9
    for name in TRANSLATIONS:
        if name == "none":
            continue
        score = translation_inward_score(
            name,
            pos_x=pos_x,
            pos_y=pos_y,
            anchor_x=anchor_x,
            anchor_y=anchor_y,
            forward_x=forward_x,
            forward_y=forward_y,
        )
        if score > best_score:
            best_score = score
            best = name
    return best


def _inverse_weights(
    dense_pcts: list[float],
    *,
    alpha: float,
    max_weight_ratio: float,
) -> list[float]:
    alpha = max(0.0, float(alpha))
    raw = [(max(0.0, p) / 100.0 + _EPS) ** (-alpha) for p in dense_pcts]
    lo = min(raw) if raw else 1.0
    if lo <= 0:
        lo = _EPS
    cap = lo * max(1.0, float(max_weight_ratio))
    return [min(w, cap) for w in raw]


def load_action_catalog(
    csv_path: Path | None = None,
    *,
    alpha: float = 1.0,
    max_weight_ratio: float = _DEFAULT_MAX_WEIGHT_RATIO,
) -> ActionCatalog:
    """Load the 81-bin distribution and compute inverse-frequency weights."""
    path = Path(csv_path) if csv_path is not None else _DEFAULT_CSV
    rows: list[tuple[int, str, str, float]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            action_id = int(row["action_id"])
            translation = str(row["translation"]).strip()
            rotation = str(row["rotation"]).strip()
            dense_pct = float(row["dense_pct"])
            if translation not in TRANSLATION_KEYS:
                raise ValueError(f"unknown translation in CSV: {translation!r}")
            if rotation not in ROTATIONS:
                raise ValueError(f"unknown rotation in CSV: {rotation!r}")
            rows.append((action_id, translation, rotation, dense_pct))

    if len(rows) != 81:
        raise ValueError(f"expected 81 action bins, got {len(rows)} from {path}")

    rows.sort(key=lambda r: r[0])
    weights = _inverse_weights(
        [r[3] for r in rows],
        alpha=alpha,
        max_weight_ratio=max_weight_ratio,
    )
    actions = tuple(
        DiscreteAction(
            action_id=aid,
            translation=tr,
            rotation=rot,
            dense_pct=pct,
            weight=w,
        )
        for (aid, tr, rot, pct), w in zip(rows, weights, strict=True)
    )
    by_id = {a.action_id: a for a in actions}
    by_pair = {(a.translation, a.rotation): a for a in actions}
    return ActionCatalog(
        actions=actions,
        by_id=by_id,
        by_pair=by_pair,
        weights=tuple(a.weight for a in actions),
    )


@lru_cache(maxsize=8)
def default_action_catalog(
    alpha: float = 1.0,
    max_weight_ratio: float = _DEFAULT_MAX_WEIGHT_RATIO,
) -> ActionCatalog:
    return load_action_catalog(alpha=alpha, max_weight_ratio=max_weight_ratio)
