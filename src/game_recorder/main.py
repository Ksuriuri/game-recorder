"""CLI entry point for Game Recorder.

Usage:
    game-recorder                  # Start with defaults (30 fps, ./recordings)
    game-recorder --fps 60         # Override frame rate
    game-recorder --output ./data  # Custom output directory
    game-recorder --quality 18     # Higher quality (lower CQ = larger files)

While recording:
    Ctrl+F9   — toggle recording on/off
    Ctrl+C    — stop and exit
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import logging
import sys
import threading
import time
from pathlib import Path

from game_recorder.config import Config
from game_recorder.session import Session

logger = logging.getLogger(__name__)

# ── Hotkey via RegisterHotKey ────────────────────────────────────────────────

MOD_CTRL = 0x0002
VK_F9 = 0x78
HOTKEY_ID_TOGGLE = 1
WM_HOTKEY = 0x0312


def _hotkey_listener(
    toggle_cb: callable,  # type: ignore[valid-type]
    stop_event: threading.Event,
) -> None:
    """Register Ctrl+F9 as a global hotkey and listen for it."""
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    if not user32.RegisterHotKey(None, HOTKEY_ID_TOGGLE, MOD_CTRL, VK_F9):
        logger.warning("Failed to register Ctrl+F9 hotkey (already in use?)")
        return

    msg = wt.MSG()
    try:
        while not stop_event.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID_TOGGLE:
                    toggle_cb()
            else:
                time.sleep(0.05)
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID_TOGGLE)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="game-recorder",
        description="Game data capture: video + audio + keyboard/mouse for world-model training.",
    )
    parser.add_argument("--fps", type=int, default=30, help="Target capture FPS (default: 30)")
    parser.add_argument(
        "--output", type=str, default="recordings", help="Output directory (default: ./recordings)"
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=23,
        help="Video quality — CQ value, lower=better (default: 23)",
    )
    parser.add_argument(
        "--audio-device",
        type=str,
        default=None,
        help="DirectShow audio device name (default: auto-detect loopback)",
    )
    parser.add_argument(
        "--mouse-hz",
        type=float,
        default=200,
        help="Mouse-move sample rate in Hz (default: 200)",
    )
    parser.add_argument(
        "--segment-minutes",
        type=float,
        default=0.0,
        help="Auto-save every N minutes into a new mp4 + jsonl pair (default: 0 = disabled, single file)",
    )
    parser.add_argument(
        "--no-hotkey",
        action="store_true",
        help="Disable Ctrl+F9 toggle hotkey (start recording immediately)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    parser.add_argument(
        "--list-audio-devices",
        action="store_true",
        help="List DirectShow names for --audio-device, show WASAPI support, then exit",
    )
    args = parser.parse_args()

    if args.list_audio_devices:
        from game_recorder.config import find_ffmpeg
        from game_recorder.encoder.ffmpeg_pipe import (
            _ffmpeg_has_wasapi_demuxer,
            _list_dshow_devices,
        )

        ff = find_ffmpeg()
        has_wasapi = _ffmpeg_has_wasapi_demuxer(ff)
        print("FFmpeg:", ff)
        print("WASAPI indev (loopback / default playback):", "yes" if has_wasapi else "no")
        if not has_wasapi:
            print(
                "  Most static Windows builds (incl. BtbN) have no wasapi demuxer; use DirectShow. "
                "For system audio: enable Stereo Mix, or route to VoiceMeeter, or use --audio-device."
            )
        print("DirectShow names (use with --audio-device):")
        devs = _list_dshow_devices(ff)
        if not devs:
            print("  (none found)")
        else:
            for n in devs:
                print(" ", n)
        return

    # Logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    segment_seconds = max(0, int(round(args.segment_minutes * 60)))

    config = Config(
        fps=args.fps,
        output_dir=Path(args.output),
        video_quality=args.quality,
        audio_device=args.audio_device,
        mouse_poll_interval_ms=1000.0 / args.mouse_hz,
        segment_seconds=segment_seconds,
    )

    session: Session | None = None
    session_lock = threading.Lock()
    app_stop = threading.Event()

    def _toggle() -> None:
        nonlocal session
        with session_lock:
            if session is None:
                session = Session(config)
                session.start()
                print(f"\n>>> RECORDING STARTED  [{session.session_id}]")
                print("    Press Ctrl+F9 to stop, Ctrl+C to exit.\n")
            else:
                print("\n>>> STOPPING …")
                session.stop()
                print(f">>> RECORDING SAVED    [{session.session_dir}]\n")
                session = None

    # ── Start ────────────────────────────────────────────────────────────

    print("=" * 60)
    print("  Game Recorder — world model data capture")
    print(f"  FPS: {config.fps}  |  Quality: CQ {config.video_quality}")
    if config.segment_seconds > 0:
        print(
            f"  Auto-save: every {config.segment_seconds // 60}m"
            f"{config.segment_seconds % 60:02d}s "
            f"({config.fps * config.segment_seconds} frames)"
        )
    else:
        print("  Auto-save: disabled (single file)")
    print(f"  Output: {config.output_dir.resolve()}")
    if not args.no_hotkey:
        print("  Hotkey: Ctrl+F9 to toggle recording")
    print("  Ctrl+C to exit")
    print("=" * 60)

    if args.no_hotkey:
        # Immediately start recording
        _toggle()

    # Hotkey listener thread
    hotkey_thread: threading.Thread | None = None
    if not args.no_hotkey:
        hotkey_thread = threading.Thread(
            target=_hotkey_listener,
            args=(_toggle, app_stop),
            name="hotkey",
            daemon=True,
        )
        hotkey_thread.start()

    # Main loop — wait for Ctrl+C
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n>>> Ctrl+C received")
    finally:
        app_stop.set()
        with session_lock:
            if session is not None:
                print(">>> Stopping active session …")
                session.stop()
                print(f">>> Saved to {session.session_dir}")
        print("Bye.")


if __name__ == "__main__":
    main()
