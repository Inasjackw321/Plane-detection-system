"""Command line: python -m planetracker <command> ..."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import S2_SOURCES, Settings


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _add_window_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--airport", default="all", help="airport id from config/airports.json, or 'all'")
    p.add_argument("--start", default=None, help="start date YYYY-MM-DD (default: --days ago)")
    p.add_argument("--end", default=None, help="end date YYYY-MM-DD (default: today)")
    p.add_argument("--days", type=int, default=30, help="look-back window when --start is omitted")
    p.add_argument("--max-cloud", type=float, default=60.0, help="max tile cloud cover %% (default 60)")
    p.add_argument("--max-scenes", type=int, default=4, help="newest passes per satellite (default 4)")
    p.add_argument("--sensors", default="S2,S1", help="S2, S1 or S2,S1 (default)")


def _settings(args) -> Settings:
    s = Settings()
    if args.data:
        s.data_dir = Path(args.data)
    if args.airports:
        s.airports_path = Path(args.airports)
    if args.s2_source:
        s.s2_source = args.s2_source
    return s


def _opts(args):
    from .pipeline import AnalyseOptions
    sensors = tuple(x.strip().upper() for x in args.sensors.split(",") if x.strip())
    bad = [x for x in sensors if x not in ("S1", "S2")]
    if bad:
        raise SystemExit(f"unknown sensor(s) {bad}; use S2 and/or S1")
    return AnalyseOptions(start=args.start or _days_ago(args.days), end=args.end or date.today().isoformat(),
                          max_cloud=args.max_cloud, max_scenes=args.max_scenes, sensors=sensors)


def _airports(settings: Settings, which: str):
    from .airports import load_airports
    all_ = load_airports(settings.airports_path)
    if which == "all":
        return all_
    chosen = [a for a in all_ if a.id == which]
    if not chosen:
        raise SystemExit(f"unknown airport {which!r}; choose from {[a.id for a in all_]} or 'all'")
    return chosen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="planetracker",
                                 description="Aircraft detection at airports in Sentinel-1 and Sentinel-2 imagery")
    ap.add_argument("--data", help="data directory (images, database)")
    ap.add_argument("--airports", help="airports JSON (default: config/airports.json)")
    ap.add_argument("--s2-source", choices=sorted(S2_SOURCES), help="Sentinel-2 catalogue (default: earth-search)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("airports", help="list the airports")

    p = sub.add_parser("search", help="list Sentinel passes over the airports (no download)")
    _add_window_args(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("analyse", help="detect aircraft at the airports")
    _add_window_args(p)
    p.add_argument("--reprocess", action="store_true", help="re-run passes that were already analysed")
    p.add_argument("--refresh-outline", action="store_true", help="download the OpenStreetMap outline again")

    p = sub.add_parser("watch", help="analyse every airport repeatedly")
    p.add_argument("--every-hours", type=float, default=12.0)
    p.add_argument("--days", type=int, default=10, help="look-back window on each run")
    p.add_argument("--max-scenes", type=int, default=4)

    p = sub.add_parser("serve", help="web map and API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--open", action="store_true", help="open the map in the web browser")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    settings = _settings(args)

    if args.cmd == "airports":
        from .airports import load_airports
        for a in load_airports(settings.airports_path):
            print(f"{a.id:10s} {a.lat:9.5f} {a.lon:10.5f}  {a.name} ({a.country})")
        return 0

    if args.cmd == "search":
        from .pipeline import find_passes
        opts = _opts(args)
        out = {}
        for a in _airports(settings, args.airport):
            for sensor in opts.sensors:
                passes = find_passes(settings, a, opts, sensor)
                out.setdefault(a.id, {})[sensor] = [s.to_dict() for s in passes]
                if not args.json:
                    for s in passes:
                        cc = f"cloud={s.cloud_cover:.0f}%" if s.cloud_cover is not None else ""
                        print(f"{a.id:10s} {sensor} {s.datetime[:19]}  {s.id:50s} cover={s.coverage:.0%} {cc}")
        if args.json:
            print(json.dumps(out, indent=2))
        return 0

    if args.cmd == "analyse":
        from .pipeline import analyse_airport
        opts = _opts(args)
        opts.reprocess = args.reprocess
        opts.refresh_outline = args.refresh_outline
        failed = 0
        for a in _airports(settings, args.airport):
            rep = analyse_airport(settings, a, opts)
            failed += rep.failed and not rep.processed
        return 1 if failed else 0

    if args.cmd == "watch":
        from .pipeline import AnalyseOptions, analyse_airport
        while True:
            now = datetime.now(timezone.utc)
            opts = AnalyseOptions(start=(now - timedelta(days=args.days)).date().isoformat(),
                                  end=now.date().isoformat(), max_scenes=args.max_scenes)
            for a in _airports(settings, "all"):
                try:
                    analyse_airport(settings, a, opts)
                except Exception:
                    logging.exception("Analysis of %s failed; will retry next cycle", a.id)
            logging.info("Next run in %.1f h", args.every_hours)
            time.sleep(args.every_hours * 3600)

    if args.cmd == "serve":
        from .server import create_app
        url = f"http://{args.host}:{args.port}"
        if args.open:
            threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        logging.info("Plane Tracker running at %s (Ctrl+C to stop)", url)
        create_app(settings).run(host=args.host, port=args.port, threaded=True)
        return 0
    return 2


def run() -> int:
    import requests
    try:
        return main()
    except requests.RequestException as exc:
        logging.error("Network error talking to the imagery catalogue: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(run())
