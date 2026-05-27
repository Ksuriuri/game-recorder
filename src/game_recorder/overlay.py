"""Small Windows status overlay for recording state."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from game_recorder.storage.library_index import read_totals

logger = logging.getLogger(__name__)

Command = (
    tuple[Literal["recording"], bool]
    | tuple[Literal["stop"]]
    | tuple[Literal["auto_stop_notice"], Literal["idle", "forbidden_key", "violent"], str]
)

_AUTO_STOP_HEADLINES: dict[str, tuple[str, str]] = {
    "idle": ("由于长时间未移动人物角色", "本次录制已自动结束"),
    "forbidden_key": ("检测到按下了非人物移动的按键", "本次录制已自动结束"),
    "violent": ("由于操作过于剧烈", "本次录制已自动结束"),
}

GWL_EXSTYLE = -20
GA_ROOT = 2
WS_EX_TOOLWINDOW = 0x00000080
WDA_EXCLUDEFROMCAPTURE = 0x00000011
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


class RecordingStatusOverlay:
    """Threaded, click-through overlay showing idle/recording state."""

    def __init__(
        self,
        idle_hint: str,
        recording_hint: str,
        recordings_dir: Path | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self._idle_hint = idle_hint
        self._recording_hint = recording_hint
        self._recordings_dir = Path(recordings_dir) if recordings_dir else None
        self._on_quit = on_quit
        self._library_total_s: float = 0.0
        self._commands: queue.Queue[Command] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="status-overlay", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def set_recording(self, recording: bool) -> None:
        if self._thread and self._thread.is_alive():
            self._commands.put(("recording", recording))

    def show_auto_stop_notice(
        self,
        reason: Literal["idle", "forbidden_key", "violent"],
        restart_line: str,
    ) -> None:
        if self._thread and self._thread.is_alive():
            self._commands.put(("auto_stop_notice", reason, restart_line))

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._commands.put(("stop",))
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:
            logger.warning("状态悬浮窗不可用：tkinter 导入失败：%s", exc)
            self._ready.set()
            return

        root = tk.Tk()
        root.title("游戏录制状态")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg="#181818")

        top = tk.Frame(root, bg="#2a2a2a", height=26)
        top.pack(fill="x")
        top.pack_propagate(False)

        quit_btn = tk.Label(
            top,
            text="退出",
            fg="#ff7070",
            bg="#2a2a2a",
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
            padx=8,
            pady=1,
            relief="flat",
            bd=0,
        )
        quit_btn.pack(side="right", padx=4, pady=2)

        canvas = tk.Canvas(
            root,
            width=220,
            height=94,
            bg="#181818",
            highlightthickness=2,
            highlightbackground="#ffffff",
        )
        canvas.pack()

        root.update_idletasks()
        self._position(root)

        root_hwnd = self._top_level_hwnd(wt.HWND(root.winfo_id()))
        self._make_tool_window(root_hwnd)
        self._exclude_from_capture(root_hwnd)

        def _quit_click(_event: object = None) -> None:
            if self._on_quit is not None:
                self._on_quit()

        quit_btn.bind("<Button-1>", _quit_click)

        root.deiconify()
        self._ready.set()
        recording_started_at: float | None = None
        notice_window: tk.Toplevel | None = None

        def dismiss_notice() -> None:
            nonlocal notice_window
            if notice_window is not None:
                try:
                    notice_window.destroy()
                except tk.TclError:
                    pass
                notice_window = None

        def show_auto_stop_notice(
            reason: Literal["idle", "forbidden_key", "violent"],
            restart_line: str,
        ) -> None:
            nonlocal notice_window
            dismiss_notice()
            notice = tk.Toplevel(root)
            notice_window = notice
            notice.overrideredirect(True)
            notice.attributes("-topmost", True)
            notice.configure(bg="#8b0000")
            frame = tk.Frame(notice, bg="#8b0000", padx=28, pady=22)
            frame.pack()
            headline = _AUTO_STOP_HEADLINES[reason]
            lines = (*headline, restart_line)
            for i, line in enumerate(lines):
                tk.Label(
                    frame,
                    text=line,
                    fg="#ffffff" if i < 2 else "#ffe066",
                    bg="#8b0000",
                    font=("Microsoft YaHei UI", 16 if i == 0 else 15, "bold"),
                    wraplength=560,
                    justify="center",
                ).pack(pady=(0, 10 if i < 2 else 0))
            notice.update_idletasks()
            w = notice.winfo_reqwidth()
            h = notice.winfo_reqheight()
            x = max(0, (notice.winfo_screenwidth() - w) // 2)
            y = max(0, (notice.winfo_screenheight() - h) // 3)
            notice.geometry(f"{w}x{h}+{x}+{y}")
            notice_hwnd = self._top_level_hwnd(wt.HWND(notice.winfo_id()))
            self._make_tool_window(notice_hwnd)
            self._exclude_from_capture(notice_hwnd)
            notice.deiconify()
            self._raise_topmost(notice_hwnd)

        def apply_recording(recording: bool) -> None:
            nonlocal recording_started_at
            if recording:
                recording_started_at = time.monotonic()
                dismiss_notice()
            else:
                recording_started_at = None
                refresh_library_total()
            update_status_text()

        def refresh_library_total() -> None:
            if self._recordings_dir is None:
                return
            try:
                total_s, _ = read_totals(self._recordings_dir)
                self._library_total_s = total_s
            except Exception as exc:
                logger.debug("读取库累计时长失败：%s", exc)

        def draw_library_total(y: int, *, size: int = 9) -> None:
            if self._recordings_dir is None:
                return
            total_s = max(0, int(round(self._library_total_s)))
            canvas.create_text(
                110,
                y,
                text=f"累计有效视频时长 {self._format_duration(total_s)}",
                fill="#a8c8ff",
                font=("Microsoft YaHei UI", size, "bold"),
            )

        def update_status_text() -> None:
            canvas.delete("all")
            if recording_started_at is None:
                canvas.create_text(
                    110,
                    20,
                    text="未开始录制",
                    fill="#ffffff",
                    font=("Microsoft YaHei UI", 14, "bold"),
                )
                canvas.create_text(
                    110,
                    44,
                    text=self._idle_hint,
                    fill="#d8d8d8",
                    font=("Microsoft YaHei UI", 10, "bold"),
                )
                draw_library_total(72)
            else:
                elapsed_s = max(0, int(time.monotonic() - recording_started_at))
                canvas.create_text(
                    110,
                    16,
                    text="正在录制",
                    fill="#ff5a5a",
                    font=("Microsoft YaHei UI", 13, "bold"),
                )
                canvas.create_text(
                    110,
                    38,
                    text=f"已录制 {self._format_duration(elapsed_s)}",
                    fill="#ffffff",
                    font=("Microsoft YaHei UI", 12, "bold"),
                )
                canvas.create_text(
                    110,
                    58,
                    text=self._recording_hint,
                    fill="#d8d8d8",
                    font=("Microsoft YaHei UI", 9, "bold"),
                )
                draw_library_total(80, size=9)
            root.deiconify()
            root.update_idletasks()
            root.attributes("-topmost", True)
            self._exclude_from_capture(root_hwnd)
            self._raise_topmost(root_hwnd)
            self._position(root)

        def pump() -> None:
            try:
                while True:
                    cmd = self._commands.get_nowait()
                    if cmd[0] == "stop":
                        dismiss_notice()
                        root.destroy()
                        return
                    if cmd[0] == "recording":
                        apply_recording(cmd[1])
                    if cmd[0] == "auto_stop_notice":
                        show_auto_stop_notice(cmd[1], cmd[2])
                    if cmd[0] == "library_ready":
                        update_status_text()
            except queue.Empty:
                pass
            update_status_text()
            if notice_window is not None:
                try:
                    notice_hwnd = self._top_level_hwnd(
                        wt.HWND(notice_window.winfo_id())
                    )
                    self._exclude_from_capture(notice_hwnd)
                    self._raise_topmost(notice_hwnd)
                except tk.TclError:
                    pass
            root.after(500, pump)

        if self._recordings_dir is not None:

            def _bootstrap_library() -> None:
                refresh_library_total()
                self._commands.put(("library_ready",))

            threading.Thread(
                target=_bootstrap_library,
                name="overlay-library-bootstrap",
                daemon=True,
            ).start()

        update_status_text()
        root.after(100, pump)
        root.mainloop()

    @staticmethod
    def _position(root: object) -> None:
        width = root.winfo_reqwidth()
        height = root.winfo_reqheight()
        x = max(0, root.winfo_screenwidth() - width - 24)
        y = 24
        root.geometry(f"{width}x{height}+{x}+{y}")

    @staticmethod
    def _format_duration(total_seconds: int) -> str:
        hours, rem = divmod(total_seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _user32() -> ctypes.WinDLL:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        user32.GetAncestor.restype = wt.HWND
        return user32

    @classmethod
    def _top_level_hwnd(cls, hwnd: wt.HWND) -> wt.HWND:
        """Tk ``winfo_id()`` is often a child HWND; Win32 affinity needs the root."""
        user32 = cls._user32()
        root = user32.GetAncestor(hwnd, GA_ROOT)
        return root if root else hwnd

    @classmethod
    def _make_tool_window(cls, hwnd: wt.HWND) -> None:
        hwnd = cls._top_level_hwnd(hwnd)
        user32 = cls._user32()
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd,
            GWL_EXSTYLE,
            ex_style | WS_EX_TOOLWINDOW,
        )

    @classmethod
    def _exclude_from_capture(cls, hwnd: wt.HWND) -> None:
        """Keep overlay visible on the monitor but out of DXGI/screen capture."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not hasattr(user32, "SetWindowDisplayAffinity"):
            logger.warning("SetWindowDisplayAffinity 不可用，悬浮窗会出现在录制画面中")
            return
        hwnd = cls._top_level_hwnd(hwnd)
        user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
        user32.SetWindowDisplayAffinity.restype = wt.BOOL
        user32.GetWindowDisplayAffinity.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        user32.GetWindowDisplayAffinity.restype = wt.BOOL
        if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            err = ctypes.get_last_error()
            logger.warning(
                "悬浮窗无法从录屏中排除 (error=%s)，录制画面可能包含右上角状态窗",
                err,
            )
            return
        affinity = wt.DWORD()
        if (
            user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
            and affinity.value != WDA_EXCLUDEFROMCAPTURE
        ):
            logger.warning(
                "悬浮窗录屏排除未生效 (affinity=0x%x)",
                affinity.value,
            )

    @classmethod
    def _raise_topmost(cls, hwnd: wt.HWND) -> None:
        user32 = cls._user32()
        user32.SetWindowPos.argtypes = [
            wt.HWND,
            wt.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wt.UINT,
        ]
        user32.SetWindowPos.restype = wt.BOOL
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )

