# Earthquake Monitor + Shaking Estimator

Real-time global earthquake detection combined with scientifically grounded,
**region-aware** ground-motion (shaking intensity) estimation and geographic
visualization for configurable target locations.

## What this does

1. **Detects earthquakes in near-real-time** from two independent public
   catalogs — no raw seismic station data involved (see "Why catalogs, not
   raw stations" below).
2. **Deduplicates** events reported by both sources into a single alert.
3. For events above a magnitude threshold, **classifies the event's tectonic
   region** and **selects an appropriate GMPE** for it (a Middle East
   earthquake and a Hawaii volcanic earthquake use different models — see
   "Region-aware GMPE selection" below).
4. **Estimates expected shaking** (Peak Ground Acceleration, PGA, in %g, plus
   a human-readable Modified Mercalli Intensity damage description) at each
   configured target location within valid range, using real per-location
   soil-condition (Vs30) data.
5. **Generates three shake map images** per qualifying event: a regional
   overview, a street-level view, and a tight building-scale close-up — each
   with a real geographic basemap (roads, terrain, coastlines).

This is **detection and estimation, not prediction** — it reports
earthquakes that have already occurred, as soon as they're published, and
estimates their impact. No system can forecast an earthquake before it
happens.

## Architecture

```
EMSC WebSocket (push)  ──┐
                          ├──► Dedup ──► magnitude ≥ threshold? ──► classify region
USGS GeoJSON (polled)  ──┘                                              │
                                                                          ▼
                                                          select region-appropriate GMPE
                                                                          │
                                                                          ▼
                                                    compute PGA + MMI at each target
                                                    (real Vs30, real distance)
                                                                          │
                                                                          ▼
                                              3x shake map PNGs (regional / street / zoom)
                                              with real basemap tiles + coastlines
```

- **EMSC** (European-Mediterranean Seismological Centre): real-time
  WebSocket, push-based, low latency.
- **USGS**: polled every N seconds, global coverage, independent detection
  pipeline from EMSC.
- **hazardlib** (`openquake.hazardlib`): real, peer-reviewed GMPEs, with the
  specific model selected per event based on its tectonic region (see below).
- **Vs30** (site soil condition): looked up live per target location from
  USGS's Global Vs30 Mosaic (Heath et al. 2020), not a flat assumption.
- **Basemap tiles**: Esri's public tile services (via `contextily`), with
  automatic fallback to bare coastline outlines (via `geopandas`/
  `geodatasets`) if tile fetching fails for any reason.

### Region-aware GMPE selection

A single fixed GMPE will still compute *a* number for an earthquake
anywhere on Earth — it has no built-in awareness of geography — but the
result silently degrades in accuracy the further the event is from the
tectonic setting the model was actually calibrated on. This project instead
classifies each event's location against a table of rough regional bounding
boxes and picks an appropriate model:

| Region | Tectonic type | GMPE used |
|---|---|---|
| Middle East / Mediterranean | Active Shallow Crust | Akkar et al. (2014) — calibrated on European/Middle Eastern recordings, matching a real published GEM Foundation regional hazard model for this area |
| Western North America | Active Shallow Crust (California-calibrated) | Boore, Stewart, Seyhan & Atkinson (2014) — NGA-West2 |
| Hawaii | **Volcanic** ⚠️ | Boore et al. (2014), used as a rough proxy — **explicitly flagged with a caution note**, since volcanic/flank earthquakes aren't well represented by any standard tectonic GMPE |
| Alaska/Aleutians, Tonga/Kermadec, Japan, Indonesia | Subduction Interface | Zhao et al. (2006) |
| Central/Eastern US | Stable Continental | Atkinson & Boore (2006) |
| Anywhere unmatched | Generic fallback | Akkar et al. (2014), with a caution note that no specific region matched |

Bounding boxes are simple rectangles, not precise tectonic-plate geometry —
good enough to pick a sensible *category* of model, not a research-grade
regionalization. Both the terminal output and every map title show which
region and model were actually used for that event, and print an explicit
**CAUTION** line whenever the match is imperfect (as with Hawaii).

### Why catalogs, not raw seismic stations

Raw station-level detection (e.g. STA/LTA triggering on live waveforms)
was the original approach explored for this project, but hit real
technical walls:
- Israel's `IS` network has station metadata but no public waveform
  data via FDSN/EIDA.
- GEOFON's `GE` network has waveform access via FDSN, but real-time
  SeedLink streaming failed due to a server protocol mismatch (GEOFON's
  newer "HMB SeedLink" vs. ObsPy's classic SeedLink client).

Catalog aggregators (EMSC, USGS) sidestep both problems: they already
process detections from dense national networks worldwide and publish
results fast, without requiring direct station access.

## Setup

**Recommended: Docker** (see "Running with Docker" below) -- this project's
dependencies (GDAL, openquake.hazardlib, a specific Python version) took
real, hard-won debugging to get installed correctly, and the included
`Dockerfile` bakes that exact working setup in so you never have to
repeat it. This is genuinely the easiest way to run this project,
especially if sharing it with someone else.

**If you want to run it directly on your machine instead** (without
Docker), it requires `openquake.hazardlib`, which needs **Python ≤3.12**
(not compatible with newer versions), plus GDAL, which has proven
unreliable to install via pip alone -- use conda-forge for these
specific packages:

```bash
brew install miniconda        # if not already installed
conda init zsh                # or bash; restart terminal after
conda create -n openquake_conda python=3.12
conda activate openquake_conda
conda install -c conda-forge openquake.engine websocket-client requests contextily geodatasets flask folium
```

Run the monitor:
```bash
conda activate openquake_conda
python earthquake_monitor_with_shaking.py
```

Run the web dashboard (in a separate terminal, same environment):
```bash
conda activate openquake_conda
python dashboard.py
```
Then open `http://localhost:5001` in a browser.

## Configuration

All configurable parameters live in **`config.py`**, not scattered through
the main script. Every scalar setting (thresholds, URLs, credentials,
feature toggles) can be overridden via an **environment variable of the
same name** -- without editing `config.py` or rebuilding a Docker image.
If no environment variable is set, the default value in `config.py` is
used, so nothing changes for basic usage.

**Quick one-off override:**
```bash
python earthquake_monitor_with_shaking.py --test-map  # uses config.py defaults

SHAKING_MAGNITUDE_THRESHOLD=3.0 python earthquake_monitor_with_shaking.py --test-map  # overridden
```

**Better for several settings at once -- a `.env` file** (Docker reads
this natively via `--env-file`, no extra Python dependency needed):
```bash
# .env  (add this file to .gitignore -- it can hold your email password!)
NOTIFY_EMAIL=true
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your16charapppassword
EMAIL_FROM=you@gmail.com
EMAIL_TO=you@gmail.com
SHAKING_MAGNITUDE_THRESHOLD=4.0
```

**Important `.env` formatting rules** (Docker's parser is strict):
- No spaces around `=` (`NOTIFY_EMAIL=true`, not `NOTIFY_EMAIL = true`)
- No inline comments after a value (`SMTP_PORT=587` on its own line, not
  `SMTP_PORT=587  # some comment` -- the comment gets read as part of the value)
- Booleans accept `true`/`false`/`1`/`0`/`yes`/`no` (case-insensitive)
- `BBOX` as `"minlat,maxlat,minlon,maxlon"`, e.g. `BBOX=25,45,20,50`, or
  `BBOX=none` for global
- `TARGET_LOCATIONS_JSON` for a full override of target cities, e.g.
  `TARGET_LOCATIONS_JSON=[["Tel Aviv",32.0853,34.7818],["Rome",41.9,12.5]]`

**Structured settings that stay in `config.py` itself** (too complex to
represent as a single environment variable) -- edit these directly and
rebuild/restart if you need to change them: `REGION_GMPE_TABLE`,
`DEFAULT_REGION`, and the default `TARGET_LOCATIONS` list (which
`TARGET_LOCATIONS_JSON` can override at runtime without editing the file).

## Usage examples

### Main monitor (live, runs until Ctrl+C)

```bash
conda activate openquake_conda
python earthquake_monitor_with_shaking.py
```

### Instant test map (no waiting on real events)

```bash
# Default: synthetic M6.5 near Hilo, Hawaii (tests the volcanic-region
# caution-flagging path)
python earthquake_monitor_with_shaking.py --test-map

# Dead Sea Transform preset (tests Middle East / Akkar routing, near
# your Tel Aviv / Jerusalem targets)
python earthquake_monitor_with_shaking.py --test-map --test-preset dead_sea

# Fully custom location/magnitude/depth
python earthquake_monitor_with_shaking.py --test-map \
    --test-lat 31.6 --test-lon 35.4 --test-mag 7.0 --test-depth 15
```

Sample real output (captured during testing — a real M5.3 near Tonga):

```
  *** NEW EVENT (USGS) ***
      M5.3  26 km NW of Neiafu, Tonga
      Location: -18.8489, -173.9417  Depth: 35 km

  --- Estimated shaking (Akkar Et Al Rjb 2014) for M5.3 26 km NW of Neiafu, Tonga ---
      Region: Unclassified (default fallback) (Active Shallow Crust (generic default))
      CAUTION: Event location didn't match any known region -- using a generic default model.
      Nuku'alofa, Tonga         287 km   Vs30=   232 m/s   PGA ~  0.075 %g   MMI I    Not felt
      Tel Aviv                16792 km   (beyond 300km -- outside AkkarEtAlRjb2014's valid range, skipped)
      Shake map (regional) saved: shakemaps/shakemap_..._TEST.png
      Shake map (street) saved: shakemaps/shakemap_..._TEST_street.png
      Shake map (zoom_street) saved: shakemaps/shakemap_..._TEST_zoom_street.png
```

Sample output for a Middle East test event (real captured run):

```
Generating a TEST shake map with a synthetic M6.5 event at (31.6, 35.4).

  --- Estimated shaking (AkkarEtAlRjb2014) for M6.5 TEST EVENT on the Dead Sea Transform, near Jericho ---
      Region: Middle East / Mediterranean (Active Shallow Crust)
      Jerusalem                 26 km   Vs30=   456 m/s   PGA ~  9.533 %g   MMI VI     Moderate -- felt by all, slight damage
      Tel Aviv                  80 km   Vs30=   268 m/s   PGA ~  2.831 %g   MMI IV-V   Light -- felt widely, dishes/windows rattle
```

Stop the live monitor anytime with `Ctrl+C`.

### Detection-only version (no shaking estimate)

```bash
python global_earthquake_monitor.py
```

### One-off shaking calculator (no live monitoring)

```bash
# hazardlib version (needs openquake_conda env)
python openquake_shaking_estimate.py --mag 6.5 --depth 10 --distances 10 30 50 100 --vs30 400

# pyGMM version (works outside openquake_conda, e.g. your normal Python)
python pygmm_shaking_estimate.py --model ChiouYoungs2014 --mag 6.5 --dist 10 30 50 100 --vs30 400
python pygmm_shaking_estimate.py --model all --mag 6.5 --dist 10 30 50 100 --vs30 400   # compare all known models
```

### Official USGS ShakeMap for a real cataloged event

```bash
python shakemap_tool.py --list-recent                # find an event ID
python shakemap_tool.py --official us7000abcd         # download its real ShakeMap images
```

### DIY illustrative shake estimate for a hypothetical scenario

```bash
python shakemap_tool.py --estimate --lat 34.0 --lon -118.2 --mag 6.5 --depth 10 \
    --targets "Los Angeles:34.05:-118.24" "San Diego:32.72:-117.16"
```

### Fetch raw European station waveforms (independent of the monitor)

```bash
python europe_stations_fetch.py --list-stations
python europe_stations_fetch.py --fetch STATIONCODE --hours-ago 1
```

## Configurable parameters

All parameters live in **`config.py`**. Each name below is also the exact
environment variable name that can override it (see "Configuration" above).

### Detection

| Parameter | Default | What it controls |
|---|---|---|
| `EMSC_WS_URL` | `wss://www.seismicportal.eu/standing_order/websocket` | EMSC's real-time event WebSocket endpoint. |
| `USGS_FEED_URL` | `.../summary/all_hour.geojson` | Which USGS feed to poll. Swap for `all_day.geojson` etc. for a longer lookback window per poll. |
| `USGS_POLL_INTERVAL_SEC` | `60` | How often (seconds) to re-poll USGS. Lower = fresher data, more requests. |
| `MIN_MAGNITUDE` | `0.0` | Minimum magnitude to track/report at all. `0.0` = show everything either source publishes. Raise (e.g. `2.5`) to cut noise. |
| `BBOX` | `None` (global) | Restrict tracked events to a region. Env var format: `"minlat,maxlat,minlon,maxlon"`, e.g. `25,45,20,50`. `none` = worldwide. |
| `DEDUP_TIME_SEC` | `90` | Max time difference (seconds) for two reports to be considered the same event. |
| `DEDUP_DIST_DEG` | `1.0` | Max lat/lon difference (degrees, ~100km) for two reports to be considered the same event. |
| `DEDUP_MAG_DELTA` | `1.0` | Max magnitude difference for two reports to be considered the same event (agencies often disagree slightly). |

### Notifications and history

| Parameter | Default | What it controls |
|---|---|---|
| `NOTIFY_MAGNITUDE_THRESHOLD` | `4.0` | Minimum magnitude that triggers a notification (independent of `SHAKING_MAGNITUDE_THRESHOLD`). |
| `NOTIFY_EMAIL` | `False` | Enable/disable email alerts via SMTP. |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `587` | SMTP server settings. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | `""` | SMTP login. For Gmail, `SMTP_PASSWORD` must be an App Password, not your normal password (see "Notifications" below). |
| `EMAIL_FROM` / `EMAIL_TO` | `""` | Sender/recipient addresses. |
| `WEBHOOK_URL` | `None` | Slack or Discord webhook URL. Unset disables webhook notifications. |
| `WEBHOOK_TYPE` | `"slack"` | `"slack"` or `"discord"` -- picks the correct JSON payload shape. |
| `HISTORY_DB_PATH` | `earthquake_history.db` | Path to the SQLite event-history database (also read by `dashboard.py`). |

### Shaking estimation

| Parameter | Default | What it controls |
|---|---|---|
| `SHAKING_MAGNITUDE_THRESHOLD` | `5.0` | Minimum magnitude that triggers a shaking computation. Below this, events are still reported but no GMPE/map is generated. |
| `TARGET_LOCATIONS` | 6 example cities | List of `(name, lat, lon)` tuples. Edit the default in `config.py`, or override entirely at runtime via `TARGET_LOCATIONS_JSON`. |
| `MAX_VALID_DISTANCE_KM` | `300` | Most GMPEs are only empirically calibrated up to a few hundred km from the source. Targets farther than this are skipped rather than shown as misleading near-zero values. |
| `REGION_GMPE_TABLE` | see table above | The region → tectonic type → GMPE lookup. Python-only (not env-overridable) -- edit `config.py` directly to add/adjust regions. |
| `DEFAULT_REGION` | Akkar (generic fallback) | Model used when no region in the table matches. Python-only, same as above. |

### Maps and visualization

| Parameter | Default | What it controls |
|---|---|---|
| `SHAKEMAP_OUTPUT_DIR` | `shakemaps` | Folder where generated shake map PNGs are saved. |
| `SHAKEMAP_VMAX` | `None` (auto) | Set a fixed number (%g) to force every map onto the same color scale, making different events visually comparable. `None`/unset = each map auto-scales to its own data. |
| `SHAKEMAP_LOG_SCALE` | `False` | Log color scale shows detail across huge magnitude ranges in one map, but visually compresses severity differences — read the tradeoff note in `config.py` before enabling. |
| `SHAKEMAP_USE_CONTEXTILY` | `True` | Use real web-map basemap tiles. Falls back to bare coastline outlines if tile fetching fails for any reason (network issue, provider blocking, etc). |
| `SHAKEMAP_CONTEXTILY_PROVIDER` | `Esri.WorldStreetMap` | Which tile provider/style to use. OpenStreetMap and CartoDB were both tried and found unreliable for this kind of automated (script-side) tile fetching — Esri's public tiles work without a key. (Note: the web dashboard's map uses OpenStreetMap directly instead, since those tiles load in the user's own browser, not fetched server-side, so the automated-traffic policy doesn't apply there.) |
| `STREET_MAP_SPAN_DEG` | `0.5` (~55km) | Span of the second (street-level) map output, centered on the closest in-range target. |
| `ZOOM_STREET_MAP_SPAN_DEG` | `0.03` (~3.3km) | Span of the third (building/street close-up) map output. |

### Vs30 lookup (site soil conditions)

`lookup_vs30(lat, lon)` queries USGS's real **Global Vs30 Mosaic** ArcGIS
service live for each target location and caches results per location
(so repeated events don't re-query the same city). No parameter to tune
here beyond the target list itself — this always reflects real data, not
an assumption.

### Adding a new tectonic region / GMPE

Add an entry to `REGION_GMPE_TABLE` in `config.py`:
```python
("Region name", minlat, maxlat, minlon, maxlon, "Tectonic Type",
 "openquake.hazardlib.gsim.module_path", "ClassName", caution_note_or_None)
```
Entries are checked in order; the first bounding-box match wins. `hazardlib`
includes dozens of published GMPEs for different tectonic settings — browse
`openquake.hazardlib.gsim` for options.

## Web Dashboard

`dashboard.py` is a lightweight Flask app that turns the terminal-only
monitor into a browsable web page -- it only **reads** from the same
SQLite database and shake map image folder the monitor writes to, so it
can be started, stopped, or restarted independently without affecting
detection, notifications, or computation at all.

**What it shows:**
- An interactive world map (Folium/Leaflet, OpenStreetMap tiles) with
  markers for the last 200 logged events, color-coded by magnitude
- A sortable event list below the map
- Click any marker or list row → a detail page with the matched
  region/tectonic type/GMPE, a caution note if applicable, a table of
  per-target PGA/MMI results, and all three shake map images inline

**Run it:**
```bash
conda activate openquake_conda
python dashboard.py
```
Then open `http://localhost:5001`. Auto-refreshes every 60 seconds.

**Populate it with test data** (if you haven't run the live monitor yet):
```bash
python earthquake_monitor_with_shaking.py --test-map
```
This logs a synthetic event (tagged with source `"TEST"`) to the same
database the dashboard reads from, so you can see the full page working
without waiting on a real M5+ earthquake.

**Note on requests during development**: if you edit `dashboard.py` while
it's running, you need to actually restart the process (Ctrl+C, then
`python dashboard.py` again) -- a browser refresh alone re-fetches the
same still-running old code, since `debug=False` doesn't auto-reload.

## Configuration for other scripts

The scripts below aren't part of the main combined pipeline but have their
own configurable constants near the top of each file, worth knowing about:

**`shakemap_tool.py`**
| Parameter | Default | What it controls |
|---|---|---|
| `USGS_SUMMARY_URL` | `.../summary/significant_month.geojson` | Feed used by `--list-recent` to find real event IDs with official ShakeMaps. |
| `OUTPUT_DIR` | `shakemap_output` | Where downloaded official ShakeMap images and DIY estimate plots are saved. |
| `span_deg` (inside `generate_estimate`) | `3.0` | Grid span (degrees) for the DIY illustrative shaking-estimate map. |

**`pygmm_shaking_estimate.py`**
| Parameter | Default | What it controls |
|---|---|---|
| `KNOWN_MODELS` | 4 models (ChiouYoungs2014, BSSA14, CampbellBozorgnia2014, AbrahamsonSilvaKamai2014) | Models tried when `--model all` is used. Only `ChiouYoungs2014` is confirmed working against the exact installed pyGMM API pattern (the others use the same constructor call and may or may not match your version — errors are surfaced clearly if not). |

**`openquake_shaking_estimate.py`**
| Parameter | Default | What it controls |
|---|---|---|
| Model | `BooreEtAl2014` (hardcoded) | Standalone one-off calculator; doesn't have the region-aware selection from the main monitor. Edit the import/instantiation near the top to change it. |

**`europe_stations_fetch.py`**
| Parameter | Default | What it controls |
|---|---|---|
| `DATA_CENTER` | `GFZ` | FDSN data center to query (confirmed to actually archive waveform data, unlike some others tried during this project — see caveats). |
| `NETWORK` | `GE` | FDSN network code (GEOFON's own global network). |
| `CHANNEL` | `BH?` | Wildcard for all three broadband components (Z/N/E). |
| `EUROPE_BBOX` | continental Europe | Bounding box for `--list-stations`. |
| `WINDOW_SECONDS` | `3600` | Length of each waveform fetch window. |

**`israel_live_trigger_monitor.py`**
| Parameter | Default | What it controls |
|---|---|---|
| `STATIONS` | 5 confirmed real `IS` network codes | Kept for reference; note this network's waveform data isn't actually publicly retrievable (see caveats) so this script won't return data despite valid station codes. |
| `POLL_INTERVAL_SEC` | `300` | Polling interval, if you adapt this approach to a network that does have public waveform access. |

## Other scripts in this project (exploratory / earlier stages)

**Core files** (required to run the main pipeline): `config.py`,
`earthquake_monitor_with_shaking.py`, `dashboard.py`, `Dockerfile`,
`docker-compose.yml`.

**Everything below** was built along the way and remains useful for other
purposes, though it's not part of the final combined pipeline:

- `global_earthquake_monitor.py` — the detection-only version (no
  shaking estimate), if you just want event alerts.
- `shakemap_tool.py` — fetch USGS's *official* ShakeMap for a real
  cataloged event, or run a simplified illustrative (not scientifically
  validated) DIY shaking estimate.
- `openquake_shaking_estimate.py` / `pygmm_shaking_estimate.py` —
  standalone command-line shaking calculators (hazardlib and pyGMM
  versions respectively) for one-off "what if a M__ happened here"
  queries, without the live monitoring loop.
- `europe_stations_fetch.py` — discover and fetch raw waveform data
  from European FDSN stations (e.g. GEOFON's `GE` network), for
  waveform-level exploration (plotting, STA/LTA, etc.) independent of
  the catalog-based monitor.
- `israel_live_trigger_monitor.py` / `israel_seedlink_streaming_monitor.py`
  — the original raw-station approaches for Israel specifically; kept
  for reference, though `IS` network waveform data isn't publicly
  available and SeedLink streaming isn't currently viable against
  GEOFON's server (see "Why catalogs, not raw stations" above).

## Notifications

Two independent notification channels, both optional, both trigger for
events at or above `NOTIFY_MAGNITUDE_THRESHOLD` (separate from
`SHAKING_MAGNITUDE_THRESHOLD`, so you can be notified about smaller
events than the ones that get a full shaking computation):

**Email** (via standard SMTP, Python's built-in `smtplib` -- no extra
dependency): set `NOTIFY_EMAIL=true` plus `SMTP_HOST`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO`. Works identically inside or
outside Docker, since it's just an outbound network connection. For
Gmail specifically, you need a 16-character App Password (see
"Configuration" above), not your normal account password.

**Webhook** (Slack or Discord): set `WEBHOOK_URL` to your webhook URL
and `WEBHOOK_TYPE` to `"slack"` or `"discord"`. Unset (default) disables
it. Also works fine in Docker.

Both channels are tested together via `--test-map`, which sends a real
notification for its synthetic M6.5 test event -- useful for confirming
email/webhook delivery works before waiting on a genuine earthquake.

## Event history (SQLite)

Every detected event -- and its shaking estimate, if one was computed --
is automatically logged to a local SQLite database (`HISTORY_DB_PATH`,
default `earthquake_history.db`), so history survives restarts and can be
analyzed independently of the running monitor. **The web dashboard
(`dashboard.py`) is the easiest way to browse this data** -- see "Web
Dashboard" above.

Two tables: `events` (time, location, magnitude, depth, place, which
source(s) reported it, matched region/tectonic type/GMPE, and the file
paths of all three generated shake map images) and `shaking_estimates`
(per-target distance, Vs30, PGA, MMI, linked to its parent event).

Existing databases created before the map-path columns existed are
automatically migrated (columns added) the next time `init_history_db()`
runs -- no manual action needed, old rows just show blank map paths.

Quick summary without running the full monitor:
```bash
python earthquake_monitor_with_shaking.py --history-summary
```

Or query it directly with any SQLite tool:
```bash
sqlite3 earthquake_history.db "SELECT * FROM events ORDER BY mag DESC LIMIT 5;"
```

## Running with Docker

**This is the recommended way to run this project**, especially if
sharing it with someone else. The included `Dockerfile` encapsulates the
entire working conda/GDAL/openquake/Flask setup discovered during this
project's development (including fixes for a missing system library and
a NumPy/pandas package-conflict that only showed up when mixing pip and
conda installs) -- building it once means never repeating that
debugging process again, on this machine or any other.

### What to send someone

Required files (everything the Dockerfile and docker-compose.yml reference):
```
Dockerfile
docker-compose.yml
config.py
earthquake_monitor_with_shaking.py
dashboard.py
global_earthquake_monitor.py
shakemap_tool.py
openquake_shaking_estimate.py
README.md
```
Put them all in one folder (or share a GitHub repo containing them --
cleanest option if you already have one set up).

### 1. Install Docker

- **Mac/Windows**: Docker Desktop from docker.com
- **Linux**: `sudo apt install docker.io` (Ubuntu/Debian) then
  `sudo systemctl start docker`

Verify it works before anything else:
```bash
docker run hello-world
```
Should print a message starting with "Hello from Docker!". If you get a
proxy/network error instead, check Docker Desktop's Settings → Resources
→ Proxies -- a leftover corporate-proxy configuration can block this
entirely.

### 2. Build the image

```bash
cd path/to/project/folder
docker build -t earthquake-monitor .
```
Takes a few minutes the first time (installing conda, Python 3.12,
GDAL, openquake.engine, Flask). Watch for `Successfully tagged
earthquake-monitor:latest` or check `docker images` afterward.

### 3. Verify the environment actually works

```bash
docker run --rm --entrypoint conda earthquake-monitor \
    run -n openquake_conda python -c "import openquake.hazardlib; print('hazardlib OK')"
```
Should print `hazardlib OK`. This one check caught two real bugs during
development (a missing system library, a dependency-resolver conflict),
so it's worth running explicitly rather than skipping ahead.

### 4. Set up configuration (optional but recommended)

Create a `.env` file in the same folder (see "Configuration" above for
full format rules) -- at minimum, if you want email alerts:
```bash
# .env -- add this to .gitignore, it holds your email password!
NOTIFY_EMAIL=true
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your16charapppassword
EMAIL_FROM=you@gmail.com
EMAIL_TO=you@gmail.com
```

### 5. Run the instant test map (no waiting on real events)

```bash
docker run --rm -v $(pwd)/shakemaps:/app/shakemaps \
    --env-file .env earthquake-monitor --test-map
```
Check your local `shakemaps/` folder afterward for the generated PNGs.

### 6. Run the live monitor

Two mounted volumes matter here: the shake maps folder, and the history
database **as a file** (create it empty first with `touch` if it doesn't
exist yet, or Docker will silently create a directory there instead and
the database will fail to open):
```bash
touch earthquake_history.db   # only needed once, if the file doesn't exist yet

docker run -it --rm --env-file .env \
    -v $(pwd)/shakemaps:/app/shakemaps \
    -v $(pwd)/earthquake_history.db:/app/earthquake_history.db \
    earthquake-monitor
```
Stop anytime with Ctrl+C.

### 7. Run the web dashboard (separate container, same mounted data)

```bash
docker run -it --rm -p 5001:5001 \
    -v $(pwd)/shakemaps:/app/shakemaps \
    -v $(pwd)/earthquake_history.db:/app/earthquake_history.db \
    --entrypoint conda earthquake-monitor \
    run --no-capture-output -n openquake_conda python dashboard.py
```
Then open `http://localhost:5001`. This can run at the same time as the
live monitor (step 6) in a separate terminal -- they share the same
mounted files, one writes, the other only reads.

### Easier alternative: Docker Compose (one command, both containers)

Steps 6 and 7 above run as two separate `docker run` commands in two
terminals. `docker-compose.yml` (included) starts both together with a
single command instead, using the same shared mounted files:

```bash
touch earthquake_history.db   # once, if it doesn't already exist
docker compose build
docker compose up
```

Logs from both containers interleave in one terminal (prefixed
`earthquake-monitor` / `earthquake-dashboard`). Dashboard still opens at
`http://localhost:5001`. Configuration still comes from `.env` in the
same folder -- Compose reads it automatically, no `--env-file` flag needed.

```bash
docker compose up -d       # run in the background instead
docker compose logs -f     # follow logs if started with -d
docker compose down        # stop both
```

**If you change the `Dockerfile` or add a new dependency**, force a real
rebuild rather than trusting a stale cached image (Compose reuses an
existing local image with the same tag by default, which can hide
changes):
```bash
docker compose down
docker compose build --no-cache
docker compose up
```
Verify a specific dependency actually made it into the image if
something's missing at runtime:
```bash
docker run --rm --entrypoint conda earthquake-monitor \
    run -n openquake_conda python -c "import folium, flask; print('OK')"
```

### Notes

- **macOS desktop notifications don't work inside Docker** (containers
  can't access the host's notification system) -- this project uses
  email instead specifically because it works identically inside or
  outside Docker (it's just an outbound SMTP connection). Webhook
  notifications work fine in Docker too, for the same reason.
- **Windows path syntax differs**: PowerShell uses `${PWD}` instead of
  `$(pwd)`; Command Prompt uses `%cd%`. The `docker build`/`docker run`
  commands themselves are otherwise identical across Mac/Linux/Windows --
  Docker handles the OS differences internally. `docker compose` sidesteps
  this entirely, since paths are defined once in `docker-compose.yml`.
- **Editing `.env` never requires a rebuild** -- only editing the actual
  Python files (or the `Dockerfile`'s dependency list) does. This is the
  whole point of the environment-variable configuration system:
  reconfigure freely without touching the image.

## Caveats

- **Not prediction.** Nothing here forecasts an earthquake before it
  happens.
- **Shaking estimates are approximate**, even with region-aware model
  selection. Any GMPE has genuine uncertainty (typically a factor of ~2x
  in either direction is considered normal), and none account for the
  detailed geology of the specific path between source and target — only
  straight-line distance, magnitude, and site-level Vs30. Treat outputs as
  informative estimates, not precise forecasts of actual damage.
- **Region matching is a rough approximation**, not precise tectonic-plate
  geometry. Some real tectonic boundaries don't align neatly with simple
  bounding boxes. Always check the printed region/caution note for each
  event rather than assuming the "obvious" model applies.
- **Volcanic and other non-standard settings** (Hawaii being the clearest
  example) aren't well represented by any standard tectonic GMPE — results
  there are explicitly flagged as a rough proxy, not a real regional model.
- **Coverage gaps.** Very small or highly localized events may not appear
  in EMSC or USGS at all if they fall below each agency's publication
  threshold for that region.
- **Basemap tiles require live internet access** at runtime and depend on
  third-party free tile services, which have changed access policies more
  than once during this project's development (OpenStreetMap blocking,
  CartoDB's new API key requirement). The coastline-outline fallback has
  proven the most reliable option if tile providers become unavailable.
