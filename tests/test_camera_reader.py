from pathlib import Path
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from src.camera_reader import CaptureError, FrameSource, save_image


def test_image_source_and_release(tmp_path: Path):
    path = tmp_path / "image.png"
    image = np.zeros((48, 64, 3), dtype=np.uint8)
    save_image(path, image)
    source = FrameSource("image", str(path))
    with source as frames:
        assert np.array_equal(frames.read(), image)
        assert frames.read() is None
    with pytest.raises(CaptureError, match="请先打开输入"):
        source.read()


def test_local_video_decode_and_end_of_file(tmp_path: Path):
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 12, (64, 48))
    assert writer.isOpened()
    try:
        for level in (12, 120, 240):
            writer.write(np.full((48, 64, 3), level, dtype=np.uint8))
    finally:
        writer.release()
    with FrameSource("video", str(path)) as frames:
        for level in (12, 120, 240):
            image = frames.read()
            assert image is not None and image.shape == (48, 64, 3)
            assert abs(float(image.mean()) - level) < 3
        assert frames.read() is None


def test_missing_local_image(tmp_path: Path):
    with pytest.raises(CaptureError, match="无法读取图片"):
        with FrameSource("image", str(tmp_path / "missing.jpg")):
            pytest.fail("不存在的图片被打开")


def test_rtsp_passes_timeout_and_releases_on_error(monkeypatch):
    capture = Mock()
    capture.isOpened.return_value = True
    capture.read.return_value = (False, None)
    opener = Mock(return_value=capture)
    monkeypatch.setattr(cv2, "VideoCapture", opener)

    url = "rtsp://127.0.0.1:8554/test"
    with pytest.raises(CaptureError, match="rtsp 读取失败"):
        with FrameSource("rtsp", url, timeout_ms=725) as source:
            source.read()
    opener.assert_called_once_with(
        url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 725, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 725],
    )
    capture.release.assert_called_once()
