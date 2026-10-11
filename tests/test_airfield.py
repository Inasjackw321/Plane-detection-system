import json

from planetracker.airfield import get_outline, outline_from_overpass, outline_mask
from planetracker.airports import Airport, load_airports
from planetracker.config import ROOT

AIRPORT = Airport("test", "Test Field", "Nowhere", 25.0, 51.0, 1500)


def _square(lon, lat, d):
    return [{"lon": lon - d, "lat": lat - d}, {"lon": lon + d, "lat": lat - d}, {"lon": lon + d, "lat": lat + d},
            {"lon": lon - d, "lat": lat + d}, {"lon": lon - d, "lat": lat - d}]


OVERPASS = {"elements": [
    {"type": "way", "tags": {"aeroway": "apron"}, "geometry": _square(51.0, 25.0, 0.003)},
    {"type": "way", "tags": {"aeroway": "taxiway"},
     "geometry": [{"lon": 51.003, "lat": 25.0}, {"lon": 51.010, "lat": 25.0}]},
    {"type": "way", "tags": {"aeroway": "runway"},
     "geometry": [{"lon": 50.99, "lat": 24.995}, {"lon": 51.01, "lat": 24.995}]},
    {"type": "way", "tags": {"building": "hangar"}, "geometry": _square(51.002, 25.002, 0.0005)},
]}


def test_outline_keeps_aprons_and_taxiways_and_drops_buildings():
    g = outline_from_overpass(OVERPASS, 25.0)
    assert g.contains(_pt(51.0, 25.0))            # apron
    assert g.contains(_pt(51.008, 25.0))          # taxiway
    assert not g.contains(_pt(51.002, 25.002))    # hangar cut out
    assert not g.contains(_pt(50.995, 24.995))    # runway left out by default
    assert outline_from_overpass(OVERPASS, 25.0, include_runways=True).contains(_pt(50.995, 24.995))


def _pt(lon, lat):
    from shapely.geometry import Point
    return Point(lon, lat)


def test_outline_mask_and_cache(tmp_path):
    custom = tmp_path / "airfields"
    custom.mkdir()
    g = outline_from_overpass(OVERPASS, 25.0)
    from shapely.geometry import mapping
    (custom / "test.geojson").write_text(json.dumps({"type": "Feature", "geometry": mapping(g)}))
    geom, source = get_outline(AIRPORT, custom, tmp_path / "osm", [], use_osm=False)
    assert source == "custom outline"
    mask = outline_mask(geom, AIRPORT.grid())
    assert mask.shape == AIRPORT.grid().shape
    r, c = AIRPORT.grid().pixel(51.0, 25.0)
    assert mask[int(round(r)), int(round(c))]
    assert 0.02 < mask.mean() < 0.5

    geom, source = get_outline(AIRPORT, tmp_path / "none", tmp_path / "osm", [], use_osm=False)
    assert geom is None and source == "none"


def test_grid_is_ten_metres_on_the_ground():
    g = AIRPORT.grid()
    assert g.shape == (300, 300)
    w, s, e, n = g.bounds_lonlat()
    from pyproj import Geod
    _, _, width = Geod(ellps="WGS84").inv(w, 25.0, e, 25.0)
    assert abs(width - 3000) < 10
    r, c = g.pixel(*g.lonlat(12.3, 45.6))
    assert abs(r - 12.3) < 1e-6 and abs(c - 45.6) < 1e-6


def test_configured_airports():
    ids = [a.id for a in load_airports(ROOT / "config" / "airports.json")]
    assert ids == ["engels", "ukrainka", "al-udeid", "ovda", "souda"]
