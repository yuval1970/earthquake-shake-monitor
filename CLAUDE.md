# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Real-time global earthquake detection combined with region-aware ground-motion
(shaking intensity) estimation and shake-map visualization. Detects events
from EMSC (WebSocket push) and USGS (polled GeoJSON), deduplicates them,
classifies each event's tectonic region, picks a matching peer-reviewed GMPE
(Ground Motion Prediction Equation) from `openquake.hazardlib`, and computes
expected PGA/MMI at configured target locations. See `README.md` for the full
architecture diagram, the region→GMPE table, all configurable parameters, and
usage examples — it is kept detailed and up to date; read it before making
non-trivial changes rather than re-deriving behavior from code alone.

This is a flat collection of standalone scripts, not an installable package —
there is no `setup.py`/`pyproject.toml`, no test suite, and no build/lint
tooling configured in this repo.

## Environment setup

`openquake.hazardlib` requires **Python ≤3.12**. Scripts that depend on it
must run inside the dedicated conda env:

```bash
conda activate openquake_conda
```

If the env doesn't exist yet:
```bash
brew install miniconda
conda init zsh
conda create -n openquake_conda python=3.12
conda activate openquake_conda
conda install -c conda-forge openquake.engine
pip install websocket-client requests contextily geodatasets
```

Scripts using `obspy` instead of `hazardlib` (`europe_stations_fetch.py`,
`israel_live_trigger_monitor.py`, `israel_seedlink_streaming_monitor.py`) do
not need `openquake_conda`.

## Running the scripts

Main combined pipeline (live detection + shaking estimate + shake maps):
```bash
conda activate openquake_conda
python earthquake_monitor_with_shaking.py
```

Instant synthetic test event, no waiting for a real earthquake (useful when
changing map generation or GMPE-selection logic):
```bash
python earthquake_monitor_with_shaking.py --test-map                          # M6.5 Hilo, Hawaii preset (volcanic caution path)
python earthquake_monitor_with_shaking.py --test-map --test-preset dead_sea   # Middle East / Akkar routing
python earthquake_monitor_with_shaking.py --test-map --test-lat 31.6 --test-lon 35.4 --test-mag 7.0 --test-depth 15
```

Detection-only, no shaking estimate:
```bash
python global_earthquake_monitor.py
```

One-off shaking calculator, no live monitoring loop:
```bash
python openquake_shaking_estimate.py --mag 6.5 --depth 10 --distances 10 30 50 100 --vs30 400
```

Raw European FDSN waveform fetch (independent of the monitor):
```bash
python europe_stations_fetch.py --list-stations
python europe_stations_fetch.py --fetch STATIONCODE --hours-ago 1
```

There is no automated test suite — validate changes by running
`--test-map` (fast, synthetic) or the live monitor and inspecting console
output plus the generated PNGs in `shakemaps/`.

## Architecture (`earthquake_monitor_with_shaking.py`)

This is the file most work will touch. Pipeline, in order:

1. **Detection** — two independent, concurrent sources feed the same event
   handling path: `run_emsc_listener()` (WebSocket, push) on one thread and
   `run_usgs_poller()` (polls `USGS_POLL_INTERVAL_SEC`) on another, both
   started from `main()`. Each calls `report_event()`.
2. **Dedup** — `find_matching_event()` checks a shared in-memory list of
   recently seen events against `DEDUP_TIME_SEC` / `DEDUP_DIST_DEG` /
   `DEDUP_MAG_DELTA` so the same physical event reported by both EMSC and
   USGS produces one alert, not two.
3. **Region classification + GMPE selection** — `select_gmpe_for_location()`
   walks `_REGION_GMPE_TABLE` (module-level list near the top of the file,
   first-match-wins bounding boxes) and lazily imports/instantiates the
   matched `hazardlib` GMPE class via `_get_gmpe_instance()`, which caches
   instances in `_gmpe_instance_cache`. **`hazardlib` is imported once at
   module load time on the main thread** — do not move GMPE imports inside a
   function callable from both the EMSC and USGS threads; a prior lazy-import
   version hit an intermittent metaclass registration race there (see the
   comment block above `_gmpe_instance_cache`).
4. **Shaking computation** — for events ≥ `SHAKING_MAGNITUDE_THRESHOLD`,
   `compute_shaking_at_targets()` computes distance (`haversine_km`) and
   Vs30 (`lookup_vs30()`, live USGS Global Vs30 Mosaic query, cached per
   location) for each `TARGET_LOCATIONS` entry, runs the selected GMPE, and
   converts PGA to a human MMI description via `pga_to_mmi_description()`.
   Targets beyond `MAX_VALID_DISTANCE_KM` are skipped rather than shown with
   misleading values.
5. **Shake maps** — `generate_all_shakemaps()` calls
   `generate_shakemap_plot()` three times (regional / street / building-scale
   zoom spans) using `contextily` basemap tiles with an automatic
   `geopandas`/`geodatasets` coastline-outline fallback
   (`_load_world_boundaries()`) if tile fetching fails. Output goes to
   `SHAKEMAP_OUTPUT_DIR`.

All tunable parameters (`CONFIGURATION` section near the top of the file —
feed URLs, dedup thresholds, `TARGET_LOCATIONS`, map spans/providers, etc.)
are documented in `README.md`'s "Configurable parameters" tables; check there
before assuming a value's purpose.

`global_earthquake_monitor.py` mirrors steps 1–2 of this pipeline
(detection + dedup only, no shaking/maps) and is structurally the same code
with the GMPE/shaking/map layers removed — useful as the simpler reference
when debugging detection/dedup logic in isolation.

## Other scripts

- `openquake_shaking_estimate.py` — standalone hazardlib shaking calculator
  (hardcoded `BooreEtAl2014`, not region-aware). No live monitoring.
- `europe_stations_fetch.py` — fetches raw waveform data from GEOFON's `GE`
  FDSN network via `obspy`. Independent of the catalog-based monitor.
- `israel_live_trigger_monitor.py` / `israel_seedlink_streaming_monitor.py`
  — earlier raw-station-based (STA/LTA) approaches for Israel's `IS`
  network, kept for reference only. **Not functional**: `IS` network
  waveform data isn't publicly retrievable via FDSN/EIDA, and GEOFON's
  SeedLink server uses a newer "HMB SeedLink" protocol that doesn't match
  ObsPy's classic SeedLink client. This is why the project moved to
  catalog aggregators (EMSC/USGS) instead of raw station triggering — see
  README's "Why catalogs, not raw stations".
- `opsTest.py` — exploratory Jupyter-style script (has an inline `%matplotlib`
  magic, not a plain runnable `.py`).
- `*.ipynb` notebooks — exploratory analysis, not part of the pipeline.

Note: `README.md` also documents `shakemap_tool.py` and
`pygmm_shaking_estimate.py` (official USGS ShakeMap fetch, and a pyGMM-based
calculator) — these files are **not present** in the current working tree;
treat those README sections as aspirational/stale rather than runnable until
the files reappear.