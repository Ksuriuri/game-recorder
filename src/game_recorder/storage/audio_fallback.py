"""Persist audio-source fallback state across cold restarts (cafe / flaky drivers)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

AUDIO_FALLBACK_FILENAME = ".audio_fallback.json"


@dataclass
class AudioFallbackState:
    """Sources that recently caused encoder death, plus the next preferred pick."""

    skipped: list[str] = field(default_factory=list)
    preferred: str | None = None

    def skip_set(self) -> frozenset[str]:
        return frozenset(self.skipped)


def audio_fallback_path(output_dir: Path) -> Path:
    return output_dir / AUDIO_FALLBACK_FILENAME


def load_audio_fallback(output_dir: Path) -> AudioFallbackState:
    path = audio_fallback_path(output_dir)
    if not path.exists():
        return AudioFallbackState()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        skipped_raw = raw.get("skipped") or []
        skipped = [str(x) for x in skipped_raw if isinstance(x, str) and x.strip()]
        preferred = raw.get("preferred")
        preferred_s = str(preferred) if isinstance(preferred, str) and preferred.strip() else None
        return AudioFallbackState(skipped=skipped, preferred=preferred_s)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("读取音频回退状态失败：%s", exc)
        return AudioFallbackState()


def save_audio_fallback(output_dir: Path, state: AudioFallbackState) -> None:
    path = audio_fallback_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {"skipped": list(state.skipped), "preferred": state.preferred}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def clear_audio_fallback(output_dir: Path) -> None:
    path = audio_fallback_path(output_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("删除音频回退状态失败：%s", exc)


def mark_audio_source_failed(
    output_dir: Path,
    failed_source: str | None,
    *,
    next_preferred: str | None,
) -> AudioFallbackState:
    """Blacklist *failed_source* and remember *next_preferred* for the next process."""
    state = load_audio_fallback(output_dir)
    if failed_source:
        if failed_source not in state.skipped:
            state.skipped.append(failed_source)
        if state.preferred == failed_source:
            state.preferred = None
    state.preferred = next_preferred
    save_audio_fallback(output_dir, state)
    logger.info(
        "音频回退：已跳过 %s，下次优先 %s（累计跳过 %d 个）",
        failed_source or "<未知>",
        next_preferred or "<已无更多设备>",
        len(state.skipped),
    )
    return state


def mark_audio_source_ok(output_dir: Path, source: str | None) -> None:
    """After a healthy recording, stop blacklisting this source and prefer it."""
    if not source:
        return
    state = load_audio_fallback(output_dir)
    changed = False
    if source in state.skipped:
        state.skipped = [s for s in state.skipped if s != source]
        changed = True
    if state.preferred != source:
        state.preferred = source
        changed = True
    if not changed:
        return
    save_audio_fallback(output_dir, state)
    logger.info("音频回退：%s 录制正常，已移出跳过列表并设为优先", source)
