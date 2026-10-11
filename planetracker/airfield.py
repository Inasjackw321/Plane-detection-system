"""Where aircraft can be parked: aprons and taxiways, from OpenStreetMap.

Searching only paved airside surfaces is what keeps buildings, cars, lamp posts
and crop fields from being reported as aircraft. The outline comes from, in order:

1. ``config/airfields/<airport id>.geojson`` (hand-drawn, lon/lat), if present;
2. a cached OpenStreetMap download in ``data/osm/<airport id>.geojson``;
3. a fresh Overpass API query (aprons, taxiways, optionally runways, minus buildings).

If none is available the whole box is searched and confidence is reduced.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import requests
from pyproj import Transformer
from rasterio.features import rasterize
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
import shapely
from shapely.ops import unary_union

from .airports import Airport, Grid

log = logging.getLogger(__name__)

TAXIWAY_HALF_WIDTH_M = 15.0   # taxiway plus shoulders
RUNWAY_HALF_WIDTH_M = 30.0
BUILDING_BUFFER_M = 10.0

_TO_MERC = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_TO_WGS = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


def shp_transform(transformer: Transformer, geom: BaseGeometry) -> BaseGeometry:
    def fn(xy):
        x, y = transformer.transform(xy[:, 0], xy[:, 1])
        return np.column_stack([x, y])
    return shapely.transform(geom, fn)


def overpass_query(bbox: tuple[float, float, float, float]) -> str:
    w, s, e, n = bbox
    b = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
    return (f'[out:json][timeout:60];('
            f'way["aeroway"~"^(apron|taxiway|runway|parking_position|holding_position)$"]{b};'
            f'relation["aeroway"="apron"]{b};'
            f'way["building"]{b};'
            f');out geom;')


def _ground_scale(lat: float) -> float:
    """Mercator metres per ground metre."""
    return 1 / np.cos(np.radians(lat))


def _way_geom(el: dict) -> BaseGeometry | None:
    pts = [(p["lon"], p["lat"]) for p in el.get("geometry", []) if p]
    if len(pts) < 2:
        return None
    if len(pts) >= 4 and pts[0] == pts[-1]:
        poly = Polygon(pts)
        return poly if poly.is_valid else poly.buffer(0)
    return LineString(pts)


def _relation_geom(el: dict) -> BaseGeometry | None:
    outers = []
    inners = []
    for m in el.get("members", []):
        pts = [(p["lon"], p["lat"]) for p in m.get("geometry", []) if p]
        if len(pts) >= 4 and pts[0] == pts[-1]:
            (inners if m.get("role") == "inner" else outers).append(Polygon(pts).buffer(0))
    if not outers:
        return None
    g = unary_union(outers)
    if inners:
        g = g.difference(unary_union(inners))
    return g


def outline_from_overpass(data: dict, lat: float, include_runways: bool = False) -> BaseGeometry | None:
    """Turn an Overpass ``out geom`` answer into the searchable airside area (lon/lat)."""
    k = _ground_scale(lat)
    keep, drop = [], []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        geom = _relation_geom(el) if el.get("type") == "relation" else _way_geom(el)
        if geom is None or geom.is_empty:
            continue
        g = shp_transform(_TO_MERC, geom)
        aeroway = tags.get("aeroway")
        if "building" in tags and aeroway is None:
            if g.geom_type in ("Polygon", "MultiPolygon"):
                drop.append(g.buffer(BUILDING_BUFFER_M * k))
            continue
        if aeroway in ("apron", "parking_position", "holding_position"):
            if g.geom_type in ("Polygon", "MultiPolygon"):
                keep.append(g)
            else:
                keep.append(g.buffer(TAXIWAY_HALF_WIDTH_M * k))
        elif aeroway == "taxiway":
            keep.append(g.buffer(_half_width(tags, TAXIWAY_HALF_WIDTH_M) * k))
        elif aeroway == "runway" and include_runways:
            keep.append(g.buffer(_half_width(tags, RUNWAY_HALF_WIDTH_M) * k))
    if not keep:
        return None
    area = unary_union(keep)
    if drop:
        area = area.difference(unary_union(drop))
    if area.is_empty:
        return None
    return shp_transform(_TO_WGS, area.simplify(1.0))


def _half_width(tags: dict, default: float) -> float:
    try:
        return max(float(str(tags.get("width", "")).split()[0]) / 2 + 5, default * 0.5)
    except (ValueError, IndexError):
        return default


def fetch_overpass(urls: list[str], bbox: tuple[float, float, float, float]) -> dict:
    q = overpass_query(bbox)
    last: Exception | None = None
    for url in urls:
        try:
            r = requests.post(url, data={"data": q}, timeout=90,
                              headers={"User-Agent": "plane-tracker/1.0"})
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Overpass %s failed: %s", url, exc)
            last = exc
    raise RuntimeError(f"OpenStreetMap download failed: {last}")


def _load_geojson(path: Path) -> BaseGeometry | None:
    data = json.loads(path.read_text())
    if data.get("type") == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in data["features"] if f.get("geometry")]
    elif data.get("type") == "Feature":
        geoms = [shape(data["geometry"])] if data.get("geometry") else []
    else:
        geoms = [shape(data)]
    geoms = [g if g.is_valid else g.buffer(0) for g in geoms]
    return unary_union(geoms) if geoms else None


def get_outline(airport: Airport, airfields_dir: Path, osm_dir: Path, overpass_urls: list[str],
                use_osm: bool = True, refresh: bool = False) -> tuple[BaseGeometry | None, str]:
    """Return (airside area in lon/lat or None, where it came from)."""
    custom = Path(airfields_dir) / f"{airport.id}.geojson"
    if custom.exists():
        return _load_geojson(custom), "custom outline"
    cache = Path(osm_dir) / f"{airport.id}.geojson"
    if cache.exists() and not refresh:
        geom = _load_geojson(cache)
        return geom, "OpenStreetMap" if geom is not None else "none (OpenStreetMap has no aprons here)"
    if not use_osm:
        return None, "none"
    try:
        data = fetch_overpass(overpass_urls, airport.grid().bounds_lonlat())
    except RuntimeError as exc:
        log.warning("%s: %s; searching the whole box instead", airport.name, exc)
        return None, "none (OpenStreetMap unreachable)"
    geom = outline_from_overpass(data, airport.lat)
    cache.parent.mkdir(parents=True, exist_ok=True)
    feature = {"type": "Feature", "properties": {"source": "OpenStreetMap contributors (ODbL)"},
               "geometry": mapping(geom) if geom is not None else None}
    cache.write_text(json.dumps({"type": "FeatureCollection", "features": [feature]}))
    return geom, "OpenStreetMap" if geom is not None else "none (OpenStreetMap has no aprons here)"


def outline_mask(outline: BaseGeometry | None, grid: Grid) -> np.ndarray | None:
    """Rasterise the outline onto the grid (True = airside)."""
    if outline is None:
        return None
    merc = shp_transform(_TO_MERC, outline)
    mask = rasterize([(mapping(merc), 1)], out_shape=grid.shape, transform=grid.transform,
                     fill=0, dtype="uint8", all_touched=False)
    return mask.astype(bool)
