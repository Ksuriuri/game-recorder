"""Unit tests for H.264 encoder selection (NVENC → AMF → QSV → libx264)."""

from __future__ import annotations

from unittest.mock import patch

from game_recorder.config import (
    Config,
    hw_encoder_runtime_usable,
    listed_h264_encoders,
    select_h264_encoder,
)
from game_recorder.encoder.ffmpeg_pipe import _video_encoder_args


def _clear_encoder_caches() -> None:
    listed_h264_encoders.cache_clear()
    hw_encoder_runtime_usable.cache_clear()
    select_h264_encoder.cache_clear()


def test_select_prefers_nvenc_when_usable() -> None:
    _clear_encoder_caches()
    with (
        patch(
            "game_recorder.config.listed_h264_encoders",
            return_value=frozenset({"h264_nvenc", "h264_amf", "h264_qsv", "libx264"}),
        ),
        patch(
            "game_recorder.config.hw_encoder_runtime_usable",
            side_effect=lambda _ff, enc: enc == "h264_nvenc",
        ),
    ):
        assert select_h264_encoder("ffmpeg") == "h264_nvenc"


def test_select_falls_back_to_amf_then_qsv() -> None:
    _clear_encoder_caches()
    with (
        patch(
            "game_recorder.config.listed_h264_encoders",
            return_value=frozenset({"h264_nvenc", "h264_amf", "h264_qsv", "libx264"}),
        ),
        patch(
            "game_recorder.config.hw_encoder_runtime_usable",
            side_effect=lambda _ff, enc: enc == "h264_amf",
        ),
    ):
        assert select_h264_encoder("ffmpeg") == "h264_amf"

    _clear_encoder_caches()
    with (
        patch(
            "game_recorder.config.listed_h264_encoders",
            return_value=frozenset({"h264_nvenc", "h264_amf", "h264_qsv", "libx264"}),
        ),
        patch(
            "game_recorder.config.hw_encoder_runtime_usable",
            side_effect=lambda _ff, enc: enc == "h264_qsv",
        ),
    ):
        assert select_h264_encoder("ffmpeg") == "h264_qsv"


def test_select_software_when_no_hw() -> None:
    _clear_encoder_caches()
    with (
        patch(
            "game_recorder.config.listed_h264_encoders",
            return_value=frozenset({"libx264"}),
        ),
        patch(
            "game_recorder.config.hw_encoder_runtime_usable",
            return_value=False,
        ),
    ):
        assert select_h264_encoder("ffmpeg") == "libx264"


def test_video_encoder_args_cover_all_backends(tmp_path) -> None:
    cfg = Config(
        output_dir=tmp_path,
        video_quality=23,
        video_preset="p4",
        x264_threads=2,
        auto_move=False,
    )
    assert _video_encoder_args("h264_nvenc", cfg)[:2] == ["-c:v", "h264_nvenc"]
    assert "-cq" in _video_encoder_args("h264_nvenc", cfg)

    amf = _video_encoder_args("h264_amf", cfg)
    assert amf[:2] == ["-c:v", "h264_amf"]
    assert "-qp_i" in amf and "-qp_p" in amf

    qsv = _video_encoder_args("h264_qsv", cfg)
    assert qsv[:2] == ["-c:v", "h264_qsv"]
    assert "-global_quality" in qsv

    soft = _video_encoder_args("libx264", cfg)
    assert soft[:2] == ["-c:v", "libx264"]
    assert "-threads" in soft
