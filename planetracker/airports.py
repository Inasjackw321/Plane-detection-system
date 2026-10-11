"""The airports that are analysed, and the image grid laid over each one."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from affine import Affine
from pyproj import Transformer

GROUND_PIXEL_M = 10.0  # Sentinel-1 and Sentinel-2 10 m bands
_TO_MERC = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_TO_WGS = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


@dataclass
class Airport:
    id: str
    name: str
    country: str
    lat: float
    lon: float
    half_size_m: float = 3500.0

    def to_dict(self) -> dict:
        return asdict(self)

    def grid(self) -> "Grid":
        return Grid.around(self.lon, self.lat, self.half_size_m)


@dataclass
class Grid:
    """A square image grid in Web Mercator (EPSG:3857) centred on an airport.

    The pixel size is chosen so that one pixel is 10 m on the ground, and because
    the grid is in Web Mercator, images rendered on it line up exactly with the
    web map without any further warping.
    """
    transform: Affine
    width: int
    height: int
    crs: str = "EPSG:3857"
    pixel_m: float = GROUND_PIXEL_M

    @classmethod
    def around(cls, lon: float, lat: float, half_size_m: float) -> "Grid":
        cx, cy = _TO_MERC.transform(lon, lat)
        p = GROUND_PIXEL_M / math.cos(math.radians(lat))  # mercator metres per 10 ground metres
        half = int(round(half_size_m / GROUND_PIXEL_M))
        tr = Affine(p, 0, cx - half * p, 0, -p, cy + half * p)
        return cls(tr, 2 * half, 2 * half)

    @property
    def shape(self) -> tuple[int, int]:
        return self.height, self.width

    def bounds_merc(self) -> tuple[float, float, float, float]:
        t = self.transform
        return t.c, t.f + t.e * self.height, t.c + t.a * self.width, t.f

    def bounds_lonlat(self) -> tuple[float, float, float, float]:
        """(west, south, east, north)"""
        x0, y0, x1, y1 = self.bounds_merc()
        w, s = _TO_WGS.transform(x0, y0)
        e, n = _TO_WGS.transform(x1, y1)
        return w, s, e, n

    def lonlat(self, row: float, col: float) -> tuple[float, float]:
        """Geographic position of a fractional pixel position (pixel centres at integers)."""
        t = self.transform
        x, y = t.c + t.a * (col + 0.5), t.f + t.e * (row + 0.5)
        return _TO_WGS.transform(x, y)

    def pixel(self, lon: float, lat: float) -> tuple[float, float]:
        """(row, col) of a geographic position."""
        x, y = _TO_MERC.transform(lon, lat)
        t = self.transform
        col, row = (x - t.c) / t.a, (y - t.f) / t.e
        return row - 0.5, col - 0.5


def load_airports(path: Path) -> list[Airport]:
    data = json.loads(Path(path).read_text())
    items = data["airports"] if isinstance(data, dict) else data
    out = []
    for a in items:
        out.append(Airport(id=str(a["id"]), name=a["name"], country=a.get("country", ""),
                           lat=float(a["lat"]), lon=float(a["lon"]),
                           half_size_m=float(a.get("half_size_m", 3500))))
    ids = [a.id for a in out]
    if len(set(ids)) != len(ids):
        raise ValueError(f"Duplicate airport ids in {path}")
    return out


def get_airport(path: Path, airport_id: str) -> Airport:
    for a in load_airports(path):
        if a.id == airport_id:
            return a
    raise KeyError(f"No airport {airport_id!r} in {path}")
