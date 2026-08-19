"""WBench-style multi-turn trajectory paradigms over the 81-bin action space.

``BalancedRadiusPolicy`` samples each action independently, which can only ever
produce WBench's ``progressive`` category. The other five paradigms are
constraints *between* turns — exact reversal, perpendicular turn, four-step
closure, period-1 alternation — so an episode has to be planned as a whole up
front. This module does the planning; the policy consumes it through a queue
and keeps its radius/stuck guards as a preemption layer.

Every WBench navigation turn is modelled as one *varying channel* plus an
optional constant *carrier*:

    channel=translation, carrier=none      W → W → S → S
    channel=rotation,    carrier=none      left → left → right → right
    channel=rotation,    carrier=forward   W+up → W+up → W+down → W+down

The vocabulary is deliberately restricted to what WBench actually uses: four
cardinal translations, four diagonals, and the four simple rotations (it never
commands yaw and pitch at once). The remaining bins stay reachable through the
policy's normal weighted sampling.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from game_recorder.auto_move.action_space import (
    TRANSLATION_CAM_VEL,
    world_vel_for_translation,
)

PARADIGMS: tuple[str, ...] = (
    "repeat",
    "roundtrip",
    "loop",
    "l_shape",
    "zigzag",
    "progressive",
)

# Not a WBench paradigm — every WBench turn commands motion. Real play spends
# stretches standing completely still, which independent sampling only ever
# produces in one 2.5–4.5 s slice, so it is planned as its own episode and kept
# out of ``PARADIGMS`` (and out of the paradigm weights).
PAUSE_PARADIGM = "pause"

# Case counts in the WBench navigation split (158 cases), for reference only.
WBENCH_CASE_COUNTS: dict[str, int] = {
    "roundtrip": 50,
    "progressive": 34,
    "repeat": 23,
    "l_shape": 20,
    "loop": 20,
    "zigzag": 11,
}

# Not WBench's own proportions: the free sampler already produces
# ``progressive`` on its own, so it is held low and the five paradigms that
# cannot arise from independent sampling take the bulk of the mass.
DEFAULT_PARADIGM_WEIGHTS: dict[str, float] = {
    "roundtrip": 0.24,
    "loop": 0.20,
    "l_shape": 0.18,
    "zigzag": 0.16,
    "repeat": 0.10,
    "progressive": 0.12,
}

# Measured over the 601 WBench navigation turns: translation-only 60.6%,
# look-only 33.8%, translation+look 5.7%.
_P_TRANSLATION_CHANNEL = 0.606
_P_CARRIER_GIVEN_ROTATION = 0.145

# Turns per case in WBench: 4 → 51.2%, 2 → 23.2%, 3 → 13.5%, 5/6 → 8.3%.
_TURN_COUNT_WEIGHTS: dict[int, float] = {2: 0.23, 3: 0.14, 4: 0.51, 5: 0.06, 6: 0.06}

CARDINAL_TRANSLATIONS: tuple[str, ...] = ("forward", "backward", "left", "right")
DIAGONAL_TRANSLATIONS: tuple[str, ...] = (
    "forward_left",
    "forward_right",
    "backward_left",
    "backward_right",
)
PARADIGM_TRANSLATIONS: tuple[str, ...] = CARDINAL_TRANSLATIONS + DIAGONAL_TRANSLATIONS
PARADIGM_ROTATIONS: tuple[str, ...] = (
    "yaw_right",
    "yaw_left",
    "pitch_up",
    "pitch_down",
)
YAW_ROTATIONS: tuple[str, ...] = ("yaw_right", "yaw_left")

# Exact opposites — roundtrip / loop closure depends on these being true
# inverses (W↔S, A↔D, left↔right, up↔down).
INVERSE_TRANSLATION: dict[str, str] = {
    "none": "none",
    "forward": "backward",
    "backward": "forward",
    "left": "right",
    "right": "left",
    "forward_left": "backward_right",
    "backward_right": "forward_left",
    "forward_right": "backward_left",
    "backward_left": "forward_right",
}

INVERSE_ROTATION: dict[str, str] = {
    "none": "none",
    "yaw_right": "yaw_left",
    "yaw_left": "yaw_right",
    "pitch_up": "pitch_down",
    "pitch_down": "pitch_up",
    "yaw_right_pitch_up": "yaw_left_pitch_down",
    "yaw_left_pitch_down": "yaw_right_pitch_up",
    "yaw_right_pitch_down": "yaw_left_pitch_up",
    "yaw_left_pitch_up": "yaw_right_pitch_down",
}

# Two tokens are perpendicular when they sit on different axes. ``l_shape``
# needs a perpendicular turn and ``loop`` needs a perpendicular pair to close.
_TRANSLATION_AXIS: dict[str, str] = {
    "forward": "front_back",
    "backward": "front_back",
    "left": "left_right",
    "right": "left_right",
    "forward_left": "diagonal_a",
    "backward_right": "diagonal_a",
    "forward_right": "diagonal_b",
    "backward_left": "diagonal_b",
}

_ROTATION_AXIS: dict[str, str] = {
    "yaw_right": "yaw",
    "yaw_left": "yaw",
    "pitch_up": "pitch",
    "pitch_down": "pitch",
}

# Different axes alone is not enough: "forward" and "forward_right" sit on
# different axes but are only 45° apart, which would close a loop as a
# parallelogram. Pairing within a family keeps the two legs truly orthogonal.
_TRANSLATION_FAMILY: dict[str, str] = {
    **{t: "cardinal" for t in CARDINAL_TRANSLATIONS},
    **{t: "diagonal" for t in DIAGONAL_TRANSLATIONS},
}

_ROTATION_FAMILY: dict[str, str] = {r: "simple" for r in PARADIGM_ROTATIONS}

CHANNELS: tuple[str, ...] = ("translation", "rotation")


@dataclass(frozen=True)
class PlannedTurn:
    translation: str
    rotation: str
    hold_s: float
    turn_index: int


@dataclass(frozen=True)
class Episode:
    paradigm: str
    # Which channel the paradigm structure varies over.
    channel: str
    # Constant translation held across every turn ("none" for most episodes).
    carrier: str
    turns: tuple[PlannedTurn, ...]

    def __len__(self) -> int:
        return len(self.turns)

    @property
    def peak_outbound_units(self) -> float:
        return peak_outbound_units(self.turns)

    @property
    def heading_frozen(self) -> bool:
        """True when no turn commands rotation, so the camera heading holds.

        Always true for translation-channel plans, which makes their world
        displacement exactly predictable instead of a worst-case guess.
        """
        return all(t.rotation == "none" for t in self.turns)

    def with_hold(self, hold_s: float) -> Episode:
        """Same plan at a different per-turn duration."""
        return Episode(
            paradigm=self.paradigm,
            channel=self.channel,
            carrier=self.carrier,
            turns=tuple(
                PlannedTurn(
                    translation=t.translation,
                    rotation=t.rotation,
                    hold_s=float(hold_s),
                    turn_index=t.turn_index,
                )
                for t in self.turns
            ),
        )


def plan_pause(hold_s: float) -> Episode:
    """A single turn of doing nothing at all — no keys held, no camera motion.

    ``peak_outbound_units`` is 0 and ``heading_frozen`` is true, so the policy's
    radius budget leaves the sampled duration alone.
    """
    return Episode(
        paradigm=PAUSE_PARADIGM,
        channel="none",
        carrier="none",
        turns=(
            PlannedTurn(
                translation="none",
                rotation="none",
                hold_s=float(hold_s),
                turn_index=0,
            ),
        ),
    )


@dataclass(frozen=True)
class _Alphabet:
    tokens: tuple[str, ...]
    inverse: Mapping[str, str]
    axis: Mapping[str, str]
    family: Mapping[str, str]

    def perpendicular(self, token: str) -> tuple[str, ...]:
        axis = self.axis[token]
        family = self.family[token]
        return tuple(
            t
            for t in self.tokens
            if self.family[t] == family and self.axis[t] != axis
        )

    def has_perpendicular(self) -> bool:
        return any(self.perpendicular(t) for t in self.tokens)


def _translation_alphabet() -> _Alphabet:
    return _Alphabet(
        tokens=PARADIGM_TRANSLATIONS,
        inverse=INVERSE_TRANSLATION,
        axis=_TRANSLATION_AXIS,
        family=_TRANSLATION_FAMILY,
    )


def _rotation_alphabet(*, allow_pitch: bool) -> _Alphabet:
    tokens = PARADIGM_ROTATIONS if allow_pitch else YAW_ROTATIONS
    return _Alphabet(
        tokens=tokens,
        inverse=INVERSE_ROTATION,
        axis=_ROTATION_AXIS,
        family=_ROTATION_FAMILY,
    )


def peak_outbound_units(turns: Sequence[PlannedTurn]) -> float:
    """Farthest the plan gets from its start, in units of one turn's travel.

    Walks the plan in the camera frame with unit-length steps, so ``W W S S``
    scores 2.0 (out two turns, then back), ``W D S A`` scores ~1.41 (a closed
    square), and a look-only plan scores 0.0. Rotation is ignored, which
    over-estimates curved paths — the value only feeds a conservative radius
    budget, so erring outward is the right direction.
    """
    x = 0.0
    y = 0.0
    peak = 0.0
    for turn in turns:
        vx, vy = TRANSLATION_CAM_VEL.get(turn.translation, (0.0, 0.0))
        norm = math.hypot(vx, vy)
        if norm > 1e-9:
            x += vx / norm
            y += vy / norm
        peak = max(peak, math.hypot(x, y))
    return peak


def prefix_displacements(
    turns: Sequence[PlannedTurn],
    *,
    forward_x: float,
    forward_y: float,
) -> tuple[tuple[float, float], ...]:
    """World displacement after each turn, in units of one turn's travel.

    Only exact while ``Episode.heading_frozen`` holds; a rotating plan curves
    away from these straight segments.
    """
    out: list[tuple[float, float]] = []
    x = 0.0
    y = 0.0
    for turn in turns:
        vx, vy = world_vel_for_translation(
            turn.translation, forward_x=forward_x, forward_y=forward_y
        )
        x += vx
        y += vy
        out.append((x, y))
    return tuple(out)


def max_step_scale(
    displacements: Sequence[tuple[float, float]],
    *,
    offset_x: float,
    offset_y: float,
    limit: float,
) -> float:
    """Largest per-turn travel distance keeping every waypoint within *limit*.

    *offset* is the current position relative to the anchor and *displacements*
    are the cumulative unit steps from ``prefix_displacements``. Solves
    ``|offset + step * scale| <= limit`` per waypoint and takes the tightest.
    """
    if limit <= 0.0:
        return 0.0
    c = offset_x * offset_x + offset_y * offset_y - limit * limit
    if c > 0.0:
        # Already outside the limit; nothing to plan.
        return 0.0
    best = math.inf
    for dx, dy in displacements:
        a = dx * dx + dy * dy
        if a <= 1e-12:
            continue
        b = 2.0 * (offset_x * dx + offset_y * dy)
        root = (-b + math.sqrt(max(0.0, b * b - 4.0 * a * c))) / (2.0 * a)
        best = min(best, max(0.0, root))
    return best


def sample_paradigm(
    rng: random.Random,
    weights: Mapping[str, float] | None = None,
) -> str:
    table = dict(DEFAULT_PARADIGM_WEIGHTS if not weights else weights)
    names = [n for n in PARADIGMS if float(table.get(n, 0.0)) > 0.0]
    if not names:
        return "progressive"
    return rng.choices(names, weights=[float(table[n]) for n in names], k=1)[0]


def _sample_turn_count(rng: random.Random, paradigm: str) -> int:
    if paradigm == "loop":
        return 4
    counts = list(_TURN_COUNT_WEIGHTS)
    if paradigm in ("roundtrip", "zigzag", "l_shape"):
        counts = [n for n in counts if n % 2 == 0]
    return rng.choices(counts, weights=[_TURN_COUNT_WEIGHTS[n] for n in counts], k=1)[0]


def _seq_repeat(rng: random.Random, n: int, alpha: _Alphabet) -> tuple[str, ...]:
    return (rng.choice(alpha.tokens),) * n


def _seq_roundtrip(rng: random.Random, n: int, alpha: _Alphabet) -> tuple[str, ...]:
    half = max(1, n // 2)
    token = rng.choice(alpha.tokens)
    return (token,) * half + (alpha.inverse[token],) * (n - half)


def _seq_loop(rng: random.Random, n: int, alpha: _Alphabet) -> tuple[str, ...]:
    first = rng.choice(alpha.tokens)
    options = alpha.perpendicular(first)
    if not options:
        return _seq_roundtrip(rng, n, alpha)
    second = rng.choice(options)
    cycle = (first, second, alpha.inverse[first], alpha.inverse[second])
    return tuple(cycle[i % 4] for i in range(n))


def _seq_l_shape(rng: random.Random, n: int, alpha: _Alphabet) -> tuple[str, ...]:
    first = rng.choice(alpha.tokens)
    options = alpha.perpendicular(first)
    if not options:
        return _seq_repeat(rng, n, alpha)
    second = rng.choice(options)
    half = max(1, n // 2)
    return (first,) * half + (second,) * (n - half)


def _seq_zigzag(rng: random.Random, n: int, alpha: _Alphabet) -> tuple[str, ...]:
    first = rng.choice(alpha.tokens)
    # WBench shows both flavours: exact reversal (A→D→A→D) and a perpendicular
    # partner (W+left→W+right alternating around a forward carrier).
    options = alpha.perpendicular(first)
    if options and rng.random() >= 0.6:
        second = rng.choice(options)
    else:
        second = alpha.inverse[first]
    return tuple(first if i % 2 == 0 else second for i in range(n))


def _seq_progressive(rng: random.Random, n: int, alpha: _Alphabet) -> tuple[str, ...]:
    out: list[str] = []
    previous: str | None = None
    for _ in range(n):
        options = [t for t in alpha.tokens if t != previous] or list(alpha.tokens)
        previous = rng.choice(options)
        out.append(previous)
    return tuple(out)


_GENERATORS = {
    "repeat": _seq_repeat,
    "roundtrip": _seq_roundtrip,
    "loop": _seq_loop,
    "l_shape": _seq_l_shape,
    "zigzag": _seq_zigzag,
    "progressive": _seq_progressive,
}


def paradigm_sequence(
    paradigm: str,
    *,
    rng: random.Random,
    turn_count: int,
    channel: str = "translation",
    allow_pitch: bool = True,
) -> tuple[str, ...]:
    """Token sequence for *paradigm* over the alphabet of *channel*."""
    if paradigm not in _GENERATORS:
        raise KeyError(f"unknown paradigm {paradigm!r}")
    if channel not in CHANNELS:
        raise KeyError(f"unknown channel {channel!r}")
    alpha = (
        _translation_alphabet()
        if channel == "translation"
        else _rotation_alphabet(allow_pitch=allow_pitch)
    )
    return _GENERATORS[paradigm](rng, max(1, int(turn_count)), alpha)


def _needs_perpendicular(paradigm: str) -> bool:
    return paradigm in ("loop", "l_shape")


def plan_episode(
    *,
    rng: random.Random,
    hold_s: float,
    paradigm: str | None = None,
    channel: str | None = None,
    carrier: str | None = None,
    turn_count: int | None = None,
    paradigm_weights: Mapping[str, float] | None = None,
    allow_pitch: bool = True,
) -> Episode:
    """Plan one paradigm episode, holding every turn for *hold_s* seconds.

    Anything left as ``None`` is sampled: the paradigm from *paradigm_weights*,
    the channel and carrier from the measured WBench turn composition, and the
    turn count from its per-case distribution.
    """
    name = paradigm if paradigm is not None else sample_paradigm(rng, paradigm_weights)
    if name not in _GENERATORS:
        raise KeyError(f"unknown paradigm {name!r}")

    if channel is None:
        channel = (
            "translation" if rng.random() < _P_TRANSLATION_CHANNEL else "rotation"
        )
    if channel not in CHANNELS:
        raise KeyError(f"unknown channel {channel!r}")

    # A yaw-only alphabet has a single axis, so it cannot express a
    # perpendicular turn or a closing 4-cycle — plan those on translations.
    if (
        channel == "rotation"
        and _needs_perpendicular(name)
        and not _rotation_alphabet(allow_pitch=allow_pitch).has_perpendicular()
    ):
        channel = "translation"

    if carrier is None:
        carrier = (
            "forward"
            if channel == "rotation" and rng.random() < _P_CARRIER_GIVEN_ROTATION
            else "none"
        )

    n = turn_count if turn_count is not None else _sample_turn_count(rng, name)
    if name == "loop" and turn_count is None:
        n = 4
    tokens = paradigm_sequence(
        name,
        rng=rng,
        turn_count=n,
        channel=channel,
        allow_pitch=allow_pitch,
    )

    turns = tuple(
        PlannedTurn(
            translation=token if channel == "translation" else carrier,
            rotation="none" if channel == "translation" else token,
            hold_s=float(hold_s),
            turn_index=index,
        )
        for index, token in enumerate(tokens)
    )
    return Episode(paradigm=name, channel=channel, carrier=carrier, turns=turns)
