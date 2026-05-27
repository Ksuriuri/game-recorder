"""Screen capture using DXcam (DXGI Desktop Duplication API).

DXcam provides zero-copy GPU frame capture as NumPy arrays.
At 1080p@30fps this uses ~2-5% of a single CPU core.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import cv2
import dxcam
import numpy as np

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

FrameCallback = Callable[[bytes, int, int, int], None]


@dataclass(frozen=True)
class CaptureRegion:
    """DXcam capture rectangle in output coordinates."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def as_dxcam_region(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


class ScreenCapture:
    """Captures screen frames via DXGI and delivers them through a callback.

    Parameters
    ----------
    fps:
        Target capture frame rate.
    on_frame:
        ``(frame_bytes, frame_index, width, height) -> None`` called for every captured frame.
        *frame_bytes* is raw BGR24 pixel data.
    """

    def __init__(
        self,
        fps: int,
        on_frame: FrameCallback,
        region: CaptureRegion | None = None,
    ) -> None:
        self.fps = fps
        self.on_frame = on_frame
        self.region = region
        self._camera: dxcam.DXCamera | None = None
        self._width: int = 0
        self._height: int = 0
        self._last_source_size: tuple[int, int] | None = None

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def run(self, stop_event: threading.Event) -> None:
        """Blocking capture loop — run this in a dedicated thread."""
        self._camera = dxcam.create(output_color="BGR")
        if self.region is None:
            self._width = self._camera.width
            self._height = self._camera.height
            dxcam_region = None
        else:
            self._width = self.region.width
            self._height = self.region.height
            dxcam_region = self.region.as_dxcam_region()
        logger.info(
            "屏幕捕获已启动：%dx%d @ %d fps%s",
            self._width,
            self._height,
            self.fps,
            "" if dxcam_region is None else f" 区域={dxcam_region}",
        )

        self._camera.start(target_fps=self.fps, video_mode=True, region=dxcam_region)
        frame_idx = 0
        frame_interval = 1.0 / self.fps

        try:
            while not stop_event.is_set():
                frame: np.ndarray | None = self._camera.get_latest_frame()
                if frame is not None:
                    height, width = frame.shape[:2]
                    if width != self._width or height != self._height:
                        source_size = (width, height)
                        if source_size != self._last_source_size:
                            logger.warning(
                                "屏幕捕获源变为 %dx%d；缩放至 %dx%d",
                                width,
                                height,
                                self._width,
                                self._height,
                            )
                            self._last_source_size = source_size
                        frame = cv2.resize(
                            frame,
                            (self._width, self._height),
                            interpolation=cv2.INTER_LINEAR,
                        )
                        height, width = frame.shape[:2]
                    elif self._last_source_size is not None:
                        logger.info(
                            "屏幕捕获源恢复为 %dx%d",
                            self._width,
                            self._height,
                        )
                        self._last_source_size = None
                    self.on_frame(
                        np.ascontiguousarray(frame).tobytes(),
                        frame_idx,
                        width,
                        height,
                    )
                    frame_idx += 1
                else:
                    # No new frame yet — brief sleep to avoid busy-wait
                    time.sleep(frame_interval * 0.25)
        finally:
            self._camera.stop()
            logger.info("屏幕捕获已停止，共 %d 帧", frame_idx)
