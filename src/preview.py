"""Show the existing fixed-ROI detector on video without motion interfaces."""

from __future__ import annotations

import argparse
import csv
from contextlib import ExitStack
import os
from pathlib import Path
import sys
from time import monotonic, sleep

import cv2

from src.camera_reader import CaptureError, FrameSource
from src.light_state import annotate, detect_state, load_config


WINDOW = "Traffic light | fixed ROI"


def preview(args):
    if not args.no_window and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        raise CaptureError("DISPLAY 未设置，请在 MobaXterm 启用 X11 转发，或使用 --no-window。")
    config = load_config(args.config)
    frames = 0
    counts = dict.fromkeys(("RED", "YELLOW", "GREEN", "UNKNOWN"), 0)
    start = monotonic()
    with ExitStack() as stack:
        source = stack.enter_context(FrameSource(args.kind, args.source, args.timeout_ms))
        if args.csv:
            args.csv.parent.mkdir(parents=True, exist_ok=True)
            stream = stack.enter_context(args.csv.open("x", newline="", encoding="utf-8"))
            writer = csv.writer(stream)
            writer.writerow(("frame", "elapsed_s", "state", "relative_confidence", "fps"))
        if not args.no_window:
            cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
            stack.callback(cv2.destroyAllWindows)
        while (args.frames == 0 or frames < args.frames) and monotonic() - start < args.seconds:
            frame = source.read()
            if frame is None:
                break
            result = detect_state(frame, config)
            counts[result.state] += 1
            frames += 1
            elapsed = monotonic() - start
            fps = frames / max(elapsed, 0.001)
            if args.csv:
                writer.writerow((frames, f"{elapsed:.3f}", result.state,
                                 f"{result.relative_confidence:.3f}", f"{fps:.2f}"))
            if not args.no_window:
                canvas = annotate(frame, result, config)
                cv2.putText(canvas, f"{fps:.1f} FPS", (10, 24), cv2.FONT_HERSHEY_SIMPLEX,
                            0.65, (255, 255, 255), 2, cv2.LINE_AA)
                cv2.imshow(WINDOW, canvas)
                delay = 2500 if args.kind == "image" else (30 if args.kind == "video" else 1)
                if cv2.waitKey(delay) & 0xff in (27, ord("q")):
                    break
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
    summary = " ".join(f"{state}={count}" for state, count in counts.items())
    print(f"{frames} frames | {frames / max(monotonic() - start, 0.001):.1f} FPS | {summary}")
    return 0 if frames else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("image", "video", "usb", "rtsp"), required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/traffic_light.yaml"))
    parser.add_argument("--no-window", action="store_true")
    parser.add_argument("--csv", type=Path, help="Write a new CSV file")
    parser.add_argument("--frames", type=int, default=0, help="Frame limit; 0 means unlimited")
    parser.add_argument("--seconds", type=float, default=60, help="Session time limit")
    parser.add_argument("--timeout-ms", type=int, default=3000)
    args = parser.parse_args()
    if args.frames < 0 or not 0 < args.seconds < float("inf") or args.timeout_ms <= 0:
        parser.error("帧数不得为负，时间限制及超时须为正数。")
    try:
        return preview(args)
    except (CaptureError, OSError, ValueError, cv2.error) as error:
        sys.exit(f"Preview stopped: {error}")


if __name__ == "__main__":
    sys.exit(main())
