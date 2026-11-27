"""无 GUI 的图片与视频输入。"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Literal

import cv2
import numpy as np


SourceKind = Literal["image", "video", "usb", "rtsp"]


class CaptureError(RuntimeError):
    """表示输入无法打开、读取或保存。"""


class FrameSource:
    """上下文管理器。图片读取一次，本地视频读到末尾时返回 None。"""

    def __init__(self, kind: SourceKind, source: str, timeout_ms: int = 3000):
        if timeout_ms <= 0:
            raise ValueError("超时必须为正数")
        self.kind = kind
        self.source = source
        self.timeout_ms = timeout_ms
        self._capture: cv2.VideoCapture | None = None
        self._image: np.ndarray | None = None
        self._opened = False

    def __enter__(self) -> "FrameSource":
        if self._opened:
            raise CaptureError("输入已打开")
        try:
            if self.kind == "image":
                self._image = read_image(self.source)
            elif self.kind == "usb":
                if self.source.isdecimal():
                    device = int(self.source)
                elif re.fullmatch(r"/dev/video[0-9]+", self.source):
                    device = self.source
                else:
                    raise ValueError("USB 输入必须是设备编号或 /dev/videoN")
                self._capture = cv2.VideoCapture(device, cv2.CAP_V4L2)
            elif self.kind in ("rtsp", "video"):
                if self.kind == "rtsp" and not self.source.startswith(("rtsp://", "rtsps://")):
                    raise ValueError("RTSP 输入必须使用 rtsp:// 或 rtsps://")
                if self.kind == "video" and not Path(self.source).is_file():
                    raise CaptureError(f"本地视频文件不存在：{self.source}")
                self._capture = cv2.VideoCapture(
                    self.source, cv2.CAP_FFMPEG,
                    [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.timeout_ms,
                     cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.timeout_ms],
                )
            else:
                raise ValueError(f"输入类型无效：{self.kind}")

            if self._capture is not None and not self._capture.isOpened():
                raise CaptureError(f"无法打开 {self.kind} 输入")
        except BaseException:
            self.close()
            raise

        self._opened = True
        return self

    def read(self) -> np.ndarray | None:
        if not self._opened:
            raise CaptureError("请先打开输入")
        if self.kind == "image":
            image, self._image = self._image, None
            return image

        assert self._capture is not None
        success, frame = self._capture.read()
        if success and frame is not None and frame.size:
            return frame
        if self.kind == "video":
            return None
        raise CaptureError(f"{self.kind} 读取失败或超时")

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._image = None
        self._opened = False

    def __exit__(self, *_: object) -> None:
        self.close()


def read_image(path: str | Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR) if Path(path).is_file() else None
    if image is None or not image.size:
        raise CaptureError(f"无法读取图片：{path}")
    return image


def save_image(path: str | Path, image: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if image.size == 0 or not cv2.imwrite(str(path), image):
        raise CaptureError(f"无法保存图片：{path}")
