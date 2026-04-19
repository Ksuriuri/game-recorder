"""Screen capture using DXcam (DXGI Desktop Duplication API).

DXcam provides zero-copy GPU frame capture as NumPy arrays.
At 1080p@30fps this uses ~2-5% of a single CPU core.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable

import dxcam
import numpy as np

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

FrameCallback = Callable[[bytes, int], None]


class ScreenCapture:
    """Captures screen frames via DXGI and delivers them through a callback.

    Parameters
    ----------
    fps:
        Target capture frame rate.
    on_frame:
        ``(frame_bytes, frame_index) -> None`` called for every captured frame.
        *frame_bytes* is raw BGR24 pixel data.
    """

    def __init__(self, fps: int, on_frame: FrameCallback) -> None:
        self.fps = fps
        self.on_frame = on_frame
        self._camera: dxcam.DXCamera | None = None
        self._width: int = 0
        self._height: int = 0

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def run(self, stop_event: threading.Event) -> None:
        """Blocking capture loop — run this in a dedicated thread."""
        self._camera = dxcam.create(output_color="BGR")
        self._width = self._camera.width
        self._height = self._camera.height
        logger.info(
            "Screen capture started: %dx%d @ %d fps", self._width, self._height, self.fps
        )

        self._camera.start(target_fps=self.fps, video_mode=True)
        frame_idx = 0
        frame_interval = 1.0 / self.fps

        try:
            while not stop_event.is_set():
                frame: np.ndarray | None = self._camera.get_latest_frame()
                if frame is not None:
                    self.on_frame(frame.tobytes(), frame_idx)
                    frame_idx += 1
                else:
                    # No new frame yet — brief sleep to avoid busy-wait
                    time.sleep(frame_interval * 0.25)
        finally:
            self._camera.stop()
            logger.info("Screen capture stopped after %d frames", frame_idx)
