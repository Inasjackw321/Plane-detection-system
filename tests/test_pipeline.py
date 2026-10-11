"""End-to-end: STAC search -> windowed COG reads -> detection -> database -> web API.

A local HTTP server plays the Sentinel-2 and Sentinel-1 STAC APIs and the imagery
buckets (with HTTP range requests, as S3 and Azure do). The synthetic tiles are in
UTM, like the real ones, so the warp onto the airport grid is exercised too.
"""
import json
import math
import os
import re
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import Polygon, mapping

from planetracker.config import Settings
from planetracker.pipeline import AnalyseOptions, analyse_airport
from planetracker.server import create_app
from tests.synth import TYPES, make_optical, make_radar

EPSG = 32639                          # UTM 39N
LAT, LON = 25.1357, 51.3150           # Al Udeid
SHAPE = (400, 400)
APRON = (100, 300, 100, 300)          # rows/cols of the apron in the synthetic tile
PLANES = [(150, 150, *TYPES["C-17"], 30, "white"), (150, 240, *TYPES["Tu-95"], 120, "grey"),
          (240, 160, *TYPES["KC-135"], 75, "grey"), (230, 250, *TYPES["Tu-160"], 0, "white")]


class RangeHandler(SimpleHTTPRequestHandler):
    """Static files with HTTP Range support, plus POST /<sensor>/search for STAC."""
    pages: dict = {}

    def log_message(self, *a):
        pass

    def send_head(self):
        path = Path(self.translate_path(self.path.split("?")[0]))
        if not path.is_file():
            self.send_error(404)
            return None
        data = path.read_bytes()
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d+)-(\d*)", rng or "")
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else len(data) - 1
            b = min(b, len(data) - 1)
            body = data[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "image/tiff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        from io import BytesIO
        return BytesIO(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        sensor = self.path.strip("/").split("/")[0]
        assert body["intersects"]["type"] == "Polygon"
        out = {"type": "FeatureCollection", "features": self.pages[sensor], "links": []}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/geo+json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _profile(origin, res, dtype, count=1, nodata=0):
    return dict(driver="GTiff", crs=f"EPSG:{EPSG}", transform=from_origin(origin[0], origin[1], res, res),
                tiled=True, blockxsize=256, blockysize=256, compress="deflate", dtype=dtype, count=count,
                nodata=nodata)


def _write(folder: Path, origin):
    folder.mkdir(parents=True)
    bands, _, truth = make_optical(shape=SHAPE, aircraft=PLANES, apron=APRON)
    for key, name in (("blue", "B02"), ("green", "B03"), ("red", "B04"), ("nir", "B08")):
        dn = np.clip((bands[key] + 0.1) / 1e-4, 1, 65535).astype(np.uint16)
        with rasterio.open(folder / f"{name}.tif", "w", height=SHAPE[0], width=SHAPE[1],
                           **_profile(origin, 10, "uint16")) as dst:
            dst.write(dn, 1)
    with rasterio.open(folder / "SCL.tif", "w", height=SHAPE[0] // 2, width=SHAPE[1] // 2,
                       **_profile(origin, 20, "uint8")) as dst:
        dst.write(bands["scl"][::2, ::2], 1)
    radar, _, _ = make_radar(shape=SHAPE, aircraft=PLANES, apron=APRON)
    for key in ("vv", "vh"):
        with rasterio.open(folder / f"{key}.tif", "w", height=SHAPE[0], width=SHAPE[1],
                           **_profile(origin, 10, "float32", nodata=-32768)) as dst:
            dst.write(radar[key].astype(np.float32), 1)
    return truth


def _item(item_id, dt, sensor, base, bounds, extra=None):
    w, s, e, n = bounds
    if sensor == "S2":
        assets = {k: {"href": f"{base}/tile/{f}.tif", "raster:bands": [{"scale": 0.0001, "offset": -0.1}]}
                  for k, f in (("blue", "B02"), ("green", "B03"), ("red", "B04"), ("nir", "B08"))}
        assets["scl"] = {"href": f"{base}/tile/SCL.tif"}
        props = {"datetime": dt, "eo:cloud_cover": 4.0, "platform": "sentinel-2b"}
    else:
        assets = {k: {"href": f"{base}/tile/{k}.tif"} for k in ("vv", "vh")}
        props = {"datetime": dt, "platform": "sentinel-1a", "sat:orbit_state": "descending"}
    props.update(extra or {})
    return {"type": "Feature", "stac_version": "1.0.0", "id": item_id,
            "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
            "bbox": [w, s, e, n], "properties": props, "assets": assets, "links": []}


@pytest.fixture()
def world(tmp_path):
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{EPSG}", always_xy=True)
    to_wgs = Transformer.from_crs(f"EPSG:{EPSG}", "EPSG:4326", always_xy=True)
    x, y = to_utm.transform(LON, LAT)
    origin = (round(x, -1) - SHAPE[1] * 5, round(y, -1) + SHAPE[0] * 5)  # airport in the middle
    served = tmp_path / "served"
    truth = _write(served / "tile", origin)

    def lonlat(r, c):
        return to_wgs.transform(origin[0] + (c + 0.5) * 10, origin[1] - (r + 0.5) * 10)

    corners = [lonlat(r, c) for r, c in ((-0.5, -0.5), (-0.5, SHAPE[1] - 0.5),
                                         (SHAPE[0] - 0.5, SHAPE[1] - 0.5), (SHAPE[0] - 0.5, -0.5))]
    lons, lats = zip(*corners)
    bounds = (min(lons), min(lats), max(lons), max(lats))
    r0, r1, c0, c1 = APRON
    apron = Polygon([lonlat(r, c) for r, c in ((r0, c0), (r0, c1), (r1 + 3, c1), (r1 + 3, c0))])
    airfields = tmp_path / "airfields"
    airfields.mkdir()
    (airfields / "test.geojson").write_text(json.dumps({"type": "Feature", "geometry": mapping(apron)}))
    airports = tmp_path / "airports.json"
    airports.write_text(json.dumps({"airports": [
        {"id": "test", "name": "Test Air Base", "country": "Qatar", "lat": LAT, "lon": LON, "half_size_m": 1500}]}))

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(RangeHandler, directory=str(served)))
    base = f"http://127.0.0.1:{server.server_port}"
    RangeHandler.pages = {
        "s2": [_item("S2B_39RWH_20261005_0_L2A", "2026-10-05T07:01:11Z", "S2", base, bounds)],
        "s1": [_item("S1A_IW_GRDH_1SDV_20261004T143012_rtc", "2026-10-04T14:30:12Z", "S1", base, bounds)],
    }
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = Settings(data_dir=tmp_path / "data", airports_path=airports, airfields_dir=airfields,
                        s2_stac_url=f"{base}/s2", s1_stac_url=f"{base}/s1", use_osm=False)
    truth_ll = [(*lonlat(r, c), max(L, S)) for r, c, L, S in truth]
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    yield settings, truth_ll
    server.shutdown()


def _near(features, lon, lat):
    def centre(f):
        return f["properties"]["lon"], f["properties"]["lat"]
    f = min(features, key=lambda f: math.dist(centre(f), (lon, lat)))
    d = math.dist(centre(f), (lon, lat)) * 111_000
    return f, d


def test_analyse_end_to_end(world):
    settings, truth = world
    from planetracker.airports import load_airports
    airport = load_airports(settings.airports_path)[0]
    rep = analyse_airport(settings, airport, AnalyseOptions(start="2026-10-01", end="2026-10-06"))
    assert rep.found == {"S2": 1, "S1": 1}
    assert (rep.processed, rep.failed) == (2, 0), rep.log
    assert rep.outline == "custom outline"
    assert rep.planes == 2 * len(PLANES)

    client = create_app(settings).test_client()
    detail = client.get("/api/airports/test").get_json()
    assert {s["sensor"] for s in detail["scenes"]} == {"S1", "S2"}
    for s in detail["scenes"]:
        assert s["status"] == "done" and s["n_planes"] == len(PLANES)
        img = client.get(s["overlay_url"])
        assert img.status_code == 200 and img.data[:4] == b"\x89PNG"
        assert client.get(s["annotated_url"]).status_code == 200

        fc = client.get(f"/api/airports/test/detections?scene={s['key']}").get_json()
        assert len(fc["features"]) == len(PLANES)
        for lon, lat, size in truth:
            f, dist = _near(fc["features"], lon, lat)
            assert dist < 20, f"{s['sensor']} aircraft found {dist:.0f} m from the truth"
            p = f["properties"]
            # the box (a polygon on the map) surrounds the aircraft
            (w, so), _, (e, n) = f["geometry"]["coordinates"][0][:3]
            assert w < lon < e and so < lat < n
            assert (e - w) * 111_000 * math.cos(math.radians(lat)) < 120
            # each aircraft was also seen by the other satellite a day apart
            assert p["corroborated"] == 1
            if s["sensor"] == "S2":
                assert abs(p["size_m"] - size) < max(0.25 * size, 12)
            chip = client.get(p["chip_url"])
            assert chip.status_code == 200 and chip.data[:4] == b"\x89PNG"

    airports = client.get("/api/airports").get_json()
    assert airports[0]["summary"]["S2"]["n_planes"] == len(PLANES)
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/airports/test/outline").get_json()["geometry"]["type"] == "Polygon"
    assert client.get("/api/airports/nope").status_code == 404

    again = analyse_airport(settings, airport, AnalyseOptions(start="2026-10-01", end="2026-10-06"))
    assert again.already_done == 2 and again.planes == 0


def test_analyse_through_web_api(world):
    settings, _ = world
    client = create_app(settings).test_client()
    r = client.post("/api/airports/test/analyse", json={"start": "2026-10-01", "end": "2026-10-06",
                                                         "sensors": ["S1"]})
    assert r.status_code == 202
    for _ in range(200):
        job = client.get("/api/airports/test/job").get_json()
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["state"] == "done", job
    assert job["report"]["found"] == {"S1": 1}
    assert job["report"]["planes"] == len(PLANES)
    assert client.post("/api/airports/test/analyse", json={"sensors": []}).status_code == 400
