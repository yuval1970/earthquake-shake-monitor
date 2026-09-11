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

This project requires `openquake.hazardlib`, which needs **Python ≤3.12**
(not compatible with newer versions). The recommended setup uses a
dedicated conda environment:

```bash
brew install miniconda        # if not already installed
conda init zsh                # or bash; restart terminal after
conda create -n openquake_conda python=3.12
conda activate openquake_conda
conda install -c conda-forge openquake.engine
pip install websocket-client requests contextily geodatasets
```

Run with:
```bash
conda activate openquake_conda
python earthquake_monitor_with_shaking.py
```

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

All parameters live in the `CONFIGURATION` section near the top of
`earthquake_monitor_with_shaking.py`.

### Detection

| Parameter | Default | What it controls |
|---|---|---|
| `EMSC_WS_URL` | `wss://www.seismicportal.eu/standing_order/websocket` | EMSC's real-time event WebSocket endpoint. |
| `USGS_FEED_URL` | `.../summary/all_hour.geojson` | Which USGS feed to poll. Swap for `all_day.geojson` etc. for a longer lookback window per poll. |
| `USGS_POLL_INTERVAL_SEC` | `60` | How often (seconds) to re-poll USGS. Lower = fresher data, more requests. |
| `MIN_MAGNITUDE` | `0.0` | Minimum magnitude to track/report at all. `0.0` = show everything either source publishes. Raise (e.g. `2.5`) to cut noise. |
| `BBOX` | `None` (global) | Restrict tracked events to a region: `(minlat, maxlat, minlon, maxlon)`. `None` = worldwide. |
| `DEDUP_TIME_SEC` | `90` | Max time difference (seconds) for two reports to be considered the same event. |
| `DEDUP_DIST_DEG` | `1.0` | Max lat/lon difference (degrees, ~100km) for two reports to be considered the same event. |
| `DEDUP_MAG_DELTA` | `1.0` | Max magnitude difference for two reports to be considered the same event (agencies often disagree slightly). |

### Shaking estimation

| Parameter | Default | What it controls |
|---|---|---|
| `SHAKING_MAGNITUDE_THRESHOLD` | `5.0` | Minimum magnitude that triggers a shaking computation. Below this, events are still reported but no GMPE/map is generated. |
| `TARGET_LOCATIONS` | 6 example cities | List of `(name, lat, lon)` tuples to estimate shaking at for qualifying events. **Edit this to your own locations of interest.** |
| `MAX_VALID_DISTANCE_KM` | `300` | Most GMPEs are only empirically calibrated up to a few hundred km from the source. Targets farther than this are skipped rather than shown as misleading near-zero values. |
| `_REGION_GMPE_TABLE` | see table above | The region → tectonic type → GMPE lookup. Add, remove, or adjust bounding boxes here to change model selection for any area. |
| `_DEFAULT_REGION` | Akkar (generic fallback) | Model used when no region in the table matches. |

### Maps and visualization

| Parameter | Default | What it controls |
|---|---|---|
| `SHAKEMAP_OUTPUT_DIR` | `shakemaps` | Folder where generated shake map PNGs are saved. |
| `SHAKEMAP_VMAX` | `None` (auto) | Set a fixed number (%g) to force every map onto the same color scale, making different events visually comparable. `None` = each map auto-scales to its own data (best per-event detail, not comparable across maps). |
| `SHAKEMAP_LOG_SCALE` | `False` | Log color scale shows detail across huge magnitude ranges in one map, but visually compresses severity differences — read the tradeoff note in the code before enabling. |
| `SHAKEMAP_USE_CONTEXTILY` | `True` | Use real web-map basemap tiles. Falls back to bare coastline outlines if tile fetching fails for any reason (network issue, provider blocking, etc). |
| `SHAKEMAP_CONTEXTILY_PROVIDER` | `Esri.WorldStreetMap` | Which tile provider/style to use. OpenStreetMap and CartoDB were both tried and found unreliable for automated use (usage-policy blocking, new API key requirement respectively) — Esri's public tiles work without a key. |
| `STREET_MAP_SPAN_DEG` | `0.5` (~55km) | Span of the second (street-level) map output, centered on the closest in-range target. |
| `ZOOM_STREET_MAP_SPAN_DEG` | `0.03` (~3.3km) | Span of the third (building/street close-up) map output. |

### Vs30 lookup (site soil conditions)

`lookup_vs30(lat, lon)` queries USGS's real **Global Vs30 Mosaic** ArcGIS
service live for each target location and caches results per location
(so repeated events don't re-query the same city). No parameter to tune
here beyond the target list itself — this always reflects real data, not
an assumption.

### Adding a new tectonic region / GMPE

Add an entry to `_REGION_GMPE_TABLE` near the top of the file:
```python
("Region name", minlat, maxlat, minlon, maxlon, "Tectonic Type",
 "openquake.hazardlib.gsim.module_path", "ClassName", caution_note_or_None)
```
Entries are checked in order; the first bounding-box match wins. `hazardlib`
includes dozens of published GMPEs for different tectonic settings — browse
`openquake.hazardlib.gsim` for options.

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

These were built along the way and remain useful for other purposes,
though they're not part of the final combined pipeline:

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
