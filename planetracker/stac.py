"""Find Sentinel-1 and Sentinel-2 passes over an airport through STAC APIs."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

import requests
from shapely.geometry import box, mapping, shape

log = logging.getLogger(__name__)

SAME_PASS_SECONDS = 300  # neighbouring tiles of one satellite pass


@dataclass
class Part:
    """One STAC item (tile / slice) of a pass."""
    item_id: str
    hrefs: dict[str, str]
    scales: dict[str, tuple[float, float]]
    coverage: float  # fraction of the airport box inside this item's footprint


@dataclass
class Scene:
    """One satellite pass over one airport (possibly several tiles)."""
    id: str
    sensor: str               # "S2" or "S1"
    datetime: str
    platform: str | None
    cloud_cover: float | None
    orbit: str | None
    coverage: float
    parts: list[Part] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "plane-tracker/1.0"
    return s


def _request(session: requests.Session, method: str, url: str, **kw) -> dict:
    for attempt in range(5):
        try:
            r = session.request(method, url, timeout=60, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            if attempt == 4:
                raise
            wait = 2 ** (attempt + 1)
            log.warning("STAC request failed (%s); retrying in %ss", exc, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _scaling(item: dict, key: str, sensor: str) -> tuple[float, float]:
    """(scale, offset) that turn stored values into reflectance (S2) or linear backscatter (S1)."""
    if sensor == "S1":
        return 1.0, 0.0
    bands = item["assets"][key].get("raster:bands") or []
    if bands and "scale" in bands[0]:
        return float(bands[0]["scale"]), float(bands[0].get("offset", 0.0))
    if key.lower() == "scl":
        return 1.0, 0.0
    # Processing baseline 04.00+ (Jan 2022 onward) stores DN with a +1000 offset.
    baseline = str(item["properties"].get("s2:processing_baseline", "00.00"))
    try:
        newer = float(baseline) >= 4.0
    except ValueError:
        newer = False
    return 1e-4, (-0.1 if newer else 0.0)


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def search_passes(source_cfg: dict, sensor: str, bbox: tuple[float, float, float, float],
                  start: str, end: str, max_cloud: float | None = None,
                  limit: int | None = None) -> list[Scene]:
    """Passes over ``bbox`` (west, south, east, north) between two ISO dates, newest first."""
    session = _session()
    url = source_cfg["stac_url"].rstrip("/") + "/search"
    area = box(*bbox)
    body: dict = {
        "collections": [source_cfg["collection"]],
        "intersects": mapping(area),
        "datetime": f"{_iso(start)}/{_iso(end, end_of_day=True)}",
        "limit": 100,
    }
    if sensor == "S2" and max_cloud is not None:
        body["query"] = {"eo:cloud_cover": {"lte": max_cloud}}

    wanted = source_cfg["assets"]
    items: list[dict] = []
    seen: set[str] = set()
    method, next_url, next_body = "POST", url, body
    while next_url:
        page = (_request(session, method, next_url, json=next_body) if method == "POST"
                else _request(session, "GET", next_url))
        for item in page.get("features", []):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            if all(wanted[k] in item.get("assets", {}) for k in wanted):
                items.append(item)
        method, next_url, next_body = _next_link(page, next_body)
        if limit and len(items) >= limit * 6:  # enough for `limit` passes even with many tiles each
            break

    passes = _group(items, sensor, wanted, area)
    passes.sort(key=lambda s: s.datetime, reverse=True)
    return passes[:limit] if limit else passes


def _group(items: list[dict], sensor: str, wanted: dict, area) -> list[Scene]:
    items = sorted(items, key=lambda it: _ts(it["properties"]["datetime"]))
    groups: list[list[dict]] = []
    for it in items:
        p = it["properties"]
        if groups:
            last = groups[-1][-1]["properties"]
            if (abs(_ts(p["datetime"]) - _ts(last["datetime"])) < SAME_PASS_SECONDS
                    and p.get("platform") == last.get("platform")):
                groups[-1].append(it)
                continue
        groups.append([it])

    scenes = []
    for g in groups:
        parts = []
        for it in g:
            cov = shape(it["geometry"]).intersection(area).area / area.area if area.area else 0.0
            if cov <= 0:
                continue
            parts.append(Part(
                item_id=it["id"],
                hrefs={k: it["assets"][v]["href"] for k, v in wanted.items()},
                scales={k: _scaling(it, v, sensor) for k, v in wanted.items()},
                coverage=round(cov, 4),
            ))
        if not parts:
            continue
        parts.sort(key=lambda p: p.coverage, reverse=True)
        footprint = None
        for it in g:
            gshape = shape(it["geometry"])
            footprint = gshape if footprint is None else footprint.union(gshape)
        props = g[0]["properties"]
        clouds = [it["properties"].get("eo:cloud_cover") for it in g if it["properties"].get("eo:cloud_cover") is not None]
        scenes.append(Scene(
            id=parts[0].item_id,
            sensor=sensor,
            datetime=props["datetime"],
            platform=props.get("platform"),
            cloud_cover=round(min(clouds), 2) if clouds else None,
            orbit=props.get("sat:orbit_state"),
            coverage=round(min(footprint.intersection(area).area / area.area, 1.0), 4),
            parts=parts,
        ))
    return scenes


def _next_link(page: dict, prev_body: dict | None) -> tuple[str, str | None, dict | None]:
    for link in page.get("links", []):
        if link.get("rel") != "next":
            continue
        if link.get("method", "GET").upper() == "POST":
            body = link.get("body") or {}
            if link.get("merge") and prev_body:
                body = {**prev_body, **body}
            return "POST", link["href"], body
        return "GET", link["href"], None
    return "GET", None, None


def _iso(date: str, end_of_day: bool = False) -> str:
    if "T" in date:
        return date
    return f"{date}T23:59:59Z" if end_of_day else f"{date}T00:00:00Z"


_sas_cache: dict[str, tuple[str, float]] = {}


def sign_href(href: str, source_cfg: dict) -> str:
    """Make an asset URL readable: s3:// to https, plus a Planetary Computer SAS token when needed."""
    if href.startswith("s3://"):
        bucket, _, key = href[5:].partition("/")
        href = f"https://{bucket}.s3.amazonaws.com/{key}"
    sas_url = source_cfg.get("sas_url")
    if not sas_url or "blob.core.windows.net" not in href:
        return href
    collection = source_cfg["collection"]
    token, expiry = _sas_cache.get(collection, ("", 0.0))
    if time.time() > expiry - 300:
        data = _request(_session(), "GET", sas_url.format(collection=collection))
        token = data["token"]
        _sas_cache[collection] = (token, time.time() + 45 * 60)
    return f"{href}{'&' if '?' in href else '?'}{token}"
