"""Tests for audio fallback chain and persistence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from game_recorder.encoder.ffmpeg_pipe import (
    AudioPlan,
    build_audio_plans,
    dshow_device_rank,
    next_audio_source_after,
)
from game_recorder.storage.audio_fallback import (
    load_audio_fallback,
    mark_audio_source_failed,
    mark_audio_source_ok,
)


def test_dshow_device_rank_prefers_stereo_mix() -> None:
    assert dshow_device_rank("Stereo Mix (Realtek)") < dshow_device_rank("Microphone (Realtek)")
    assert dshow_device_rank("Stereo Mix (Realtek)") < dshow_device_rank("VoiceMeeter Output")


def test_next_audio_source_after() -> None:
    plans = [
        AudioPlan(kind="pyloop", source_id="soundcard:default"),
        AudioPlan(kind="dshow", source_id="dshow:Stereo Mix", dshow_name="Stereo Mix"),
    ]
    assert next_audio_source_after(plans, "soundcard:default") == "dshow:Stereo Mix"
    assert next_audio_source_after(plans, "dshow:Stereo Mix") is None
    assert next_audio_source_after(plans, None) == "soundcard:default"


def test_build_audio_plans_skips_and_prefers() -> None:
    with (
        patch(
            "game_recorder.encoder.ffmpeg_pipe._ffmpeg_has_wasapi_demuxer",
            return_value=False,
        ),
        patch(
            "game_recorder.encoder.ffmpeg_pipe._pyloop.loopback_usable",
            return_value=True,
        ),
        patch(
            "game_recorder.encoder.ffmpeg_pipe.ranked_dshow_devices",
            return_value=["Stereo Mix (Realtek)", "Cable Output"],
        ),
        patch(
            "game_recorder.encoder.ffmpeg_pipe._dshow_device_usable",
            return_value=True,
        ),
    ):
        plans = build_audio_plans(
            "ffmpeg",
            skip_sources=frozenset({"soundcard:default"}),
            prefer_source="dshow:Cable Output",
            probe_dshow=True,
        )
    ids = [p.source_id for p in plans]
    assert ids[0] == "dshow:Cable Output"
    assert "soundcard:default" not in ids
    assert None not in ids
    assert "dshow:Stereo Mix (Realtek)" in ids


def test_build_audio_plans_explicit_device_only() -> None:
    with (
        patch(
            "game_recorder.encoder.ffmpeg_pipe._ffmpeg_has_wasapi_demuxer",
            return_value=True,
        ),
        patch(
            "game_recorder.encoder.ffmpeg_pipe._pyloop.loopback_usable",
            return_value=True,
        ),
    ):
        plans = build_audio_plans(
            "ffmpeg",
            explicit_device="Stereo Mix (Realtek)",
            prefer_source="soundcard:default",
        )
    assert [p.source_id for p in plans] == ["dshow:Stereo Mix (Realtek)"]


def test_build_audio_plans_never_silent() -> None:
    with (
        patch(
            "game_recorder.encoder.ffmpeg_pipe._ffmpeg_has_wasapi_demuxer",
            return_value=False,
        ),
        patch(
            "game_recorder.encoder.ffmpeg_pipe._pyloop.loopback_usable",
            return_value=False,
        ),
        patch(
            "game_recorder.encoder.ffmpeg_pipe.ranked_dshow_devices",
            return_value=[],
        ),
    ):
        plans = build_audio_plans("ffmpeg")
    assert plans == []


def test_audio_fallback_persist(tmp_path: Path) -> None:
    out = tmp_path / "recordings"
    out.mkdir()
    mark_audio_source_failed(
        out,
        "soundcard:default",
        next_preferred="dshow:Stereo Mix",
    )
    state = load_audio_fallback(out)
    assert state.skipped == ["soundcard:default"]
    assert state.preferred == "dshow:Stereo Mix"

    mark_audio_source_ok(out, "dshow:Stereo Mix")
    state = load_audio_fallback(out)
    assert "dshow:Stereo Mix" not in state.skipped
    assert state.preferred == "dshow:Stereo Mix"
    # Successful source should not wipe the skip list for other failures.
    assert "soundcard:default" in state.skipped
