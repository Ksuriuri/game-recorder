"""CLI entry point for Game Recorder.
Usage:
    game-recorder                  # Start with defaults (30 fps, ./recordings)
    game-recorder --fps 60         # Override frame rate
    game-recorder --output ./data  # Custom output directory
    game-recorder --quality 18     # Higher quality (lower CQ = larger files)

While recording:
    Ctrl+Alt+R — toggle recording on/off
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
from collections.abc import Callable
from pathlib import Path

from game_recorder.config import Config
from game_recorder.overlay import RecordingStatusOverlay
from game_recorder.session import Session

logger = logging.getLogger(__name__)

# ── Hotkey via RegisterHotKey + polling fallback ─────────────────────────────

MOD_ALT = 0x0001
MOD_CTRL = 0x0002
MOD_NOREPEAT = 0x4000
VK_CTRL = 0x11
VK_ALT = 0x12
VK_R = 0x52
HOTKEY_LABEL = "Ctrl+Alt+R"
HOTKEY_MODIFIERS = MOD_CTRL | MOD_ALT | MOD_NOREPEAT
HOTKEY_VK = VK_R
HOTKEY_ID_TOGGLE = 1
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
HOTKEY_DEBOUNCE_SECONDS = 0.5


def _hotkey_listener(
    toggle_cb: Callable[[], None],
    stop_event: threading.Event,
) -> None:
    """Listen for the global toggle hotkey, with polling for fullscreen games."""
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = wt.SHORT

    registered = bool(
        user32.RegisterHotKey(None, HOTKEY_ID_TOGGLE, HOTKEY_MODIFIERS, HOTKEY_VK)
    )
    if not registered:
        logger.warning(
            "Failed to register %s hotkey (already in use?); using polling fallback",
            HOTKEY_LABEL,
        )

    last_toggle_at = 0.0
    prev_poll_down = False

    def _fire_once() -> None:
        nonlocal last_toggle_at
        now = time.monotonic()
        if now - last_toggle_at < HOTKEY_DEBOUNCE_SECONDS:
            return
        last_toggle_at = now
        toggle_cb()

    def _key_down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    msg = wt.MSG()
    try:
        while not stop_event.is_set():
            had_message = bool(user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE))
            if had_message:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID_TOGGLE:
                    _fire_once()

            poll_down = _key_down(VK_CTRL) and _key_down(VK_ALT) and _key_down(HOTKEY_VK)
            if poll_down and not prev_poll_down:
                _fire_once()
            prev_poll_down = poll_down

            if not had_message:
                time.sleep(0.05)
    finally:
        if registered:
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
        "--x264-threads",
        type=int,
        default=2,
        help="CPU threads for libx264 fallback encoding (default: 2)",
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
        default=30,
        help="Mouse-move sample rate in Hz (default: 30)",
    )
    parser.add_argument(
        "--segment-minutes",
        type=float,
        default=0.0,
        help="Auto-save every N minutes into a new mp4 + jsonl pair (default: 0 = disabled, single file)",
    )
    parser.add_argument(
        "--capture-mode",
        choices=("auto", "foreground", "screen"),
        default="auto",
        help=(
            "Video target: auto captures a large foreground borderless game window, "
            "foreground forces the current foreground client area, screen captures the full output "
            "(default: auto)"
        ),
    )
    parser.add_argument(
        "--no-hotkey",
        action="store_true",
        help=f"Disable {HOTKEY_LABEL} toggle hotkey (start recording immediately)",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="Disable the in-game recording status overlay",
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
        from game_recorder.encoder import python_loopback as _pyloop
        from game_recorder.encoder.ffmpeg_pipe import (
            _ffmpeg_has_wasapi_demuxer,
            _list_dshow_devices,
        )

        ff = find_ffmpeg()
        has_wasapi = _ffmpeg_has_wasapi_demuxer(ff)
        has_pyloop = _pyloop.loopback_usable()
        print("FFmpeg:", ff)
        print("FFmpeg WASAPI demuxer (native loopback):", "yes" if has_wasapi else "no")
        print("Python soundcard loopback (default speaker):", "yes" if has_pyloop else "no")
        if has_pyloop:
            print(
                "  → Default zero-config audio path: captures the current Windows default "
                "playback device, no Stereo Mix / VB-CABLE needed."
            )
        elif not has_wasapi:
            print(
                "  → No automatic loopback available on this machine. Either install/repair the "
                "`soundcard` Python package, or enable Stereo Mix / install VB-CABLE and pass "
                "--audio-device."
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
        x264_threads=max(1, args.x264_threads),
        audio_device=args.audio_device,
        mouse_poll_interval_ms=1000.0 / args.mouse_hz,
        segment_seconds=segment_seconds,
        capture_mode=args.capture_mode,
    )

    session: Session | None = None
    session_lock = threading.Lock()
    app_stop = threading.Event()
    overlay: RecordingStatusOverlay | None = None

    if not args.no_overlay:
        overlay = RecordingStatusOverlay(
            idle_hint=f"按 {HOTKEY_LABEL} 开始录制" if not args.no_hotkey else "正在启动录制",
            recording_hint=f"按 {HOTKEY_LABEL} 停止" if not args.no_hotkey else "按 Ctrl+C 停止",
        )
        overlay.start()

    def _toggle() -> None:
        nonlocal session
        with session_lock:
            if session is None:
                new_session = Session(config)
                if overlay is not None:
                    overlay.set_recording(True)
                try:
                    new_session.start()
                except Exception:
                    if overlay is not None:
                        overlay.set_recording(False)
                    raise
                session = new_session
                print(f"\n>>> RECORDING STARTED  [{session.session_id}]")
                if args.no_hotkey:
                    print("    Press Ctrl+C to stop and exit.\n")
                else:
                    print(f"    Press {HOTKEY_LABEL} to stop, Ctrl+C to exit.\n")
            else:
                print("\n>>> STOPPING …")
                session.stop()
                if overlay is not None:
                    overlay.set_recording(False)
                print(f">>> RECORDING SAVED    [{session.session_dir}]\n")
                session = None

    # ── Start ────────────────────────────────────────────────────────────

    print("=" * 60)
    print("  Game Recorder — world model data capture")
    print(f"  FPS: {config.fps}  |  Quality: CQ {config.video_quality}")
    print(f"  Capture: {config.capture_mode}")
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
        print(f"  Hotkey: {HOTKEY_LABEL} to toggle recording")
    if not args.no_overlay:
        print("  Overlay: enabled (top-right status + elapsed time)")
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
                if overlay is not None:
                    overlay.set_recording(False)
                print(f">>> Saved to {session.session_dir}")
        if overlay is not None:
            overlay.stop()
        print("Bye～")


if __name__ == "__main__":
    main()
