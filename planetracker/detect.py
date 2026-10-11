"""Aircraft detection on Sentinel-2 (optical) and Sentinel-1 (radar) airport images.

Both detectors look for small, compact objects that stand out from the paved
surface around them, inside the airside area (aprons and taxiways):

Sentinel-2 (10 m, B02 B03 B04 B08)
    1. Clouds and cloud shadows from SCL are removed (SCL blobs the size of an
       aircraft are kept, because SCL often calls a white aircraft "cloud").
    2. For every band, the mean and spread of the surrounding pavement are
       measured in a 210 m window. Vegetation (NDVI) and water (NDWI) are left out
       of that background. Two passes; the second ignores first-pass hits.
    3. Each pixel's distance from the pavement, in standard deviations and
       combined over the four bands, is the anomaly. Aircraft can be brighter
       (white paint) or darker (grey paint, shadow) than the concrete; both count.
    4. Strong anomalies are seeds; each is grown to the connected pixels that are
       still anomalous, which captures the whole aircraft and its shadow.

Sentinel-1 (10 m pixels, ~20 m resolution, VV + VH)
    Metal airframes are strong radar reflectors on smooth, radar-dark tarmac. The
    backscatter is smoothed 3 x 3 against speckle, then the same local-background
    test is applied in decibels.

Size: the extent of the object is measured along eight directions with a
max-projection profile; the largest is the size (about max(length, wingspan)),
and largest / smallest separates aircraft (roughly as wide as long) from lines
such as vehicle queues, fences and pavement markings.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy import ndimage
from scipy.special import ndtri

# SCL classes: 0 nodata, 1 saturated/defective, 2 dark/topographic shadow,
# 3 cloud shadow, 4 vegetation, 5 bare, 6 water, 7 unclassified,
# 8 cloud medium prob, 9 cloud high prob, 10 thin cirrus, 11 snow.
SCL_CLOUD = (3, 8, 9, 10)
SCL_BAD = (0, 1)

ANGLES_DEG = np.arange(0.0, 180.0, 22.5)


@dataclass
class OpticalParams:
    bg_window: int = 21          # background window (px)
    k_sigma: float = 5.0         # seed threshold (combined standard deviations)
    grow_sigma: float = 2.5      # grow seeds to pixels above this
    min_contrast: float = 0.03   # seed must differ by this much reflectance in some band
    std_floor: float = 0.008     # floor for the local std (reflectance)
    max_ndvi: float = 0.30       # vegetation is not pavement
    max_ndwi: float = 0.15       # neither is water
    min_cloud_px: int = 150      # SCL cloud blobs smaller than this may be aircraft
    cloud_buffer_px: int = 5
    profile_frac: float = 0.25   # edge of the object on the max-projection profile
    psf_sigma_px: float = 0.53   # Sentinel-2 10 m point-spread function
    min_pixels: int = 2
    min_size_m: float = 10.0
    max_size_m: float = 95.0
    max_aspect: float = 3.2
    min_clear_fraction: float = 0.3  # of the airside area, else the pass counts as cloudy


@dataclass
class RadarParams:
    bg_window: int = 21
    smooth: int = 3              # speckle filter (px)
    k_sigma: float = 4.5
    min_excess_db: float = 6.0
    grow_db: float = 3.0
    std_floor_db: float = 0.5
    profile_frac: float = 0.25
    psf_sigma_px: float = 0.85   # ~20 m resolution on 10 m pixels
    min_pixels: int = 2
    min_size_m: float = 10.0
    max_size_m: float = 95.0
    max_aspect: float = 3.2


@dataclass
class Detection:
    row: float           # centre, pixel coordinates (pixel centres at integers)
    col: float
    box_r0: float        # visible extent of the object, pixel coordinates
    box_c0: float
    box_r1: float
    box_c1: float
    size_m: float        # largest extent ~ max(length, wingspan)
    min_m: float         # smallest extent
    area_m2: float
    npix: int
    contrast: float      # S2: reflectance difference from pavement (+ brighter, - darker); S1: dB above it
    snr: float
    size_class: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def size_class(size_m: float) -> str:
    if size_m >= 45:
        return "very large"   # strategic bomber, tanker, heavy transport, wide-body
    if size_m >= 32:
        return "large"        # medium bomber, airliner, tactical transport
    if size_m >= 20:
        return "medium"       # fighter-bomber, business jet
    return "small"            # fighter, helicopter, light aircraft (or a vehicle)


# ---------------------------------------------------------------------------
# shared pieces


def local_stats(img: np.ndarray, mask: np.ndarray, win: int):
    """Mean and std of ``img`` over ``mask`` pixels in a ``win`` x ``win`` window."""
    m = mask.astype(np.float32)
    x = np.where(mask, img, 0).astype(np.float32)
    n = ndimage.uniform_filter(m, win)
    s1 = ndimage.uniform_filter(x, win)
    s2 = ndimage.uniform_filter(x * x, win)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = s1 / n
        var = s2 / n - mean * mean
    std = np.sqrt(np.clip(var, 0, None))
    return np.nan_to_num(mean), np.nan_to_num(std), n


def _crossings(prof: np.ndarray, coords: np.ndarray, level: float) -> tuple[float, float] | None:
    above = np.nonzero(prof >= level)[0]
    if above.size == 0 or level <= 0:
        return None
    i0, i1 = above[0], above[-1]

    def interp(a: int, b: int) -> float:
        pa, pb = prof[a], prof[b]
        if pb == pa:
            return coords[b]
        return coords[a] + (level - pa) / (pb - pa) * (coords[b] - coords[a])

    lo = interp(i0 - 1, i0) if i0 > 0 else coords[0]
    hi = interp(i1 + 1, i1) if i1 < prof.size - 1 else coords[-1]
    return lo, hi


def measure(sig: np.ndarray, blob: np.ndarray, frac: float, sigma_px: float) -> dict | None:
    """Extent of one object. ``sig`` is its (non-negative) anomaly around it, ``blob`` its pixels."""
    rr, cc = np.nonzero(blob)
    if rr.size == 0:
        return None
    w = sig[rr, cc]
    if w.sum() <= 0:
        return None
    cr, cc0 = float(np.average(rr, weights=w)), float(np.average(cc, weights=w))
    local = np.where(ndimage.binary_dilation(blob, iterations=1), sig, 0.0)
    half = math.hypot(rr.max() - rr.min(), cc.max() - cc.min()) / 2 + 3
    ds = 0.25
    s = np.arange(-half, half + ds, ds)
    S, T = np.meshgrid(s, s, indexing="ij")
    # Blur seen by the profile: optics + pixel box + bilinear resampling.
    sigma_eff = math.sqrt(sigma_px ** 2 + 1 / 12 + 1 / 6)
    corr = 2 * float(ndtri(1 - frac)) * sigma_eff

    extents, spans = [], {}
    for ang in ANGLES_DEG:
        th = math.radians(ang)
        u = (-math.cos(th), math.sin(th))   # along: 0 deg = up (north), 90 deg = right (east)
        v = (math.sin(th), math.cos(th))    # across
        R = cr + S * u[0] + T * v[0]
        C = cc0 + S * u[1] + T * v[1]
        grid = ndimage.map_coordinates(local, [R, C], order=1, mode="constant", cval=0.0)
        prof = grid.max(axis=1)
        ends = _crossings(prof, s, frac * prof.max())
        if ends is None:
            return None
        extents.append(max(ends[1] - ends[0] - corr, 0.5))
        spans[float(ang)] = ends
    (n_lo, n_hi), (e_lo, e_hi) = spans[0.0], spans[90.0]
    return {
        "row": cr - (n_lo + n_hi) / 2,
        "col": cc0 + (e_lo + e_hi) / 2,
        "box": (cr - n_hi, cc0 + e_lo, cr - n_lo, cc0 + e_hi),
        "size_px": float(max(extents)),
        "min_px": float(min(extents)),
    }


def _confidence(snr: float, k: float, size_m: float, aspect: float, max_aspect: float,
                outlined: bool) -> float:
    snr_score = min(snr / (2 * k), 1.0)
    size_score = 1.0 if 15 <= size_m <= 80 else 0.5
    shape_score = float(np.clip((max_aspect - aspect) / (max_aspect - 1), 0, 1))
    c = 0.45 * snr_score + 0.25 * size_score + 0.15 * shape_score + 0.15 * (1.0 if outlined else 0.0)
    return round(float(np.clip(c, 0, 1)), 3)


def _extract(score: np.ndarray, seeds: np.ndarray, growable: np.ndarray, grow_level: float,
             sig: np.ndarray, contrast_img: np.ndarray, p, pixel_m: float, outlined: bool) -> list[Detection]:
    """Grow seeds into objects, measure them and keep the aircraft-like ones."""
    labels, n = ndimage.label(seeds, structure=np.ones((3, 3)))
    if n == 0:
        return []
    peaks = ndimage.maximum(score, labels, index=np.arange(1, n + 1))
    slices = ndimage.find_objects(labels)
    claimed = np.zeros(score.shape, bool)
    H, W = score.shape
    pad = int(p.max_size_m / pixel_m / 2) + 4
    out: list[Detection] = []
    for idx in np.argsort(-np.asarray(peaks)):
        sl = slices[idx]
        if sl is None:
            continue
        r0, r1 = max(sl[0].start - pad, 0), min(sl[0].stop + pad, H)
        c0, c1 = max(sl[1].start - pad, 0), min(sl[1].stop + pad, W)
        seed = labels[r0:r1, c0:c1] == idx + 1
        if (claimed[r0:r1, c0:c1] & seed).any():
            continue
        grow = (score[r0:r1, c0:c1] > grow_level) & growable[r0:r1, c0:c1]
        glab, _ = ndimage.label(grow | seed, structure=np.ones((3, 3)))
        blob = np.isin(glab, np.unique(glab[seed])) & ~claimed[r0:r1, c0:c1]
        claimed[r0:r1, c0:c1] |= blob
        npix = int(blob.sum())
        if npix < p.min_pixels:
            continue
        m = measure(sig[r0:r1, c0:c1], blob, p.profile_frac, p.psf_sigma_px)
        if m is None:
            continue
        size_m, min_m = m["size_px"] * pixel_m, m["min_px"] * pixel_m
        aspect = size_m / max(min_m, 1e-6)
        if not (p.min_size_m <= size_m <= p.max_size_m) or aspect > p.max_aspect:
            continue
        snr = float(score[r0:r1, c0:c1][blob].max())
        cvals = contrast_img[r0:r1, c0:c1][blob]
        contrast = float(cvals[np.argmax(np.abs(cvals))])
        b = m["box"]
        out.append(Detection(
            row=m["row"] + r0, col=m["col"] + c0,
            box_r0=b[0] + r0, box_c0=b[1] + c0, box_r1=b[2] + r0, box_c1=b[3] + c0,
            size_m=round(size_m, 1), min_m=round(min_m, 1),
            area_m2=round(npix * pixel_m * pixel_m, 0), npix=npix,
            contrast=round(contrast, 4), snr=round(snr, 1),
            size_class=size_class(size_m),
            confidence=_confidence(snr, p.k_sigma, size_m, aspect, p.max_aspect, outlined),
        ))
    return out


# ---------------------------------------------------------------------------
# Sentinel-2


def optical_masks(bands: dict[str, np.ndarray], airside: np.ndarray | None, p: OpticalParams):
    """Return (search area, clear fraction of the airside area)."""
    b, g, r, n = (bands[k] for k in ("blue", "green", "red", "nir"))
    valid = np.isfinite(b) & np.isfinite(g) & np.isfinite(r) & np.isfinite(n)
    scl = bands.get("scl")
    cloud = np.zeros(b.shape, bool)
    if scl is not None:
        valid &= ~np.isin(scl, SCL_BAD)
        raw = np.isin(scl, SCL_CLOUD)
        labels, nl = ndimage.label(raw)
        if nl:
            sizes = np.bincount(labels.ravel())
            sizes[0] = 0
            cloud = (sizes >= p.min_cloud_px)[labels]
            if cloud.any() and p.cloud_buffer_px:
                cloud = ndimage.maximum_filter(cloud, size=2 * p.cloud_buffer_px + 1)
    area = airside if airside is not None else np.ones(b.shape, bool)
    search = area & valid & ~cloud
    clear = float(search.sum() / max(area.sum(), 1))
    return search, clear


def detect_optical(bands: dict[str, np.ndarray], airside: np.ndarray | None,
                   p: OpticalParams | None = None, pixel_m: float = 10.0) -> tuple[list[Detection], float]:
    """Detect aircraft in one Sentinel-2 pass. Returns (detections, clear fraction)."""
    p = p or OpticalParams()
    search, clear = optical_masks(bands, airside, p)
    if clear < p.min_clear_fraction or not search.any():
        return [], clear
    keys = ("blue", "green", "red", "nir")
    X = [np.nan_to_num(bands[k]).astype(np.float32) for k in keys]
    red, nir, green = X[2], X[3], X[1]
    with np.errstate(invalid="ignore", divide="ignore"):
        ndvi = np.nan_to_num((nir - red) / (nir + red))
        ndwi = np.nan_to_num((green - nir) / (green + nir))
    pavement = search & (ndvi < p.max_ndvi) & (ndwi < p.max_ndwi)
    if not pavement.any():
        return [], clear
    background = ndimage.binary_erosion(pavement, iterations=1, border_value=1)

    def anomaly(bg):
        z2 = np.zeros(search.shape, np.float32)
        dev = np.zeros(search.shape, np.float32)
        for x in X:
            mean, std, _ = local_stats(x, bg, p.bg_window)
            d = x - mean
            z2 += (d / np.maximum(std, p.std_floor)) ** 2
            dev = np.where(np.abs(d) > np.abs(dev), d, dev)
        _, _, cover = local_stats(X[0], bg, p.bg_window)
        z = np.sqrt(z2 / len(X))
        z[cover < 0.2] = 0  # not enough pavement around to judge
        return z, dev

    z, dev = anomaly(background)
    # Large aircraft inflate the local spread; leave anything clearly off-pavement
    # out of the background on the second pass.
    first = pavement & ((z > p.k_sigma) | (np.abs(dev) > p.min_contrast))
    if first.any():
        background &= ~ndimage.maximum_filter(first, size=9)
        z, dev = anomaly(background)
    seeds = pavement & (z > p.k_sigma) & (np.abs(dev) > p.min_contrast)
    sig = np.abs(dev)
    dets = _extract(z, seeds, search, p.grow_sigma, sig, dev, p, pixel_m, airside is not None)
    return dets, clear


# ---------------------------------------------------------------------------
# Sentinel-1


def detect_radar(bands: dict[str, np.ndarray], airside: np.ndarray | None,
                 p: RadarParams | None = None, pixel_m: float = 10.0) -> tuple[list[Detection], float]:
    """Detect aircraft in one Sentinel-1 pass. Returns (detections, valid fraction)."""
    p = p or RadarParams()
    vv = bands["vv"]
    vh = bands.get("vh")
    span = vv + vh if vh is not None else vv
    valid = np.isfinite(span) & (span > 0)
    area = airside if airside is not None else np.ones(vv.shape, bool)
    search = area & valid
    frac = float(search.sum() / max(area.sum(), 1))
    if frac < 0.3:
        return [], frac
    lin = np.where(valid, span, 0).astype(np.float32)
    num = ndimage.uniform_filter(lin, p.smooth)
    den = ndimage.uniform_filter(valid.astype(np.float32), p.smooth)
    with np.errstate(invalid="ignore", divide="ignore"):
        db = 10 * np.log10(np.maximum(num / den, 1e-6))
    db = np.nan_to_num(db, nan=-60.0)
    background = ndimage.binary_erosion(search, iterations=1, border_value=1)

    def anomaly(bg):
        mean, std, cover = local_stats(db, bg, p.bg_window)
        excess = db - mean
        snr = excess / np.maximum(std, p.std_floor_db)
        snr[cover < 0.2] = 0
        return excess, snr

    excess, snr = anomaly(background)
    first = search & ((snr > p.k_sigma) | (excess > p.min_excess_db))
    if first.any():
        background &= ~ndimage.maximum_filter(first, size=9)
        excess, snr = anomaly(background)
    seeds = search & (snr > p.k_sigma) & (excess > p.min_excess_db)
    sig = np.clip(10 ** (excess / 10) - 1, 0, None)  # linear power above the tarmac
    # Grow on excess in dB: express it on the same scale as snr for _extract.
    growable = search & (excess > p.grow_db)
    dets = _extract(snr, seeds, growable, -np.inf, sig, excess, p, pixel_m, airside is not None)
    return dets, frac
