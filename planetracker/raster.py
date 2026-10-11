"""Read just the airport window of a Sentinel pass, straight from the cloud-optimised GeoTIFFs.

Only the few hundred kilobytes that cover the airport are fetched (HTTP range
requests), not the whole 100 x 100 km tile. Every band is warped onto the
airport's 10 m Web Mercator grid, so all dates and both satellites line up.
"""
from __future__ import annotations

import logging
import time

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.vrt import WarpedVRT

from .airports import Grid
from .stac import Scene, sign_href

log = logging.getLogger(__name__)

GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    GDAL_HTTP_MAX_RETRY="4",
    GDAL_HTTP_RETRY_DELAY="2",
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    VSI_CACHE="TRUE",
    GDAL_HTTP_USERAGENT="plane-tracker/1.0",
)

CATEGORICAL = {"scl"}


def _read_band(href: str, grid: Grid, resampling: Resampling) -> np.ma.MaskedArray:
    with rasterio.open(href) as src:
        nodata = src.nodata if src.nodata is not None else 0
        with WarpedVRT(src, crs=grid.crs, transform=grid.transform, width=grid.width,
                       height=grid.height, resampling=resampling, src_nodata=nodata,
                       nodata=nodata) as vrt:
            return vrt.read(1, masked=True)


def read_band(href: str, grid: Grid, resampling: Resampling, retries: int = 3) -> np.ma.MaskedArray:
    for attempt in range(retries):
        try:
            with rasterio.Env(**GDAL_ENV):
                return _read_band(href, grid, resampling)
        except RasterioIOError as exc:
            if attempt == retries - 1:
                raise
            wait = 2 ** (attempt + 1)
            log.warning("Reading %s failed (%s); retrying in %ss", href.split("?")[0], exc, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def read_pass(scene: Scene, grid: Grid, source_cfg: dict) -> dict[str, np.ndarray]:
    """Return every band of a pass on the airport grid.

    Optical bands come back as reflectance, radar bands as linear backscatter and
    SCL as class numbers. Missing data is NaN (SCL: 0). Tiles of the same pass are
    mosaicked: later tiles only fill what earlier ones did not cover.
    """
    out: dict[str, np.ndarray] = {}
    for key in scene.parts[0].hrefs:
        categorical = key in CATEGORICAL
        band = np.zeros(grid.shape, np.uint8) if categorical else np.full(grid.shape, np.nan, np.float32)
        for part in scene.parts:
            missing = band == 0 if categorical else np.isnan(band)
            if not missing.any():
                break
            href = sign_href(part.hrefs[key], source_cfg)
            arr = read_band(href, grid, Resampling.nearest if categorical else Resampling.bilinear)
            valid = ~np.ma.getmaskarray(arr) & (arr.data != 0)
            fill = missing & valid
            if categorical:
                band[fill] = arr.data[fill].astype(np.uint8)
            else:
                scale, offset = part.scales[key]
                band[fill] = arr.data[fill].astype(np.float32) * scale + offset
        out[key] = band
    return out
