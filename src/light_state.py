"""利用三个手动配置的灯位 ROI 判别单帧亮灯状态。"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
import yaml

from src.camera_reader import CaptureError, read_image, save_image


Color = Literal["RED", "YELLOW", "GREEN"]
State = Literal["RED", "YELLOW", "GREEN", "UNKNOWN"]
COLORS: tuple[Color, ...] = ("RED", "YELLOW", "GREEN")
ANNOTATION_COLORS = {"RED": (70, 90, 255), "YELLOW": (35, 225, 255),
                     "GREEN": (75, 230, 85), "UNKNOWN": (230, 230, 230)}


@dataclass(frozen=True)
class LightConfig:
    roi: tuple[float, float, float, float]
    hue_ranges: tuple[tuple[int, int], ...]
    off_value: float


@dataclass(frozen=True)
class AnalysisConfig:
    color_saturation: int
    white_saturation_max: int
    white_value_min: int
    center_radius: float
    rim_outer_radius: float
    background_inner_radius: float
    background_outer_radius: float
    min_roi_pixels: int
    min_background_pixels: int
    min_rim_color: float
    high_value: int
    min_bright_fraction: float
    min_value_rise: float
    min_local_contrast: float
    bright_fraction_full: float
    value_rise_full: float
    local_contrast_full: float
    score_weights: tuple[float, float, float, float]
    min_score: float
    min_margin: float


@dataclass(frozen=True)
class Config:
    lights: dict[Color, LightConfig]
    analysis: AnalysisConfig


@dataclass(frozen=True)
class LightEvidence:
    score: float = 0.0
    bright_fraction: float = 0.0
    center_value: float = 0.0
    background_value: float = 0.0
    value_rise: float = 0.0
    contrast: float = 0.0
    rim_color: float = 0.0
    eligible: bool = False


@dataclass(frozen=True)
class Detection:
    state: State
    relative_confidence: float
    reason: str
    lights: dict[Color, LightEvidence]


def load_config(path: str | Path) -> Config:
    with Path(path).open(encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file)
    if not isinstance(raw, dict) or set(raw) != {"lights", "analysis"}:
        raise ValueError("配置需要 lights 和 analysis")
    if not isinstance(raw["lights"], dict) or set(raw["lights"]) != set(COLORS):
        raise ValueError("lights 必须配置 RED、YELLOW 和 GREEN")
    lights = {}
    for color in COLORS:
        entry = raw["lights"][color]
        roi = tuple(map(float, entry["roi"]))
        hue_ranges = tuple(tuple(bounds) for bounds in entry["hue_ranges"])
        off_value = float(entry["off_value"])
        if len(roi) != 4:
            raise ValueError(f"{color} ROI 需要 [x, y, 宽, 高]")
        x, y, width, height = roi
        if not (0 <= x < x + width <= 1 and 0 <= y < y + height <= 1):
            raise ValueError(f"{color} ROI 必须位于归一化画面内")
        if not hue_ranges or any(
            len(bounds) != 2 or not (0 <= bounds[0] <= bounds[1] <= 179)
            for bounds in hue_ranges
        ):
            raise ValueError(f"{color} Hue 区间无效，跨零范围需拆成两个区间")
        if not 0 <= off_value <= 255:
            raise ValueError(f"{color} 未点亮亮度必须在 0 到 255 之间")
        lights[color] = LightConfig(roi, hue_ranges, off_value)

    analysis = AnalysisConfig(**raw["analysis"])
    if not (0 < analysis.center_radius < analysis.rim_outer_radius <= 1
            <= analysis.background_inner_radius < analysis.background_outer_radius):
        raise ValueError("中心、外围、背景半径须依次增大")
    if (len(analysis.score_weights) != 4
            or any(not np.isfinite(weight) or weight < 0 for weight in analysis.score_weights)
            or sum(analysis.score_weights) <= 0):
        raise ValueError("需要四个非负评分权重，且权重和须大于 0")
    if not (0 <= analysis.min_bright_fraction <= analysis.bright_fraction_full <= 1
            and analysis.bright_fraction_full > 0
            and 0 < analysis.min_value_rise <= analysis.value_rise_full <= 255
            and 0 < analysis.min_local_contrast <= analysis.local_contrast_full <= 255):
        raise ValueError("高亮占比、亮度增量或局部对比度的评分范围无效")
    if not (0 < analysis.min_rim_color <= 1
            and 0 < analysis.min_score <= 1 and 0 <= analysis.min_margin <= 1
            and analysis.min_roi_pixels > 0 and analysis.min_background_pixels > 0):
        raise ValueError("外围颜色、最低评分、领先差值或最小像素数量无效")
    if not (0 <= analysis.white_saturation_max < analysis.color_saturation <= 255
            and 0 < analysis.high_value <= analysis.white_value_min <= 255):
        raise ValueError("饱和度或高亮亮度阈值无效")
    return Config(lights, analysis)


def roi_pixels(
    roi: tuple[float, float, float, float], shape: tuple[int, ...],
) -> tuple[int, int, int, int]:
    height, width = shape[:2]
    x, y, w, h = roi
    return round(x * width), round(y * height), round((x + w) * width), round((y + h) * height)


def measure_light(frame: np.ndarray, light: LightConfig, params: AnalysisConfig) -> LightEvidence:
    """在椭圆中心、外围和背景环上分别测量发光证据。"""
    x0, y0, x1, y1 = roi_pixels(light.roi, frame.shape)
    rx, ry = (x1 - x0) / 2, (y1 - y0) / 2
    if rx <= 0 or ry <= 0:
        return LightEvidence()
    cx, cy = x0 + rx, y0 + ry
    extent = params.background_outer_radius
    height, width = frame.shape[:2]
    left = max(0, int(np.floor(cx - rx * extent)))
    right = min(width, int(np.ceil(cx + rx * extent)))
    top = max(0, int(np.floor(cy - ry * extent)))
    bottom = min(height, int(np.ceil(cy + ry * extent)))
    y, x = np.ogrid[top:bottom, left:right]
    radius_sq = ((x + 0.5 - cx) / rx) ** 2 + ((y + 0.5 - cy) / ry) ** 2
    center = radius_sq <= params.center_radius ** 2
    rim = (radius_sq > params.center_radius ** 2) & (radius_sq <= params.rim_outer_radius ** 2)
    background = ((radius_sq >= params.background_inner_radius ** 2)
                  & (radius_sq <= params.background_outer_radius ** 2))
    if (np.count_nonzero(center) < params.min_roi_pixels
            or np.count_nonzero(rim) < params.min_roi_pixels
            or np.count_nonzero(background) < params.min_background_pixels):
        return LightEvidence()

    hsv = cv2.cvtColor(frame[top:bottom, left:right], cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    matching_hue = np.zeros_like(hue, dtype=bool)
    for low, high in light.hue_ranges:
        matching_hue |= (hue >= low) & (hue <= high)
    colored = matching_hue & (saturation >= params.color_saturation)
    rim_color = float(np.mean(colored[rim]))
    rim_matches = rim_color >= params.min_rim_color
    white_hot = (saturation <= params.white_saturation_max) & (value >= params.white_value_min)
    bright = (value >= params.high_value) & (colored | (white_hot & rim_matches))
    bright_fraction = float(np.mean(bright[center]))
    center_value = float(np.median(value[center]))
    background_value = float(np.median(value[background]))
    value_rise = center_value - light.off_value
    contrast = center_value - background_value
    normalized = np.clip(
        [bright_fraction / params.bright_fraction_full, value_rise / params.value_rise_full,
         contrast / params.local_contrast_full, rim_color], 0.0, 1.0,
    )
    score = float(np.average(normalized, weights=params.score_weights)) if rim_matches else 0.0
    eligible = (rim_matches and bright_fraction >= params.min_bright_fraction
                and value_rise >= params.min_value_rise and contrast >= params.min_local_contrast
                and score >= params.min_score)
    return LightEvidence(score, bright_fraction, center_value, background_value,
                         value_rise, contrast, rim_color, eligible)


def detect_state(frame: np.ndarray, config: Config) -> Detection:
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8 or not frame.size:
        raise ValueError("识别输入必须是非空 uint8 BGR 三通道图片")

    lights = {color: measure_light(frame, config.lights[color], config.analysis) for color in COLORS}
    eligible = [color for color in COLORS if lights[color].eligible]
    if not eligible:
        return Detection("UNKNOWN", 0.0, "insufficient_evidence", lights)
    if len(eligible) > 1:
        return Detection("UNKNOWN", 0.0, "multiple_lights", lights)

    winner = eligible[0]
    runner_score = max(lights[color].score for color in COLORS if color != winner)
    winner_score = lights[winner].score
    if winner_score - runner_score < config.analysis.min_margin:
        return Detection("UNKNOWN", 0.0, "low_margin", lights)
    return Detection(winner, winner_score * (1 - runner_score), "single_light", lights)


def annotate(frame: np.ndarray, detection: Detection, config: Config) -> np.ndarray:
    height, width = frame.shape[:2]
    canvas = np.full((height + 118, max(320, width), 3), 25, dtype=np.uint8)
    canvas[:height, :width] = frame
    for color in COLORS:
        x0, y0, x1, y1 = roi_pixels(config.lights[color].roi, frame.shape)
        ink = ANNOTATION_COLORS[color]
        cv2.rectangle(canvas[:height, :width], (x0, y0), (x1 - 1, y1 - 1), ink, 1)
        text = f"{color} {detection.lights[color].score:.2f}"
        cv2.putText(canvas, text, (10, height + 46 + COLORS.index(color) * 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, ink, 1, cv2.LINE_AA)

    title = f"{detection.state}   rel. confidence {detection.relative_confidence:.2f}"
    cv2.putText(canvas, title, (10, height + 21), cv2.FONT_HERSHEY_SIMPLEX, 0.52,
                ANNOTATION_COLORS[detection.state], 1, cv2.LINE_AA)
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/traffic_light.yaml"))
    parser.add_argument("--output", required=True, type=Path)
    config_args = parser.parse_args()
    try:
        config = load_config(config_args.config)
        frame = read_image(config_args.image)
        detection = detect_state(frame, config)
        save_image(config_args.output, annotate(frame, detection, config))
    except (CaptureError, cv2.error, OSError, yaml.YAMLError, TypeError, KeyError, ValueError) as exc:
        parser.exit(1, f"识别失败：{exc}\n")
    print(json.dumps(asdict(detection), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
