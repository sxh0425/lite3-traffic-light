from pathlib import Path

import cv2
import numpy as np
import pytest

from scripts.generate_simulated_data import SCENARIOS, render
from src.light_state import COLORS, annotate, detect_state, load_config


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = [
    pytest.param(scenario, 202603 + index * 3 + variant, id=f"{scenario.name}_{variant:02d}")
    for index, scenario in enumerate(SCENARIOS)
    for variant in range(3)
]


@pytest.fixture(scope="module")
def config():
    return load_config(ROOT / "configs" / "traffic_light.yaml")


@pytest.mark.parametrize(("scenario", "seed"), SAMPLES)
def test_generated_sample(scenario, seed, config):
    frame = render(scenario, seed)
    result = detect_state(frame, config)
    assert result.state == scenario.expected, result
    assert set(result.lights) == set(COLORS)
    assert all(0 <= light.score <= 1 for light in result.lights.values())
    if len(scenario.lit) > 1:
        assert result.reason == "multiple_lights"
    if result.state == "UNKNOWN":
        assert result.relative_confidence == 0
    else:
        assert 0 < result.relative_confidence <= 1
        assert result.lights[result.state].eligible


def test_red_hue_wrap(config):
    frame = render(SCENARIOS[0], 202603)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    for hue in (2, 177):
        alternate = hsv.copy()
        alternate[48:112, 128:192, 0] = hue
        result = detect_state(cv2.cvtColor(alternate, cv2.COLOR_HSV2BGR), config)
        assert result.state == "RED"


def test_white_core_without_colored_rim(config):
    frame = np.full((320, 320, 3), 40, dtype=np.uint8)
    cv2.circle(frame, (160, 80), 28, (255, 255, 255), -1)
    result = detect_state(frame, config)
    assert result.state == "UNKNOWN"
    assert result.lights["RED"].bright_fraction == 0


def test_tiny_frame_has_insufficient_roi(config):
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    assert detect_state(frame, config).state == "UNKNOWN"


def test_annotate_preserves_input(config):
    frame = render(SCENARIOS[2], 202609)
    original = frame.copy()
    result = detect_state(frame, config)
    annotated = annotate(frame, result, config)
    assert np.array_equal(frame, original)
    assert annotated.shape[0] > frame.shape[0]
    assert not np.array_equal(annotated[: frame.shape[0]], frame)
