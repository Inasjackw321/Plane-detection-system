# Plane Tracker: aircraft at airports in Sentinel-1 and Sentinel-2 imagery

This tool finds aircraft parked at airports in free **Sentinel-2** optical imagery and **Sentinel-1** radar imagery,
and **draws a box round each one**. It works like the ship tracker: a web map, an SQLite store, and an image chip of
every detection. The difference is that you **click an airport** and it analyses that airport.

| Airport | Location | Coordinates |
|---|---|---|
| Engels-2 Air Base | Saratov Oblast, Russia | 51.48058, 46.21039 |
| Ukrainka Air Base | Amur Oblast, Russia | 51.17000, 128.44500 (51°10'12"N 128°26'42"E) |
| Al Udeid Air Base | Qatar | 25.13567, 51.31499 |
| Ovda Air Base | Israel | 29.93608, 34.93717 |
| Souda Bay / Chania Airport | Crete, Greece | 35.53319, 24.15532 |

The airports are listed in `config/airports.json`. You can add more there.

```
click airport ──► STAC search ──► read airport window ──► airside mask ──► detect ──► boxes + chips ──► SQLite ──► map
                  S2 + S1          (HTTP range reads)       (OpenStreetMap)   S2 / S1
```

## Quick start

```bash
pip install -r requirements.txt
python run_app.py                 # opens http://127.0.0.1:8000 in your browser
```

You can also double-click `start_windows.bat` on Windows or run `./start_mac_linux.sh` on macOS/Linux.
You don't need any accounts or API keys.

**Using the map**

1. **Click an airport**, either its ✈ label on the map or its name in the list. The map zooms in and analysis starts
   straight away. It finds the newest passes of each satellite over the last 30 days. If the airport was analysed
   in the last 12 hours, the stored results are shown instead. Press **Analyse** to run it again.
2. Progress appears live. Each pass appears under **Passes** when it finishes.
3. **Pick a pass.** That day's Sentinel image is laid over the map, and every aircraft gets a **box**. The box colour
   shows its size:

   | Colour | Size | Typical aircraft |
   |---|---|---|
   | red | ≥ 45 m | strategic bomber, tanker, heavy transport, wide-body |
   | orange | 32–45 m | medium bomber, airliner, tactical transport |
   | yellow | 20–32 m | fighter-bomber, business jet |
   | blue | < 20 m | fighter, helicopter, light aircraft (or a vehicle) |

   A dashed box means confidence is below 60 %. Click a box or a list entry to see a close-up chip with the size,
   contrast and confidence. The popup also shows whether the **other satellite** saw an aircraft at the same spot
   within 3 days, and whether the spot was **occupied on other passes**. **annotated image** opens a PNG of the
   whole airfield with numbered boxes, ready to share.

Change the date range, the number of passes per satellite or the satellites in the **Analyse** panel.
**Min confidence** hides weak detections.

## Command line

| Command | What it does |
|---|---|
| `python -m planetracker airports` | List the airports |
| `python -m planetracker search --airport engels --days 30` | List passes over an airport (no download) |
| `python -m planetracker analyse --airport all --days 30 --max-scenes 4` | Analyse from the terminal |
| `python -m planetracker watch --every-hours 12` | Keep analysing every airport for new passes |
| `python -m planetracker serve [--open]` | Web map and JSON API |

Options: `--sensors S2,S1`, `--start/--end YYYY-MM-DD`, `--max-cloud` (tile cloud %), `--reprocess`,
`--refresh-outline`, `--data DIR` (default `./data`), and `--s2-source earth-search|planetary-computer`.

## Data sources

- **Sentinel-2 L2A** (optical, 10 m; blue, green, red, near-infrared and the scene classification). This comes from
  [Element 84 Earth Search](https://earth-search.aws.element84.com/v1) (`sentinel-2-c1-l2a`) or Microsoft Planetary
  Computer. It sees aircraft paint and shadows, but only in daylight and without cloud.
- **Sentinel-1 RTC** (radar, 10 m pixels with about 20 m resolution, VV and VH). This comes from
  [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/dataset/sentinel-1-rtc) (`sentinel-1-rtc`),
  which signs URLs anonymously. Radar sees through cloud and works at night. Metal airframes are bright on smooth,
  radar-dark tarmac.
- **Airfield outlines** come from OpenStreetMap through the Overpass API: aprons, taxiways and parking positions,
  minus buildings and hangars. They are cached in `data/osm/`.

**No tiles are downloaded.** Every image is a cloud-optimised GeoTIFF, and only the few hundred kilobytes over the
airport (6–8 km square) are read with HTTP range requests. They are warped onto a 10 m grid centred on the airport,
so every date and both satellites line up exactly with each other and with the web map.

## How detection works (`planetracker/detect.py`)

1. **Where to look.** Only airside pavement is searched. This is what keeps buildings, cars, lamp posts and fields
   out of the results. If OpenStreetMap has no aprons for an airport, the whole box is searched and confidence is
   reduced. You can also draw your own outline (see below).
2. **Sentinel-2.**
   - Clouds and cloud shadows are removed using the scene classification, plus 50 m around them. Patches the size
     of an aircraft are kept, because the classification often labels a white aircraft as "cloud".
   - For each of the four 10 m bands, the mean and spread of the surrounding pavement are measured in a 210 m
     window. Vegetation and water are left out of that background.
   - A pixel's distance from the pavement, in standard deviations and combined over the four bands, is its
     anomaly. Aircraft can be **brighter** (white paint) or **darker** (grey paint, shadow) than concrete, and both
     count.
   - A seed needs an anomaly of 5σ and a difference of at least 0.03 reflectance. It is then grown to the connected
     pixels above 2.5σ. A second pass recomputes the background without the first pass's hits, so large aircraft
     don't hide each other.
   - If less than 30 % of the airside area is clear, the pass is marked *cloudy*.
3. **Sentinel-1.** VV + VH backscatter is smoothed over 3 × 3 pixels against speckle. The same local-background test
   is then run in decibels: a seed must be 4.5σ and at least 6 dB above the tarmac around it.
4. **Size and shape.** The object's extent is measured along 8 directions using a max-projection profile, corrected
   for the sensor blur. The largest extent is the **size**, which is about the larger of length and wingspan. The
   ratio of largest to smallest extent rejects lines, such as vehicle queues, fences and pavement markings, because
   aircraft are roughly as wide as they are long. Objects must be 10–95 m across.
5. **Box.** The visible extent north–south and east–west, plus 10 m, is drawn on the map, on the annotated image and
   on each chip.
6. **Cross-checks.**
   - **Corroborated:** the other satellite saw an object within 30 m within 3 days.
   - **Same spot before:** the same satellite saw an object at that spot on another pass. This could be a parked
     aircraft that hasn't moved, or a fixed object.
7. **Confidence** combines anomaly strength, plausible size, shape, and whether an airfield outline was used.

**Accuracy.** On synthetic airfields with swept-wing aircraft rendered at 1 m and blurred with the Sentinel-2
point-spread function, every aircraft from a 23 m Su-34 to a 56 m Tu-160 is found, in white or grey paint. The
position is within about 20 m, and the Sentinel-2 size is within about 25 % (or 12 m). There are no false alarms on
empty airfields, buildings or speckle. On Sentinel-1 every large aircraft is found, but the size is only good to
about ±20 m. Real imagery will be less clean, for these reasons:

- Fighters (15–20 m) are only 2 pixels across. They are found when their contrast with the pavement is strong, and
  are easily missed under camouflage or in shelters.
- Aircraft parked close together, nose to tail, can merge into one box.
- Ground vehicles, containers and equipment on aprons can look like small aircraft. Treat the small (blue) class
  with care.
- Aircraft inside hangars or hardened shelters are invisible to both satellites.
- Sentinel-2 passes about every 5 days and Sentinel-1 about every 6–12 days, depending on the area.

## Changing the airports or the search area

- **Airports:** edit `config/airports.json` (`id`, `name`, `country`, `lat`, `lon`, `half_size_m` = half the width of
  the analysed box).
- **Search area:** to replace the OpenStreetMap outline, put a GeoJSON polygon of the aprons (lon/lat) in
  `config/airfields/<airport id>.geojson`. Run with `--refresh-outline` to download OpenStreetMap again.

## API

| Endpoint | |
|---|---|
| `GET /api/airports` | Airports with their latest result per satellite |
| `GET /api/airports/<id>` | One airport: passes (status, aircraft count, image URLs, bounds) and job status |
| `POST /api/airports/<id>/analyse` `{start, end, max_scenes, max_cloud, sensors, reprocess}` | Start an analysis (runs in the background, one airport at a time) |
| `GET /api/airports/<id>/job` | Progress of that analysis |
| `GET /api/airports/<id>/detections?scene=<key>&sensor=&min_confidence=` | GeoJSON: one box polygon per aircraft, with size, class, confidence, cross-checks and chip URL |
| `GET /api/airports/<id>/outline` | Airside search area |
| `GET /images/...` | Image overlays, annotated images and chips |

## Project layout

```
config/airports.json     the airports
planetracker/
  airports.py            airport list and the 10 m Web Mercator grid over each one
  stac.py                Sentinel-1 / Sentinel-2 catalogue search, grouping tiles into passes
  raster.py              windowed reads of the airport from cloud-optimised GeoTIFFs
  airfield.py            OpenStreetMap aprons/taxiways -> search mask
  detect.py              optical and radar aircraft detectors, size measurement
  render.py              image overlay, annotated image with boxes, chips
  pipeline.py            analyse one airport: search -> read -> detect -> store
  db.py                  SQLite store, cross-satellite and repeat-spot checks
  server.py              Flask web map + JSON API
  web/                   the map (Leaflet is bundled, BSD-2 licence)
tests/                   pytest suite (synthetic imagery; no internet needed)
```

## Tests

```bash
pip install -r requirements.txt
python -m pytest -q
```

- `tests/test_detect.py` checks detection, position and size on synthetic aircraft for both sensors. It also checks
  for no false alarms on empty airfields, and for clouds being handled.
- `tests/test_airfield.py` checks how OpenStreetMap is parsed and how the grid is laid out.
- `tests/test_pipeline.py` runs the whole chain end to end. A local HTTP server plays both STAC APIs and the imagery
  buckets, serving UTM GeoTIFFs with range requests. The test checks the search, the windowed reads, the warp onto
  the airport grid, both detectors, the cross-satellite check, the chips and the web API.

Imagery: Copernicus Sentinel data, processed by ESA. Airfield outlines: © OpenStreetMap contributors (ODbL).
