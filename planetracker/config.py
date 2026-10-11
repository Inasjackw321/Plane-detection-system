"""Central settings. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Sentinel-2 L2A (optical, 10 m). Earth Search needs no signing; Planetary Computer
# hands out short-lived SAS tokens anonymously.
S2_SOURCES = {
    "earth-search": {
        "stac_url": "https://earth-search.aws.element84.com/v1",
        "collection": "sentinel-2-c1-l2a",
        "assets": {"blue": "blue", "green": "green", "red": "red", "nir": "nir", "scl": "scl"},
    },
    "planetary-computer": {
        "stac_url": "https://planetarycomputer.microsoft.com/api/stac/v1",
        "collection": "sentinel-2-l2a",
        "assets": {"blue": "B02", "green": "B03", "red": "B04", "nir": "B08", "scl": "SCL"},
        "sas_url": "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}",
    },
}

# Sentinel-1 radar (10 m, terrain corrected, sees through cloud and at night).
S1_SOURCE = {
    "stac_url": "https://planetarycomputer.microsoft.com/api/stac/v1",
    "collection": "sentinel-1-rtc",
    "assets": {"vv": "vv", "vh": "vh"},
    "sas_url": "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}",
}

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("PLANETRACKER_DATA", str(ROOT / "data"))))
    airports_path: Path = field(default_factory=lambda: Path(
        _env("PLANETRACKER_AIRPORTS", str(ROOT / "config" / "airports.json"))))
    # Hand-drawn airfield outlines (<airport id>.geojson) that replace the OpenStreetMap ones.
    airfields_dir: Path = field(default_factory=lambda: Path(
        _env("PLANETRACKER_AIRFIELDS", str(ROOT / "config" / "airfields"))))
    s2_source: str = field(default_factory=lambda: _env("PLANETRACKER_S2_SOURCE", "earth-search"))
    # Overrides (handy for mirrors and tests).
    s2_stac_url: str | None = field(default_factory=lambda: os.environ.get("PLANETRACKER_S2_STAC_URL"))
    s1_stac_url: str | None = field(default_factory=lambda: os.environ.get("PLANETRACKER_S1_STAC_URL"))
    overpass_url: str | None = field(default_factory=lambda: os.environ.get("PLANETRACKER_OVERPASS_URL"))
    use_osm: bool = field(default_factory=lambda: _env("PLANETRACKER_USE_OSM", "1") != "0")

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def osm_dir(self) -> Path:
        return self.data_dir / "osm"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "planes.db"

    def s2_config(self) -> dict:
        if self.s2_source not in S2_SOURCES:
            raise ValueError(f"Unknown Sentinel-2 source {self.s2_source!r}; choose from {sorted(S2_SOURCES)}")
        cfg = dict(S2_SOURCES[self.s2_source])
        if self.s2_stac_url:
            cfg["stac_url"] = self.s2_stac_url
        return cfg

    def s1_config(self) -> dict:
        cfg = dict(S1_SOURCE)
        if self.s1_stac_url:
            cfg["stac_url"] = self.s1_stac_url
        return cfg

    def overpass_urls(self) -> list[str]:
        return [self.overpass_url] if self.overpass_url else list(OVERPASS_URLS)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.images_dir, self.osm_dir):
            d.mkdir(parents=True, exist_ok=True)
