import math

import numpy as np
import pytest

from planetracker.detect import detect_optical, detect_radar, size_class
from tests.synth import TYPES, make_optical, make_radar

POSITIONS = [(90, 90), (90, 150), (90, 210), (150, 90), (150, 150), (150, 210)]
AIRCRAFT = [(r, c, *TYPES[name], (i * 37) % 180, "white" if i % 2 else "grey")
            for i, ((r, c), name) in enumerate(zip(POSITIONS, TYPES))]


def _match(dets, r, c):
    best = min(dets, key=lambda d: math.hypot(d.row - r, d.col - c))
    assert math.hypot(best.row - r, best.col - c) < 2, "aircraft not found near its true position"
    return best


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_optical_finds_and_sizes_every_aircraft(seed):
    bands, airside, truth = make_optical(aircraft=AIRCRAFT, seed=seed)
    dets, clear = detect_optical(bands, airside)
    assert clear > 0.95
    assert len(dets) == len(AIRCRAFT)
    for (r, c, L, S) in truth:
        d = _match(dets, r, c)
        true = max(L, S)
        assert abs(d.size_m - true) <= max(0.25 * true, 12), f"{true} m aircraft measured {d.size_m} m"
        # the box surrounds the aircraft centre
        assert d.box_r0 <= r <= d.box_r1 and d.box_c0 <= c <= d.box_c1


def test_white_aircraft_labelled_cloud_by_scl_are_still_found():
    bands, airside, truth = make_optical(aircraft=[(150, 150, 54.1, 55.7, 20, "white")])
    assert (bands["scl"] == 9).any()
    dets, _ = detect_optical(bands, airside)
    assert len(dets) == 1 and dets[0].size_class == "very large"


@pytest.mark.parametrize("seed", range(5))
def test_no_false_alarms_on_empty_airfield(seed):
    bands, airside, _ = make_optical(seed=seed)
    assert detect_optical(bands, airside)[0] == []
    bands, airside, _ = make_radar(seed=seed)
    assert detect_radar(bands, airside)[0] == []


def test_cloud_covered_airfield_reports_low_clear_fraction():
    bands, airside, _ = make_optical(seed=1, clouds=True)
    bands["scl"][60:244, 60:200] = 9
    dets, clear = detect_optical(bands, airside)
    assert dets == [] and clear < 0.3


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_radar_finds_every_aircraft(seed):
    bands, airside, truth = make_radar(aircraft=AIRCRAFT, seed=seed)
    dets, _ = detect_radar(bands, airside)
    assert len(dets) == len(AIRCRAFT)
    for (r, c, L, S) in truth:
        d = _match(dets, r, c)
        assert abs(d.size_m - max(L, S)) < 20  # radar sizes are rough
        assert d.contrast > 10


def test_size_classes():
    assert size_class(54) == "very large"
    assert size_class(40) == "large"
    assert size_class(23) == "medium"
    assert size_class(15) == "small"


def test_no_outline_lowers_confidence():
    bands, airside, _ = make_optical(aircraft=AIRCRAFT[:1])
    with_mask = detect_optical(bands, airside)[0][0].confidence
    without = detect_optical(bands, None)[0]
    assert len(without) >= 1
    assert max(d.confidence for d in without) < with_mask
    assert np.isfinite(with_mask)
