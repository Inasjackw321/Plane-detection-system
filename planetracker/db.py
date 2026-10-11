"""SQLite storage for analysed passes and aircraft detections."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenes (
    key TEXT PRIMARY KEY,          -- airport:sensor:scene id
    airport_id TEXT NOT NULL,
    sensor TEXT NOT NULL,          -- S2 | S1
    scene_id TEXT NOT NULL,
    datetime TEXT NOT NULL,
    ts REAL NOT NULL,
    platform TEXT,
    orbit TEXT,
    cloud_cover REAL,
    coverage REAL,
    clear_fraction REAL,
    status TEXT NOT NULL,          -- done | cloudy | nodata | failed
    note TEXT,
    n_planes INTEGER DEFAULT 0,
    processed_at REAL,
    overlay TEXT,
    annotated TEXT,
    bounds TEXT
);
CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_key TEXT NOT NULL REFERENCES scenes(key),
    airport_id TEXT NOT NULL,
    sensor TEXT NOT NULL,
    datetime TEXT NOT NULL,
    ts REAL NOT NULL,
    num INTEGER NOT NULL,          -- number on the annotated image
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    box_w REAL, box_s REAL, box_e REAL, box_n REAL,
    size_m REAL,
    min_m REAL,
    area_m2 REAL,
    contrast REAL,
    snr REAL,
    size_class TEXT,
    confidence REAL,
    persistent INTEGER DEFAULT 0,   -- other passes of the same satellite type with an object here
    corroborated INTEGER DEFAULT 0, -- passes of the other satellite type within CORROBORATE_DAYS with an object here
    chip TEXT
);
CREATE INDEX IF NOT EXISTS idx_det_airport ON detections(airport_id, ts);
CREATE INDEX IF NOT EXISTS idx_det_scene ON detections(scene_key);
CREATE INDEX IF NOT EXISTS idx_scene_airport ON scenes(airport_id, ts);
"""

SAME_SPOT_M = 30
CORROBORATE_DAYS = 3


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _dist_m(lon1, lat1, lon2, lat2) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * k, lat2 - lat1) * 111_320


def scene_key(airport_id: str, sensor: str, scene_id: str) -> str:
    return f"{airport_id}:{sensor}:{scene_id}"


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # scenes ---------------------------------------------------------------

    def scene_status(self, key: str) -> str | None:
        row = self.conn.execute("SELECT status FROM scenes WHERE key=?", (key,)).fetchone()
        return row["status"] if row else None

    def save_scene(self, airport_id: str, scene, status: str, note: str = "", clear_fraction=None,
                   n_planes: int = 0, overlay: str | None = None, annotated: str | None = None,
                   bounds=None) -> str:
        key = scene_key(airport_id, scene.sensor, scene.id)
        with self.lock, self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO scenes
                   (key, airport_id, sensor, scene_id, datetime, ts, platform, orbit, cloud_cover, coverage,
                    clear_fraction, status, note, n_planes, processed_at, overlay, annotated, bounds)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, airport_id, scene.sensor, scene.id, scene.datetime, _ts(scene.datetime),
                 scene.platform, scene.orbit, scene.cloud_cover, scene.coverage, clear_fraction, status,
                 note, n_planes, time.time(), overlay, annotated, json.dumps(bounds) if bounds else None),
            )
        return key

    def scenes(self, airport_id: str, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM scenes WHERE airport_id=? ORDER BY ts DESC LIMIT ?", (airport_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["bounds"] = json.loads(d["bounds"]) if d["bounds"] else None
            out.append(d)
        return out

    # detections -----------------------------------------------------------

    def replace_detections(self, key: str, airport_id: str, scene, rows: list[dict]) -> None:
        ts = _ts(scene.datetime)
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM detections WHERE scene_key=?", (key,))
            self.conn.executemany(
                """INSERT INTO detections
                   (scene_key, airport_id, sensor, datetime, ts, num, lon, lat, box_w, box_s, box_e, box_n,
                    size_m, min_m, area_m2, contrast, snr, size_class, confidence, chip)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(key, airport_id, scene.sensor, scene.datetime, ts, r["num"], r["lon"], r["lat"],
                  r["box_w"], r["box_s"], r["box_e"], r["box_n"], r["size_m"], r["min_m"], r["area_m2"],
                  r["contrast"], r["snr"], r["size_class"], r["confidence"], r.get("chip"))
                 for r in rows],
            )

    def update_flags(self, airport_id: str) -> None:
        """Recompute, for every detection at an airport, how often the same spot was occupied
        on other passes (persistent) and whether the other satellite saw it too (corroborated)."""
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, scene_key, sensor, ts, lon, lat FROM detections WHERE airport_id=?", (airport_id,))]
        updates = []
        for d in rows:
            same, other = set(), set()
            for e in rows:
                if e["scene_key"] == d["scene_key"]:
                    continue
                if _dist_m(d["lon"], d["lat"], e["lon"], e["lat"]) > SAME_SPOT_M:
                    continue
                if e["sensor"] == d["sensor"]:
                    same.add(e["scene_key"])
                elif abs(e["ts"] - d["ts"]) <= CORROBORATE_DAYS * 86400:
                    other.add(e["scene_key"])
            updates.append((len(same), len(other), d["id"]))
        with self.lock, self.conn:
            self.conn.executemany("UPDATE detections SET persistent=?, corroborated=? WHERE id=?", updates)

    def detections(self, airport_id: str, scene_key: str | None = None, sensor: str | None = None,
                   start: str | None = None, end: str | None = None, min_confidence: float = 0,
                   limit: int = 20000) -> list[dict]:
        q = ["SELECT * FROM detections WHERE airport_id=? AND confidence >= ?"]
        args: list = [airport_id, min_confidence]
        if scene_key:
            q.append("AND scene_key=?")
            args.append(scene_key)
        if sensor:
            q.append("AND sensor=?")
            args.append(sensor)
        if start:
            q.append("AND ts >= ?")
            args.append(_ts(start if "T" in start else start + "T00:00:00+00:00"))
        if end:
            q.append("AND ts <= ?")
            args.append(_ts(end if "T" in end else end + "T23:59:59+00:00"))
        q.append("ORDER BY ts DESC, num ASC LIMIT ?")
        args.append(limit)
        return [dict(r) for r in self.conn.execute(" ".join(q), args).fetchall()]

    def summary(self, airport_id: str) -> dict:
        """Latest usable pass of each satellite and its aircraft count."""
        out: dict = {"scenes_done": 0, "last_processed": None}
        c = self.conn
        out["scenes_done"] = c.execute(
            "SELECT COUNT(*) FROM scenes WHERE airport_id=? AND status='done'", (airport_id,)).fetchone()[0]
        out["last_processed"] = c.execute(
            "SELECT MAX(processed_at) FROM scenes WHERE airport_id=?", (airport_id,)).fetchone()[0]
        for sensor in ("S2", "S1"):
            row = c.execute(
                "SELECT key, datetime, n_planes FROM scenes WHERE airport_id=? AND sensor=? AND status='done'"
                " ORDER BY ts DESC LIMIT 1", (airport_id, sensor)).fetchone()
            out[sensor] = dict(row) if row else None
        return out
