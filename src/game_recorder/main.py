"""CLI entry point for Game Recorder.
Usage:
    game-recorder                  # Start with defaults (30 fps, ./recordings)
    game-recorder --fps 60         # Override frame rate
    game-recorder --output ./data  # Custom output directory
    game-recorder --quality 18     # Higher quality (lower CQ = larger files)

While recording:
    Double-tap Caps Lock — toggle recording on/off
    Ctrl+C               — stop and exit
"""

from __future__ import annotations

import argparse
import ctypes
import re
import ctypes.wintypes as wt
import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from game_recorder.config import Config
from game_recorder.hotkeys import (
    HOTKEY_DEBOUNCE_SECONDS,
    HOTKEY_HINT,
    HOTKEY_LABEL,
    HOTKEY_SEQUENCE_LENGTH,
    HOTKEY_SEQUENCE_TIMEOUT_SECONDS,
    VK_CAPSLOCK,
)
from game_recorder.capture.window_region import restore_window_focus
from game_recorder.overlay import RecordingStatusOverlay
from game_recorder.process_guard import replace_existing_instance
from game_recorder.relaunch import (
    CONTINUING_ARG,
    relaunch_process,
    schedule_restore_game_focus,
    write_pending_focus,
)
from game_recorder.session import AutoStopReason, Session
from game_recorder.storage.pending_notice import PendingAutoStopNotice, consume_pending_notice, write_pending_notice

logger = logging.getLogger(__name__)

_LEVEL_ZH = {
    "DEBUG": "调试",
    "INFO": "信息",
    "WARNING": "警告",
    "ERROR": "错误",
    "CRITICAL": "严重",
}


class _ChineseLevelFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.levelname = _LEVEL_ZH.get(record.levelname, record.levelname)
        return super().format(record)

# ── Hotkey via key-state polling (double-tap Caps Lock) ─────────────────────


def _hotkey_listener(
    toggle_cb: Callable[[], None],
    stop_event: threading.Event,
) -> None:
    """Listen for double-tap Caps Lock (polling)."""
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = wt.SHORT

    last_toggle_at = 0.0
    seq_count = 0
    seq_started_at = 0.0
    prev_caps = False

    def _fire_once() -> None:
        nonlocal last_toggle_at, seq_count
        now = time.monotonic()
        if now - last_toggle_at < HOTKEY_DEBOUNCE_SECONDS:
            return
        last_toggle_at = now
        seq_count = 0
        toggle_cb()

    def _key_down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    while not stop_event.is_set():
        caps = _key_down(VK_CAPSLOCK)
        now = time.monotonic()

        if seq_count > 0 and now - seq_started_at > HOTKEY_SEQUENCE_TIMEOUT_SECONDS:
            seq_count = 0

        if caps and not prev_caps:
            if seq_count == 0:
                seq_count = 1
                seq_started_at = now
            else:
                seq_count += 1
                if seq_count >= HOTKEY_SEQUENCE_LENGTH:
                    _fire_once()

        prev_caps = caps
        time.sleep(0.05)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="game-recorder",
        description="游戏数据采集：同步录制视频、音频与键鼠操作，用于世界模型训练。",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出")
    parser.add_argument("--fps", type=int, default=30, help="目标捕获帧率（默认：30）")
    parser.add_argument(
        "--output", type=str, default="recordings", help="输出目录（默认：./recordings）"
    )
    parser.add_argument(
        "--recording-id",
        type=str,
        default=None,
        help="录制 ID 前缀，用于会话文件夹与视频/操作日志文件名（字母、数字和连字符 -）",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=23,
        help="视频质量 CQ 值，越小越好（默认：23）",
    )
    parser.add_argument(
        "--x264-threads",
        type=int,
        default=2,
        help="libx264 软件编码使用的 CPU 线程数（默认：2）",
    )
    parser.add_argument(
        "--audio-device",
        type=str,
        default=None,
        help="DirectShow 音频设备名称（默认：自动检测环回）",
    )
    parser.add_argument(
        "--mouse-hz",
        type=float,
        default=30,
        help="鼠标移动采样率 Hz（默认：30）",
    )
    parser.add_argument(
        "--segment-minutes",
        type=float,
        default=0.0,
        help="每 N 分钟自动保存一对 mp4 + jsonl（默认：0 = 关闭，单文件）",
    )
    parser.add_argument(
        "--capture-mode",
        choices=("auto", "foreground", "screen"),
        default="auto",
        help=(
            "视频捕获目标：auto 优先捕获前台无边框游戏窗口，"
            "foreground 强制前台客户区，screen 捕获整屏输出（默认：auto）"
        ),
    )
    parser.add_argument(
        "--no-hotkey",
        action="store_true",
        help=f"禁用 {HOTKEY_LABEL} 切换热键（启动后立即开始录制）",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="禁用游戏内录制状态悬浮窗",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=10.0,
        help=(
            "超过 N 秒未按 WASD，或超过 N 秒 WASD 组合不变且无鼠标移动，"
            "则自动停止（默认：10，0 = 关闭这两项）"
        ),
    )
    parser.add_argument(
        "--frame-drop-stop-after",
        type=float,
        default=10.0,
        help=(
            "丢帧检测滑动窗口宽度（秒）：窗口内丢帧超过容忍上限则自动停止并裁尾；"
            "窗口内轻度丢帧会补写重复帧同步音画（默认：10，0 = 关闭）"
        ),
    )
    parser.add_argument(
        "--frame-drop-max-tolerated",
        type=int,
        default=5,
        help="滑动窗口内允许丢帧数，超过后才触发自动停止（默认：5）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument(
        "--no-gta-camera",
        action="store_true",
        help="禁用 GTA 相机位姿同步（不写 active_session.json / camera.jsonl）",
    )
    parser.add_argument(
        "--list-audio-devices",
        action="store_true",
        help="列出 --audio-device 可用的 DirectShow 设备名，并显示 WASAPI 支持情况后退出",
    )
    parser.add_argument(
        CONTINUING_ARG,
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.recording_id is not None:
        rid = args.recording_id.strip()
        if not rid or not re.fullmatch(r"[A-Za-z0-9-]+", rid):
            parser.error("--recording-id 只能包含字母、数字和连字符 (-) 且不能为空")
        args.recording_id = rid

    if not args.continuing:
        replace_existing_instance()

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
        print("FFmpeg WASAPI 解复用器（原生环回）:", "是" if has_wasapi else "否")
        print("Python soundcard 环回（默认扬声器）:", "是" if has_pyloop else "否")
        if has_pyloop:
            print(
                "  → 默认零配置音频路径：捕获当前 Windows 默认播放设备，"
                "无需 Stereo Mix / VB-CABLE。"
            )
        elif not has_wasapi:
            print(
                "  → 本机无可用自动环回。请安装或修复 `soundcard` Python 包，"
                "或启用 Stereo Mix / 安装 VB-CABLE 并通过 --audio-device 指定。"
            )
        print("DirectShow 设备名（配合 --audio-device 使用）：")
        devs = _list_dshow_devices(ff)
        if not devs:
            print("  （未找到）")
        else:
            for n in devs:
                print(" ", n)
        return

    # Logging
    handler = logging.StreamHandler()
    handler.setFormatter(
        _ChineseLevelFormatter(
            fmt="%(asctime)s  %(levelname)-4s  %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=[handler])

    segment_seconds = max(0, int(round(args.segment_minutes * 60)))

    config = Config(
        fps=args.fps,
        output_dir=Path(args.output),
        recording_id=args.recording_id,
        video_quality=args.quality,
        x264_threads=max(1, args.x264_threads),
        audio_device=args.audio_device,
        mouse_poll_interval_ms=1000.0 / args.mouse_hz,
        segment_seconds=segment_seconds,
        capture_mode=args.capture_mode,
        idle_timeout_s=max(0.0, float(args.idle_timeout)),
        frame_drop_stop_after_s=max(0.0, float(args.frame_drop_stop_after)),
        frame_drop_max_tolerated=max(0, int(args.frame_drop_max_tolerated)),
        gta_camera_sync=not bool(args.no_gta_camera),
    )

    session: Session | None = None
    session_lock = threading.Lock()
    auto_stop_queue: queue.Queue[AutoStopReason] = queue.Queue()
    app_stop = threading.Event()
    relaunch_after_session = False
    pending_game_focus: tuple[int | None, str] | None = None
    overlay: RecordingStatusOverlay | None = None
    overlay_ui_settled = threading.Event()

    _AUTO_STOP_CONSOLE: dict[AutoStopReason, str] = {
        "idle": "由于长时间未移动人物角色，本次录制已自动结束。",
        "stuck": "由于 WASD 按键状态长时间未变化且无鼠标移动，本次录制已自动结束。",
        "forbidden_key": "由于按下了非人物移动的按键或点击了鼠标，本次录制已自动结束。",
        "violent": "由于操作过于剧烈，本次录制已自动结束。",
        "focus_lost": "由于切换到了其他窗口，本次录制已自动结束。",
        "frame_drop": "由于检测到视频丢帧（编码跟不上），本次录制已自动结束。",
        "encoder_failed": "由于视频编码异常中断，本次录制已自动结束。",
    }

    def _restart_line() -> str:
        return (
            f"请使用热键 {HOTKEY_LABEL}（{HOTKEY_HINT}）重新开始录制"
            if not args.no_hotkey
            else "请重新运行录制程序以开始新的录制"
        )

    def _show_pending_auto_stop_notice(pending: PendingAutoStopNotice) -> None:
        extra: str | None = None
        if pending.reason == "frame_drop":
            if pending.saved:
                extra = f"最后 {config.frame_drop_stop_after_s:g} 秒已裁剪"
            elif pending.discarded_short:
                extra = (
                    f"本次有效时长不足 {config.min_recording_duration_s:g} 秒，数据已丢弃"
                )
            else:
                extra = (
                    "建议降低游戏画质/帧率，或关闭占用 CPU 的后台程序后再试"
                )
        elif pending.reason == "encoder_failed":
            extra = (
                "编码可能只写入了开头极短一段；若反复出现，请检查系统音频设备或改用 --audio-device"
            )
            if pending.discarded_short:
                extra = (
                    f"本次有效时长不足 {config.min_recording_duration_s:g} 秒，数据已丢弃。"
                    f"{extra}"
                )
        elif pending.discarded_short:
            extra = (
                f"本次有效时长不足 {config.min_recording_duration_s:g} 秒，数据已丢弃"
            )
        if overlay is not None:
            overlay.show_auto_stop_notice(pending.reason, _restart_line(), extra)
        else:
            print(f"    {_AUTO_STOP_CONSOLE[pending.reason]}")
            if extra:
                print(f"    {extra}")
            print(f"    {_restart_line()}")

    pending_auto_stop = consume_pending_notice(config.output_dir)

    if not args.no_overlay:
        overlay = RecordingStatusOverlay(
            idle_hint=HOTKEY_HINT if not args.no_hotkey else "正在启动录制",
            recording_hint=HOTKEY_HINT if not args.no_hotkey else "按 Ctrl+C 停止",
            recordings_dir=config.output_dir,
            on_quit=app_stop.set,
            ui_settled=overlay_ui_settled,
            expect_auto_stop_notice=pending_auto_stop is not None,
        )
        overlay.start()

    if pending_auto_stop is not None:
        _show_pending_auto_stop_notice(pending_auto_stop)

    def _request_session_relaunch() -> None:
        nonlocal relaunch_after_session
        relaunch_after_session = True
        app_stop.set()

    _AUTO_STOP_REASON_LOG: dict[AutoStopReason, str] = {
        "idle": "长时间未移动人物角色（无 WASD），自动停止录制 …",
        "stuck": "WASD 按键状态长时间未变化且无鼠标移动，自动停止录制 …",
        "forbidden_key": "按下了非人物移动的按键或点击了鼠标，自动停止录制 …",
        "violent": "操作过于剧烈（高频 WASD / 鼠标晃动），自动停止录制 …",
        "focus_lost": "游戏窗口失焦（切换至其他窗口），自动停止录制 …",
        "frame_drop": "检测到视频丢帧（编码跟不上），自动停止录制 …",
        "encoder_failed": "视频编码进程异常退出，自动停止录制 …",
    }

    def _stop_session(
        *,
        reason: str,
    ) -> bool:
        """Stop the active session and finalize files. Returns True if data was kept."""
        nonlocal session, pending_game_focus
        if session is None:
            return False
        target = session.capture_target
        if target is not None and (target.hwnd or target.title):
            pending_game_focus = (target.hwnd, target.title)
        print(f"\n>>> {reason}")
        if overlay is not None:
            overlay.set_recording(False)
        saved = session.stop()
        if saved:
            print(f">>> 录制已保存  [{session.session_dir}]\n")
        else:
            print(
                f">>> 录制时长不足 {config.min_recording_duration_s:g} 秒，"
                "已丢弃本次数据\n"
            )
        session = None
        return saved

    def _finish_manual_session_stop() -> None:
        """Manual stop: save, cold-restart (no red notice in the dying process)."""
        _stop_session(reason="正在停止 …")
        _request_session_relaunch()

    def _finish_auto_session_stop(reason: AutoStopReason) -> None:
        """Auto-stop: save, persist notice, cold-restart; red box shows in the new process."""
        saved = _stop_session(reason=_AUTO_STOP_REASON_LOG[reason])
        write_pending_notice(
            config.output_dir,
            PendingAutoStopNotice(
                reason=reason,
                saved=saved,
                discarded_short=not saved,
            ),
        )
        _request_session_relaunch()

    def _on_auto_stop(reason: AutoStopReason) -> None:
        auto_stop_queue.put(reason)

    def _drain_auto_stop_queue() -> None:
        while True:
            try:
                reason = auto_stop_queue.get_nowait()
            except queue.Empty:
                return
            with session_lock:
                _finish_auto_session_stop(reason)

    def _toggle() -> None:
        nonlocal session
        with session_lock:
            if session is None:
                new_session = Session(config, on_auto_stop=_on_auto_stop)
                try:
                    new_session.start()
                except Exception:
                    if overlay is not None:
                        overlay.set_recording(False)
                    raise
                target = new_session.capture_target
                if overlay is not None:
                    overlay.set_recording(True)
                if target is not None and (target.hwnd or target.title):
                    restore_window_focus(hwnd=target.hwnd, title=target.title)
                session = new_session
                print(f"\n>>> 开始录制  [{session.session_id}]")
                if args.no_hotkey:
                    print("    按 Ctrl+C 停止并退出。\n")
                else:
                    print(f"    按 {HOTKEY_LABEL} 停止，Ctrl+C 退出。\n")
            else:
                _finish_manual_session_stop()

    # ── Start ────────────────────────────────────────────────────────────

    print("=" * 60)
    print("  游戏录制器 — 世界模型数据采集")
    print(f"  帧率: {config.fps}  |  质量: CQ {config.video_quality}")
    print(f"  捕获模式: {config.capture_mode}")
    if config.segment_seconds > 0:
        print(
            f"  自动分段: 每 {config.segment_seconds // 60} 分"
            f"{config.segment_seconds % 60:02d} 秒 "
            f"（{config.fps * config.segment_seconds} 帧）"
        )
    else:
        print("  自动分段: 已关闭（单文件）")
    print(f"  输出目录: {config.output_dir.resolve()}")
    if config.recording_id:
        print(f"  录制 ID: {config.recording_id}")
    if not args.no_hotkey:
        print(f"  热键: {HOTKEY_LABEL}（{HOTKEY_HINT}）切换录制")
    if not args.no_overlay:
        print("  悬浮窗: 已启用（右上角状态 + 已录制时长，点「退出」结束）")
    if config.idle_timeout_s > 0:
        print(f"  空闲自动停止: {config.idle_timeout_s:g} 秒未按 WASD")
        print(
            f"  僵滞自动停止: {config.idle_timeout_s:g} 秒 WASD 状态不变且无鼠标移动"
        )
    if config.min_recording_duration_s > 0:
        print(
            f"  最短有效录制: {config.min_recording_duration_s:g} 秒"
            "（不足则丢弃；空闲/僵滞/窗口切换停止时末尾裁剪后再计）"
        )
    print("  禁止操作: 非 WASD 按键或鼠标点击/滚轮将自动停止录制")
    print("  窗口切换自动停止: 录制中切换至其他窗口将自动结束（末尾裁剪 1 秒）")
    if config.violent_duration_s > 0:
        print(
            f"  剧烈操作自动停止: WASD 或鼠标高频晃动连续"
            f" {config.violent_duration_s:g} 秒"
        )
    if config.frame_drop_stop_after_s > 0:
        print(
            f"  丢帧检测: {config.frame_drop_stop_after_s:g} 秒滑动窗口，"
            f"窗口内丢帧超过 {config.frame_drop_max_tolerated} 帧自动停止并裁尾；"
            f"轻度丢帧补写重复帧同步音画"
        )
    print("  Ctrl+C 退出（无控制台时请用悬浮窗「退出」）")
    print("=" * 60)

    if args.continuing:
        schedule_restore_game_focus(
            config.output_dir,
            ui_settled=overlay_ui_settled if overlay is not None else None,
        )

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

    # Main loop — wait for Ctrl+C or overlay quit
    try:
        while not app_stop.is_set():
            _drain_auto_stop_queue()
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n>>> 收到 Ctrl+C")
        app_stop.set()
    finally:
        app_stop.set()
        _drain_auto_stop_queue()
        with session_lock:
            if session is not None and not relaunch_after_session:
                print(">>> 正在停止当前会话 …")
                saved = session.stop()
                if overlay is not None:
                    overlay.set_recording(False)
                if saved:
                    print(f">>> 已保存至 {session.session_dir}")
                else:
                    print(
                        f">>> 录制时长不足 {config.min_recording_duration_s:g} 秒，"
                        "已丢弃本次数据"
                    )
                session = None
        if overlay is not None:
            overlay.stop()
        if relaunch_after_session:
            if pending_game_focus is not None:
                hwnd, title = pending_game_focus
                write_pending_focus(config.output_dir, hwnd=hwnd, title=title)
            try:
                relaunch_process()
            except OSError:
                print(">>> 冷重启失败，请手动重新运行 run.bat")
                sys.exit(1)
            os._exit(0)
        print("再见～")
        sys.exit(0)


if __name__ == "__main__":
    main()
