"""生成带已知亮灯标签的模拟图片。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path

import cv2
import numpy as np

from src.camera_reader import save_image
from src.light_state import COLORS, Color, State


IMAGE_SIZE = 320
CENTERS = ((160, 80), (160, 160), (160, 240))


@dataclass(frozen=True)
class Scenario:
    name: str
    lit: tuple[Color, ...]
    ambient: int = 72
    exposure: float = 1.0
    reflection: bool = False
    noise: float = 0.0
    blur: bool = False
    clipped_core: bool = False
    distractors: bool = False
    visible: bool = True
    weak_light: bool = False

    @property
    def expected(self) -> State:
        return self.lit[0] if len(self.lit) == 1 and self.visible and not self.weak_light else "UNKNOWN"


SCENARIOS = (
    Scenario("red", ("RED",)),
    Scenario("yellow", ("YELLOW",)),
    Scenario("green", ("GREEN",)),
    Scenario("off", ()),
    Scenario("red_yellow", ("RED", "YELLOW")),
    Scenario("red_green", ("RED", "GREEN")),
    Scenario("yellow_green", ("YELLOW", "GREEN")),
    Scenario("all_on", COLORS),
    Scenario("dark_red", ("RED",), ambient=22, exposure=0.82, noise=2),
    Scenario("dark_green", ("GREEN",), ambient=28, exposure=0.81, noise=3),
    Scenario("bright_yellow", ("YELLOW",), ambient=155, exposure=1.25),
    Scenario("bright_off", (), ambient=145, exposure=1.35),
    Scenario("off_reflection", (), reflection=True, blur=True, noise=3),
    Scenario("green_reflection", ("GREEN",), reflection=True, blur=True, noise=4),
    Scenario("red_white_core", ("RED",), exposure=1.24, clipped_core=True, blur=True),
    Scenario("yellow_white_core", ("YELLOW",), exposure=1.20, clipped_core=True, noise=2),
    Scenario("green_white_core", ("GREEN",), exposure=1.22, clipped_core=True),
    Scenario("red_distractors", ("RED",), noise=7, blur=True, distractors=True),
    Scenario("off_distractors", (), noise=6, blur=True, distractors=True),
    Scenario("no_target", (), ambient=125, distractors=True, visible=False),
    Scenario("weak_red", ("RED",), ambient=66, weak_light=True, noise=2),
)


def render(scenario: Scenario, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    y, x = np.ogrid[:IMAGE_SIZE, :IMAGE_SIZE]
    gradient = 0.07 * (x - 160) + 0.04 * (y - 160)
    background = np.clip(scenario.ambient + rng.integers(-8, 9) + gradient, 0, 255).astype(np.uint8)
    hsv = np.zeros((IMAGE_SIZE, IMAGE_SIZE, 3), dtype=np.uint8)
    hsv[:, :, 0] = rng.integers(8, 125)
    hsv[:, :, 1] = rng.integers(12, 55)
    hsv[:, :, 2] = background

    if scenario.distractors:
        cv2.circle(hsv, (44, 77), 24, (178, 220, 250), -1)
        cv2.rectangle(hsv, (243, 111), (294, 164), (27, 220, 245), -1)
        cv2.ellipse(hsv, (69, 241), (29, 23), 15, 0, 360, (63, 215, 250), -1)

    if scenario.visible:
        housing_value = int(rng.integers(28, 51))
        cv2.rectangle(hsv, (115, 36), (205, 284), (90, 20, housing_value), -1)
        hues = (int(rng.choice((1, 4, 175, 178))), int(rng.integers(24, 32)),
                int(rng.integers(56, 76)))
        for color, hue, (cx, cy) in zip(COLORS, hues, CENTERS):
            cx += int(rng.integers(-2, 3))
            cy += int(rng.integers(-2, 3))
            radius = int(rng.integers(26, 31))
            distance_sq = (x - cx) ** 2 + (y - cy) ** 2
            lens = distance_sq <= radius ** 2
            cv2.circle(hsv, (cx, cy), radius + 3, (0, 0, max(12, housing_value - 12)), -1)
            is_lit = color in scenario.lit
            if scenario.weak_light and is_lit:
                value = int(rng.integers(132, 151))
            elif is_lit:
                value = int(rng.integers(217, 246))
            else:
                value = int(rng.integers(68, 107))
            falloff = 15 * distance_sq[lens] / radius ** 2
            hsv[lens, 0] = hue
            hsv[lens, 1] = int(rng.integers(168, 235))
            hsv[lens, 2] = np.clip(value - falloff, 0, 255).astype(np.uint8)
            if is_lit and scenario.clipped_core:
                cv2.circle(hsv, (cx, cy), int(rng.integers(13, 17)), (0, 7, 253), -1)
            if scenario.reflection:
                cv2.ellipse(hsv, (cx - 6, cy - 5), (5, 9), 38, 0, 360, (25, 8, 249), -1)

    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR).astype(np.float32)
    bgr *= scenario.exposure * rng.uniform(0.97, 1.03)
    if scenario.blur:
        bgr = cv2.GaussianBlur(bgr, (5, 5), rng.uniform(0.7, 1.2))
    if scenario.noise:
        bgr += rng.normal(0, scenario.noise, bgr.shape).astype(np.float32)
    return np.clip(bgr, 0, 255).astype(np.uint8)


def generate(output: Path, variants: int, base_seed: int) -> int:
    output.mkdir(parents=True, exist_ok=True)
    samples = []
    for index, scenario in enumerate(SCENARIOS):
        for variant in range(variants):
            seed = base_seed + index * variants + variant
            filename = f"{scenario.name}_{variant:02d}.png"
            save_image(output / filename, render(scenario, seed))
            samples.append({
                "file": filename, "scenario": scenario.name, "seed": seed,
                "actual_lights": list(scenario.lit), "expected": scenario.expected,
            })
    manifest = {"source": "synthetic_opencv_numpy", "samples": samples}
    (output / "labels.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return len(samples)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/simulated"))
    parser.add_argument("--variants", type=int, default=3, help="每种场景的随机变体数")
    parser.add_argument("--seed", type=int, default=202603)
    args = parser.parse_args()
    if args.variants < 1 or args.seed < 0:
        parser.error("--variants 必须大于 0，--seed 不得为负")
    count = generate(args.output, args.variants, args.seed)
    print(f"已生成 {count} 张模拟图片和标签至 {args.output}")


if __name__ == "__main__":
    main()
