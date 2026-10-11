"""Synthetic Sentinel-1 / Sentinel-2 airport images with aircraft of known size, for tests."""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage

SS = 10  # supersampling (1 m sub-pixels at 10 m pixels)

# B02 B03 B04 B08 reflectance
CONCRETE = (0.17, 0.19, 0.21, 0.25)
ASPHALT = (0.07, 0.08, 0.09, 0.11)
GRASS = (0.04, 0.07, 0.05, 0.30)
WHITE = (0.50, 0.52, 0.53, 0.55)
GREY = (0.08, 0.09, 0.10, 0.11)
ROOF = (0.35, 0.36, 0.38, 0.40)

# name: (length m, wingspan m)
TYPES = {
    "Tu-95": (46.2, 50.0),
    "Tu-160": (54.1, 55.7),
    "Tu-22M3": (42.5, 34.3),
    "C-17": (53.0, 51.8),
    "KC-135": (41.5, 39.9),
    "Su-34": (23.3, 14.7),
}


def aircraft_footprint(length_m: float, span_m: float, heading_deg: float, size_px: int) -> np.ndarray:
    """Fraction of each 10 m pixel covered by a swept-wing aircraft, centred in a size_px square."""
    n = size_px * SS
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    c = n / 2
    th = math.radians(heading_deg)
    ux, uy = math.sin(th), -math.cos(th)
    s = ((xx - c) * ux + (yy - c) * uy) / SS * 10      # metres along fuselage (+ = nose)
    t = ((xx - c) * -uy + (yy - c) * ux) / SS * 10     # metres across
    fus = (np.abs(s) <= length_m / 2) & (np.abs(t) <= max(length_m / 18, 1.5))
    # swept wing: leading edge from root at s=+0.12 L back to the tips
    root_s = 0.12 * length_m
    sweep = 0.35 * (span_m / 2)
    le = root_s - np.abs(t) / (span_m / 2) * sweep
    chord = 0.20 * length_m * (1 - 0.6 * np.abs(t) / (span_m / 2))
    wing = (np.abs(t) <= span_m / 2) & (s <= le) & (s >= le - chord)
    tail_t = span_m * 0.18
    tle = -0.38 * length_m - np.abs(t) / tail_t * 4
    tail = (np.abs(t) <= tail_t) & (s <= tle) & (s >= tle - 0.08 * length_m)
    shape = (fus | wing | tail).astype(np.float32)
    return shape.reshape(size_px, SS, size_px, SS).mean(axis=(1, 3))


def _paint(img, mask_frac, r, c, colour):
    size = mask_frac.shape[0]
    r0, c0 = r - size // 2, c - size // 2
    for k in range(4):
        sub = img[k, r0:r0 + size, c0:c0 + size]
        sub[:] = sub * (1 - mask_frac) + colour[k] * mask_frac


def make_optical(shape=(300, 300), aircraft=(), seed=0, apron=(60, 240, 60, 240), clouds=False):
    """Return (bands dict, airside mask, truth list).

    aircraft: (row, col, length_m, span_m, heading_deg, colour) with colour "white" or "grey".
    """
    rng = np.random.default_rng(seed)
    H, W = shape
    img = np.empty((4, H, W), np.float32)
    for k in range(4):
        img[k] = GRASS[k]
    r0, r1, c0, c1 = apron
    for k in range(4):
        img[k, r0:r1, c0:c1] = CONCRETE[k]
        img[k, r1:r1 + 4, c0:c1] = ASPHALT[k]   # taxiway along the apron
    # buildings outside the airside area
    for (br, bc) in ((20, 30), (30, 270), (270, 40)):
        for k in range(4):
            img[k, br:br + 6, bc:bc + 9] = ROOF[k]
    truth = []
    for (r, c, L, S, hd, colour) in aircraft:
        size = int(max(L, S) / 10 * 1.6) + 8
        size += size % 2
        fp = aircraft_footprint(L, S, hd, size)
        # shadow, a few metres to the north-west (sun in the south-east)
        shadow = ndimage.shift(fp, (-0.4, -0.3), order=1) * 0.7
        _paint(img, np.clip(shadow - fp, 0, 1), r, c, (0.05, 0.05, 0.06, 0.07))
        _paint(img, fp, r, c, WHITE if colour == "white" else GREY)
        truth.append((r - 0.5, c - 0.5, L, S))
    for k in range(4):
        img[k] = ndimage.gaussian_filter(img[k], 0.53)
    img += rng.normal(0, 0.004, img.shape).astype(np.float32)
    img += rng.normal(0, 0.004, (H, W)).astype(np.float32)[None]  # pavement texture

    scl = np.full(shape, 4, np.uint8)
    scl[r0:r1 + 4, c0:c1] = 5
    # SCL often calls white aircraft cloud
    scl[(img[3] > 0.38) & (scl == 5)] = 9
    if clouds:
        yy, xx = np.mgrid[0:H, 0:W]
        blob = (yy - 150) ** 2 + (xx - 150) ** 2 < 70 ** 2
        img[:, blob] = 0.7
        scl[blob] = 9
    airside = np.zeros(shape, bool)
    airside[r0:r1 + 4, c0:c1] = True
    bands = {"blue": img[0], "green": img[1], "red": img[2], "nir": img[3], "scl": scl}
    return bands, airside, truth


def make_radar(shape=(300, 300), aircraft=(), seed=0, apron=(60, 240, 60, 240), looks=4.4):
    """Return (bands dict, airside mask, truth list) in linear backscatter."""
    rng = np.random.default_rng(seed)
    H, W = shape
    vv = np.full(shape, 10 ** (-12 / 10), np.float32)          # grass
    r0, r1, c0, c1 = apron
    vv[r0:r1 + 4, c0:c1] = 10 ** (-19 / 10)                     # smooth tarmac
    for (br, bc) in ((20, 30), (30, 270), (270, 40)):
        vv[br:br + 6, bc:bc + 9] = 10 ** (8 / 10)                # buildings
    truth = []
    for (r, c, L, S, hd, _colour) in aircraft:
        size = int(max(L, S) / 10 * 1.6) + 8
        size += size % 2
        fp = aircraft_footprint(L, S, hd, size)
        sr, sc = r - size // 2, c - size // 2
        refl = fp * 10 ** (3 / 10) * rng.uniform(0.3, 3.0, fp.shape)  # metal: strong, uneven
        vv[sr:sr + size, sc:sc + size] += refl.astype(np.float32)
        truth.append((r - 0.5, c - 0.5, L, S))
    vv = ndimage.gaussian_filter(vv, 0.85)
    vh = vv * 10 ** (-7 / 10)
    speckle = lambda: rng.gamma(looks, 1 / looks, shape).astype(np.float32)  # noqa: E731
    bands = {"vv": vv * speckle(), "vh": vh * speckle()}
    airside = np.zeros(shape, bool)
    airside[r0:r1 + 4, c0:c1] = True
    return bands, airside, truth
