"""Airport analysis: find passes -> read the airport window -> detect aircraft -> pictures -> store."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .airfield import get_outline, outline_mask
from .airports import Airport, Grid
from .config import Settings
from .db import Store, scene_key
from .detect import Detection, OpticalParams, RadarParams, detect_optical, detect_radar
from .raster import read_pass
from .render import save_annotated, save_chip, save_overlay, to_rgba
from .stac import Scene, search_passes

log = logging.getLogger(__name__)

SENSOR_NAMES = {"S2": "Sentinel-2", "S1": "Sentinel-1"}
BOX_PAD_PX = 1.0


@dataclass
class AnalyseOptions:
    start: str
    end: str
    max_cloud: float = 60.0          # tile cloud cover for the catalogue search; the airport itself is checked locally
    max_scenes: int = 4              # newest passes per satellite type
    sensors: tuple[str, ...] = ("S2", "S1")
    reprocess: bool = False
    refresh_outline: bool = False
    optical: OpticalParams = field(default_factory=OpticalParams)
    radar: RadarParams = field(default_factory=RadarParams)


@dataclass
class AnalyseReport:
    airport: str = ""
    outline: str = ""
    found: dict = field(default_factory=dict)
    processed: int = 0
    cloudy: int = 0
    nodata: int = 0
    failed: int = 0
    already_done: int = 0
    planes: int = 0
    current: str = ""
    log: list[str] = field(default_factory=list)


def find_passes(settings: Settings, airport: Airport, opts: AnalyseOptions, sensor: str) -> list[Scene]:
    bbox = airport.grid().bounds_lonlat()
    cfg = settings.s2_config() if sensor == "S2" else settings.s1_config()
    return search_passes(cfg, sensor, bbox, opts.start, opts.end,
                         max_cloud=opts.max_cloud if sensor == "S2" else None, limit=opts.max_scenes)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def detection_rows(dets: list[Detection], grid: Grid) -> list[dict]:
    rows = []
    for i, d in enumerate(dets, 1):
        lon, lat = grid.lonlat(d.row, d.col)
        w, s = grid.lonlat(d.box_r1 + BOX_PAD_PX, d.box_c0 - BOX_PAD_PX)
        e, n = grid.lonlat(d.box_r0 - BOX_PAD_PX, d.box_c1 + BOX_PAD_PX)
        rows.append({
            "num": i, "lon": lon, "lat": lat, "box_w": w, "box_s": s, "box_e": e, "box_n": n,
            "size_m": d.size_m, "min_m": d.min_m, "area_m2": d.area_m2, "contrast": d.contrast,
            "snr": d.snr, "size_class": d.size_class, "confidence": d.confidence,
        })
    return rows


def process_pass(settings: Settings, store: Store, airport: Airport, scene: Scene, mask: np.ndarray | None,
                 opts: AnalyseOptions) -> tuple[str, int]:
    """Analyse one pass over one airport. Returns (status, aircraft found)."""
    grid = airport.grid()
    cfg = settings.s2_config() if scene.sensor == "S2" else settings.s1_config()
    bands = read_pass(scene, grid, cfg)
    if scene.sensor == "S2":
        dets, clear = detect_optical(bands, mask, opts.optical, grid.pixel_m)
        has_data = np.isfinite(bands["red"]).mean()
        min_clear = opts.optical.min_clear_fraction
    else:
        dets, clear = detect_radar(bands, mask, opts.radar, grid.pixel_m)
        has_data = (np.isfinite(bands["vv"]) & (bands["vv"] > 0)).mean()
        min_clear = 0.3

    key = scene_key(airport.id, scene.sensor, scene.id)
    if has_data < 0.3:
        store.save_scene(airport.id, scene, "nodata", f"only {has_data:.0%} of the airport imaged",
                         clear_fraction=clear)
        store.replace_detections(key, airport.id, scene, [])
        return "nodata", 0

    folder = settings.images_dir / airport.id
    stem = f"{scene.sensor}_{scene.datetime[:10]}_{_safe(scene.id)}"
    rgba = to_rgba(bands, scene.sensor)
    overlay = save_overlay(rgba, folder / f"{stem}.png")
    title = f"{airport.name}  {SENSOR_NAMES[scene.sensor]}  {scene.datetime[:16].replace('T', ' ')} UTC"
    status = "done"
    if scene.sensor == "S2" and clear < min_clear:
        status, dets = "cloudy", []
    annotated = save_annotated(rgba, dets, folder / f"{stem}_boxes.png", title, grid.pixel_m)
    rows = detection_rows(dets, grid)
    for row, d in zip(rows, dets):
        try:
            chip = save_chip(rgba, d, folder / f"{stem}_chips" / f"{row['num']:03d}.png", scene.datetime[:10])
            row["chip"] = str(chip.relative_to(settings.images_dir))
        except Exception as exc:  # a broken chip must not lose the detection
            log.warning("Chip %d failed: %s", row["num"], exc)
    note = (f"{len(dets)} aircraft" if status == "done"
            else f"cloud over the airfield ({1 - clear:.0%})")
    store.save_scene(airport.id, scene, status, note, clear_fraction=round(clear, 3), n_planes=len(dets),
                     overlay=str(overlay.relative_to(settings.images_dir)),
                     annotated=str(annotated.relative_to(settings.images_dir)),
                     bounds=list(grid.bounds_lonlat()))
    store.replace_detections(key, airport.id, scene, rows)
    return status, len(dets)


def analyse_airport(settings: Settings, airport: Airport, opts: AnalyseOptions, store: Store | None = None,
                    on_progress: Callable[[AnalyseReport], None] | None = None) -> AnalyseReport:
    settings.ensure_dirs()
    own_store = store is None
    store = store or Store(settings.db_path)
    report = AnalyseReport(airport=airport.id)

    def note(msg: str) -> None:
        log.info(msg)
        report.log.append(msg)
        del report.log[:-200]
        if on_progress:
            on_progress(report)

    try:
        note(f"{airport.name}: analysing {opts.start} .. {opts.end}")
        outline, source = get_outline(airport, settings.airfields_dir, settings.osm_dir,
                                      settings.overpass_urls(), settings.use_osm, opts.refresh_outline)
        report.outline = source
        mask = outline_mask(outline, airport.grid())
        if mask is not None and not mask.any():
            mask = None
        note(f"Airfield outline: {source}" + ("" if mask is not None else " - searching the whole box"))

        passes: list[Scene] = []
        for sensor in opts.sensors:
            try:
                found = find_passes(settings, airport, opts, sensor)
            except Exception as exc:
                log.exception("%s search failed", sensor)
                note(f"{SENSOR_NAMES[sensor]} search failed: {exc}")
                report.found[sensor] = 0
                continue
            report.found[sensor] = len(found)
            note(f"{SENSOR_NAMES[sensor]}: {len(found)} passes")
            passes.extend(found)

        for i, scene in enumerate(passes, 1):
            key = scene_key(airport.id, scene.sensor, scene.id)
            if not opts.reprocess and store.scene_status(key) in ("done", "cloudy", "nodata"):
                report.already_done += 1
                continue
            report.current = key
            note(f"[{i}/{len(passes)}] {SENSOR_NAMES[scene.sensor]} {scene.datetime[:16].replace('T', ' ')}"
                 + (f" (tile cloud {scene.cloud_cover:.0f}%)" if scene.cloud_cover is not None else ""))
            try:
                status, n = process_pass(settings, store, airport, scene, mask, opts)
            except Exception as exc:
                log.exception("Pass %s failed", scene.id)
                store.save_scene(airport.id, scene, "failed", str(exc)[:500])
                report.failed += 1
                note(f"  failed: {exc}")
                continue
            if status == "done":
                report.processed += 1
                report.planes += n
                note(f"  {n} aircraft")
            elif status == "cloudy":
                report.cloudy += 1
                note("  airfield under cloud - skipped")
            else:
                report.nodata += 1
                note("  airport not imaged on this pass")
        store.update_flags(airport.id)
        report.current = ""
        note(f"Finished: {report.processed} analysed, {report.cloudy} cloudy, {report.nodata} no data, "
             f"{report.failed} failed, {report.already_done} already done, {report.planes} aircraft")
    finally:
        if own_store:
            store.close()
    return report
