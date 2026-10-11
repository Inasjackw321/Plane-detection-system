"""Web map + JSON API. Click an airport on the map to analyse it."""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from shapely.geometry import mapping

from .airfield import get_outline
from .airports import load_airports
from .config import Settings
from .db import Store
from .pipeline import AnalyseOptions, AnalyseReport, analyse_airport

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


def _feature(d: dict) -> dict:
    props = {k: v for k, v in d.items() if not k.startswith("box_")}
    props["chip_url"] = f"/images/{d['chip']}" if d.get("chip") else None
    w, s, e, n = d["box_w"], d["box_s"], d["box_e"], d["box_n"]
    return {"type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
            "properties": props}


def _scene_json(s: dict) -> dict:
    s = dict(s)
    s["overlay_url"] = f"/images/{s['overlay']}" if s.get("overlay") else None
    s["annotated_url"] = f"/images/{s['annotated']}" if s.get("annotated") else None
    return s


class Jobs:
    """Analyses run one at a time in a background thread; clicks on other airports queue up."""

    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.state: dict[str, dict] = {}
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, airport, opts: AnalyseOptions) -> dict:
        with self.lock:
            cur = self.state.get(airport.id)
            if cur and cur["state"] in ("queued", "running"):
                return cur
            st = {"state": "queued", "report": asdict(AnalyseReport(airport=airport.id)), "error": None,
                  "options": {k: v for k, v in asdict(opts).items() if k not in ("optical", "radar")}}
            self.state[airport.id] = st
        self.q.put((airport, opts))
        return st

    def get(self, airport_id: str) -> dict | None:
        return self.state.get(airport_id)

    def _worker(self) -> None:
        while True:
            airport, opts = self.q.get()
            st = self.state[airport.id]
            st["state"] = "running"
            try:
                analyse_airport(self.settings, airport, opts, store=self.store,
                                on_progress=lambda r: st.update(report=asdict(r)))
                st["state"] = "done"
            except Exception as exc:  # surfaced to the UI
                log.exception("Analysis of %s failed", airport.id)
                st.update(state="error", error=str(exc))


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    jobs = Jobs(settings, store)
    app = Flask(__name__, static_folder=None)

    def airports() -> dict:
        return {a.id: a for a in load_airports(settings.airports_path)}

    def find(airport_id: str):
        a = airports().get(airport_id)
        if a is None:
            abort(404, f"unknown airport {airport_id}")
        return a

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(WEB_DIR, name)

    @app.get("/images/<path:name>")
    def images(name):
        return send_from_directory(settings.images_dir, name)

    @app.get("/api/airports")
    def airport_list():
        out = []
        for a in airports().values():
            g = a.grid()
            out.append({**a.to_dict(), "bounds": list(g.bounds_lonlat()),
                        "summary": store.summary(a.id), "job": jobs.get(a.id)})
        return jsonify(out)

    @app.get("/api/airports/<airport_id>")
    def airport_detail(airport_id):
        a = find(airport_id)
        return jsonify({**a.to_dict(), "bounds": list(a.grid().bounds_lonlat()),
                        "summary": store.summary(a.id), "job": jobs.get(a.id),
                        "scenes": [_scene_json(s) for s in store.scenes(a.id)]})

    @app.get("/api/airports/<airport_id>/detections")
    def detections(airport_id):
        a = find(airport_id)
        args = request.args
        try:
            rows = store.detections(a.id, scene_key=args.get("scene") or None,
                                    sensor=args.get("sensor") or None,
                                    start=args.get("start") or None, end=args.get("end") or None,
                                    min_confidence=float(args.get("min_confidence", 0) or 0))
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify({"type": "FeatureCollection", "features": [_feature(r) for r in rows]})

    @app.get("/api/airports/<airport_id>/outline")
    def outline(airport_id):
        a = find(airport_id)
        geom, source = get_outline(a, settings.airfields_dir, settings.osm_dir, [], use_osm=False)
        return jsonify({"type": "Feature", "properties": {"source": source},
                        "geometry": mapping(geom) if geom is not None else None})

    @app.post("/api/airports/<airport_id>/analyse")
    def analyse(airport_id):
        a = find(airport_id)
        body = request.get_json(silent=True) or {}
        today = date.today()
        try:
            sensors = tuple(s for s in body.get("sensors", ["S2", "S1"]) if s in ("S2", "S1"))
            if not sensors:
                raise ValueError("choose Sentinel-2, Sentinel-1 or both")
            opts = AnalyseOptions(
                start=body.get("start") or (today - timedelta(days=30)).isoformat(),
                end=body.get("end") or today.isoformat(),
                max_cloud=float(body.get("max_cloud", 60)),
                max_scenes=max(1, min(int(body.get("max_scenes", 4)), 30)),
                sensors=sensors,
                reprocess=bool(body.get("reprocess", False)),
                refresh_outline=bool(body.get("refresh_outline", False)),
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(jobs.submit(a, opts)), 202

    @app.get("/api/airports/<airport_id>/job")
    def job(airport_id):
        find(airport_id)
        return jsonify(jobs.get(airport_id))

    return app
