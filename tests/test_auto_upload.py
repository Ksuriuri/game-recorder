"""Tests for post-session auto-upload eligibility."""

from __future__ import annotations

import json
from pathlib import Path

from game_recorder.storage.auto_upload import session_upload_eligibility


def _write_session(tmp: Path, *, duration_s: float, with_camera: bool) -> Path:
    session = tmp / "session_test"
    session.mkdir()
    (session / "meta.json").write_text(
        json.dumps({"duration_s": duration_s, "fps": 30}, ensure_ascii=False),
        encoding="utf-8",
    )
    if with_camera:
        (session / "camera.jsonl").write_text("{}\n", encoding="utf-8")
    return session


def test_eligible_when_long_enough_with_camera(tmp_path: Path) -> None:
    session = _write_session(tmp_path, duration_s=61.0, with_camera=True)
    ok, reason, duration = session_upload_eligibility(session, min_duration_s=60.0)
    assert ok
    assert duration == 61.0
    assert "camera.jsonl" in reason


def test_reject_exactly_one_minute(tmp_path: Path) -> None:
    session = _write_session(tmp_path, duration_s=60.0, with_camera=True)
    ok, reason, _ = session_upload_eligibility(session, min_duration_s=60.0)
    assert not ok
    assert "≤ 60" in reason


def test_reject_missing_camera(tmp_path: Path) -> None:
    session = _write_session(tmp_path, duration_s=120.0, with_camera=False)
    ok, reason, _ = session_upload_eligibility(session, min_duration_s=60.0)
    assert not ok
    assert "camera.jsonl" in reason
