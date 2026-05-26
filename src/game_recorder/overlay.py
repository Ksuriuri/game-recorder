"""Small Windows status overlay for recording state."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import queue
import threading
import time
from typing import Literal

logger = logging.getLogger(__name__)

Command = tuple[Literal["recording"], bool] | tuple[Literal["stop"]]

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080


class RecordingStatusOverlay:
    """Threaded, click-through overlay showing idle/recording state."""

    def __init__(self, idle_hint: str, recording_hint: str) -> None:
        self._idle_hint = idle_hint
        self._recording_hint = recording_hint
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

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._commands.put(("stop",))
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:
            logger.warning("Status overlay unavailable: tkinter import failed: %s", exc)
            self._ready.set()
            return

        root = tk.Tk()
        root.title("Game Recorder Status")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg="#181818")

        canvas = tk.Canvas(
            root,
            width=220,
            height=74,
            bg="#181818",
            highlightthickness=2,
            highlightbackground="#ffffff",
        )
        canvas.pack()

        root.update_idletasks()
        self._position(root)

        hwnd = wt.HWND(root.winfo_id())
        self._make_tool_window(hwnd)

        root.deiconify()
        self._ready.set()
        recording_started_at: float | None = None

        def apply_recording(recording: bool) -> None:
            nonlocal recording_started_at
            if recording:
                recording_started_at = time.monotonic()
            else:
                recording_started_at = None
            update_status_text()

        def update_status_text() -> None:
            canvas.delete("all")
            if recording_started_at is None:
                canvas.create_text(
                    110,
                    24,
                    text="未开始录制",
                    fill="#ffffff",
                    font=("Microsoft YaHei UI", 14, "bold"),
                )
                canvas.create_text(
                    110,
                    52,
                    text=self._idle_hint,
                    fill="#d8d8d8",
                    font=("Microsoft YaHei UI", 10, "bold"),
                )
            else:
                elapsed_s = max(0, int(time.monotonic() - recording_started_at))
                canvas.create_text(
                    110,
                    18,
                    text="正在录制",
                    fill="#ff5a5a",
                    font=("Microsoft YaHei UI", 13, "bold"),
                )
                canvas.create_text(
                    110,
                    42,
                    text=f"已录制 {self._format_duration(elapsed_s)}",
                    fill="#ffffff",
                    font=("Microsoft YaHei UI", 12, "bold"),
                )
                canvas.create_text(
                    110,
                    62,
                    text=self._recording_hint,
                    fill="#d8d8d8",
                    font=("Microsoft YaHei UI", 9, "bold"),
                )
            root.deiconify()
            root.update_idletasks()
            root.attributes("-topmost", True)
            self._position(root)

        def pump() -> None:
            try:
                while True:
                    cmd = self._commands.get_nowait()
                    if cmd[0] == "stop":
                        root.destroy()
                        return
                    if cmd[0] == "recording":
                        apply_recording(cmd[1])
            except queue.Empty:
                pass
            update_status_text()
            root.after(500, pump)

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
    def _make_tool_window(hwnd: wt.HWND) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long

        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd,
            GWL_EXSTYLE,
            ex_style | WS_EX_TOOLWINDOW,
        )

