#!/usr/bin/env python3
"""
earthquake_monitor_with_shaking.py

Combines two previously built and tested pieces:

  1. Real-time global earthquake detection (EMSC WebSocket + USGS
     polling, deduplicated) -- from global_earthquake_monitor.py

  2. Scientifically real ground-motion estimation using OpenQuake's
     hazardlib BSSA14 GMPE -- from openquake_shaking_estimate.py
     (confirmed working on your machine, in the openquake_conda
     environment)

WHAT THIS DOES
--------------
Watches for new earthquakes in real time. Whenever a detected event's
magnitude meets or exceeds SHAKING_MAGNITUDE_THRESHOLD, it automatically
runs BSSA14 to estimate expected shaking (PGA in %g) at each configured
TARGET_LOCATION, based on the real distance from the actual epicenter to
that location.

IMPORTANT: MUST BE RUN INSIDE THE openquake_conda ENVIRONMENT
------------------------------------------------------------------
    conda activate openquake_conda
    python earthquake_monitor_with_shaking.py

This script needs openquake.hazardlib, which is only installed in that
environment (confirmed working there, not in your other environments).

REQUIREMENTS (already satisfied inside openquake_conda)
------------------------------------------------------------
    websocket-client, requests, numpy, openquake.hazardlib

USAGE
-----
    python earthquake_monitor_with_shaking.py

Edit SHAKING_MAGNITUDE_THRESHOLD and TARGET_LOCATIONS below to configure.
"""

import json
import sqlite3
import smtplib
from email.mime.text import MIMEText
import math
import time
import threading
import argparse
from datetime import datetime, timezone

import requests
import websocket
import numpy as np
import matplotlib
matplotlib.use("Agg")  # non-interactive backend -- safe to call from any
                       # thread (EMSC's websocket thread or the main
                       # USGS-polling thread), just saves PNG files
                       # rather than trying to pop up GUI windows
import matplotlib.pyplot as plt
import matplotlib.colors
import matplotlib.ticker
import os

# All configurable parameters live in config.py -- import early, before
# any module-load-time code below (e.g. the hazardlib pre-loading block)
# needs them.
from config import *

# Import hazardlib ONCE at module load time, on the main thread, before any
# background threads start. Importing it lazily inside a function that can
# be called from multiple threads (EMSC's websocket thread vs. the main
# USGS-polling thread) risks a race during hazardlib's metaclass-based GSIM
# registration, which is the suspected cause of an intermittent
# "'BooreEtAl2014' object has no attribute 'get_mean_and_stddevs'" error
# seen when this was imported lazily per-call instead.
#
# REGION-AWARE GMPE SELECTION
# -----------------------------
# A single fixed GMPE (e.g. AkkarEtAlRjb2014, calibrated on European/Middle
# Eastern recordings) will still compute *a* number for an earthquake
# anywhere on Earth -- it has no built-in awareness of geography -- but the
# result silently degrades in accuracy the further the event is from the
# tectonic setting the model was actually built from. A Hawaii volcanic
# earthquake fed through a Middle East shallow-crustal model, for example,
# produces a plausible-looking but genuinely less trustworthy estimate.
#
# This section defines a rough, honest regionalization: a list of
# (name, bounding box, tectonic type, GMPE to use) entries, checked in
# order for the first match containing the event's location. Bounding
# boxes are deliberately simple rectangles, not precise tectonic-plate
# geometry -- good enough to pick a *category* of model, not a
# research-grade regionalization.
_gmpe_instance_cache = {}


def _get_gmpe_instance(module_path, class_name):
    """Import and instantiate a GMPE class once, caching the instance.
    Returns None (with a printed diagnostic) if the class can't be
    loaded, so a bad/renamed class name degrades gracefully instead of
    crashing the whole selection system."""
    cache_key = f"{module_path}.{class_name}"
    if cache_key in _gmpe_instance_cache:
        return _gmpe_instance_cache[cache_key]
    try:
        module = __import__(module_path, fromlist=[class_name])
        gmpe_class = getattr(module, class_name)
        instance = gmpe_class()
        _gmpe_instance_cache[cache_key] = instance
        return instance
    except Exception as e:
        print(f"  [gmpe] Failed to load {class_name} from {module_path}: "
              f"{type(e).__name__}: {e}")
        _gmpe_instance_cache[cache_key] = None
        return None


# REGION_GMPE_TABLE and DEFAULT_REGION now live in config.py (imported below)


def select_gmpe_for_location(lat, lon):
    """
    Return (region_name, tectonic_type, gmpe_instance, gmpe_name,
    caution_note_or_None) for the given location, based on
    REGION_GMPE_TABLE (from config.py). Falls back to DEFAULT_REGION
    if nothing matches.
    """
    for (name, minlat, maxlat, minlon, maxlon, tectonic_type,
        module_path, class_name, note) in REGION_GMPE_TABLE:
        if minlat <= lat <= maxlat and minlon <= lon <= maxlon:
            instance = _get_gmpe_instance(module_path, class_name)
            if instance is not None:
                return name, tectonic_type, instance, class_name, note

    # Fallback: default region, or a matched region whose GMPE class
    # failed to load
    name, tectonic_type, module_path, class_name, note = DEFAULT_REGION
    instance = _get_gmpe_instance(module_path, class_name)
    return name, tectonic_type, instance, class_name, note


try:
    from openquake.hazardlib.imt import PGA
    from openquake.hazardlib.const import StdDev
    from openquake.hazardlib.contexts import RuptureContext
    # Pre-load the default/fallback GMPE at startup (main thread) so the
    # threading-race issue from earlier doesn't resurface; other regional
    # GMPEs get loaded+cached lazily on first use via _get_gmpe_instance,
    # also always from the main thread (event detection loop), not from
    # the EMSC websocket thread.
    _DEFAULT_GMPE_INSTANCE = _get_gmpe_instance(
        DEFAULT_REGION[2], DEFAULT_REGION[3])
    _HAZARDLIB_OK = _DEFAULT_GMPE_INSTANCE is not None
except ImportError:
    _HAZARDLIB_OK = False
    _GMPE_NAME = "unknown"

# --------------------------------------------------------------------------
# CONFIGURATION -- all values live in config.py (imported at the top of
# this file). Edit config.py, not this file, to change any parameter.
# --------------------------------------------------------------------------


# Cache so we don't re-query the same location's Vs30 on every event
_vs30_cache = {}

USGS_VS30_SERVICE_URL = "https://earthquake.usgs.gov/arcgis/rest/services/eq/vs30_mosaic/MapServer/identify"


def lookup_vs30(lat, lon):
    """
    Query USGS's real Global Vs30 Mosaic service (Heath et al. 2020,
    topographic-slope-based, 30 arc-second resolution) for the actual
    Vs30 at a specific point, instead of using a flat placeholder value.

    Returns Vs30 in m/s, or DEFAULT_VS30 with a warning if the lookup
    fails for any reason.

    NOTE: this specific query has not been test-executed against a live
    connection in this environment. The ArcGIS "identify" operation
    pattern used here is standard, but the exact JSON field name holding
    the pixel value can vary between services (commonly "Pixel Value"
    or "Stretched_Value" for raster layers). This function tries a few
    likely field names and falls back to printing the raw response if
    none match, so it can be fixed against real output rather than
    guessed further.
    """
    cache_key = (round(lat, 3), round(lon, 3))
    if cache_key in _vs30_cache:
        return _vs30_cache[cache_key]

    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "sr": "4326",
        "tolerance": "2",
        "mapExtent": f"{lon-1},{lat-1},{lon+1},{lat+1}",
        "imageDisplay": "400,400,96",
        "returnGeometry": "false",
        "f": "json",
    }

    try:
        resp = requests.get(USGS_VS30_SERVICE_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if not results:
            print(f"      [Vs30] no results for ({lat}, {lon}), "
                  f"using default {DEFAULT_VS30}")
            _vs30_cache[cache_key] = DEFAULT_VS30
            return DEFAULT_VS30

        attrs = results[0].get("attributes", {})
        # Confirmed real field name from live response: 'Classify.Pixel Value'
        # Kept other likely names as fallback in case the service response
        # shape varies by query/version.
        for key in ("Classify.Pixel Value", "Pixel Value", "Stretched_Value", "Value", "value"):
            if key in attrs:
                try:
                    vs30 = float(attrs[key])
                    _vs30_cache[cache_key] = vs30
                    return vs30
                except (TypeError, ValueError):
                    continue

        print(f"      [Vs30] unexpected response shape for ({lat}, {lon}): "
              f"{attrs} -- using default {DEFAULT_VS30}. Please share this "
              f"output so the correct field name can be fixed.")
        _vs30_cache[cache_key] = DEFAULT_VS30
        return DEFAULT_VS30

    except Exception as e:
        print(f"      [Vs30] lookup failed for ({lat}, {lon}): "
              f"{type(e).__name__}: {e} -- using default {DEFAULT_VS30}")
        _vs30_cache[cache_key] = DEFAULT_VS30
        return DEFAULT_VS30

# --------------------------------------------------------------------------

# ============================================================================
# Notifications (email + optional webhook)
# ============================================================================

def notify_email(subject, body):
    """Send an email notification via standard SMTP. Works with Gmail,
    Outlook, or any SMTP provider -- no extra dependency, uses Python's
    built-in smtplib. Does nothing if NOTIFY_EMAIL is False or the
    required settings aren't filled in."""
    if not NOTIFY_EMAIL:
        return
    if not (SMTP_USERNAME and SMTP_PASSWORD and EMAIL_FROM and EMAIL_TO):
        print("  [notify] NOTIFY_EMAIL is True but SMTP settings aren't "
              "fully filled in -- skipping email. Check SMTP_USERNAME, "
              "SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO in the config section.")
        return

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    except Exception as e:
        print(f"  [notify] Email notification failed: {type(e).__name__}: {e}")
        if "gmail" in SMTP_HOST.lower() and "Username and Password not accepted" in str(e):
            print("  [notify] Hint: Gmail requires an App Password, not "
                  "your normal account password -- see the comment above "
                  "SMTP_HOST in the config section.")


def notify_webhook(message):
    """Send a message to a Slack or Discord webhook, if WEBHOOK_URL is
    configured. Does nothing if WEBHOOK_URL is None."""
    if not WEBHOOK_URL:
        return
    try:
        if WEBHOOK_TYPE == "discord":
            payload = {"content": message}
        else:  # default: slack
            payload = {"text": message}
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [notify] Webhook failed: {type(e).__name__}: {e}")


def send_event_notifications(event):
    """Send email + webhook notifications for a newly detected event,
    if it meets NOTIFY_MAGNITUDE_THRESHOLD."""
    mag = event.get("mag")
    if mag is None or mag < NOTIFY_MAGNITUDE_THRESHOLD:
        return

    dt = datetime.fromtimestamp(event["time"], tz=timezone.utc).isoformat()
    subject = f"Earthquake Alert: M{mag} - {event['place']}"
    body = (f"Magnitude: M{mag}\n"
           f"Location: {event['place']}\n"
           f"Coordinates: {event['lat']:.4f}, {event['lon']:.4f}\n"
           f"Depth: {event.get('depth')} km\n"
           f"Time (UTC): {dt}\n\n"
           f"This is an automated alert from your earthquake monitor. "
           f"This reports an earthquake that has already occurred -- "
           f"it does not predict earthquakes before they happen.")
    notify_email(subject, body)

    webhook_message = f"🌍 *M{mag}* — {event['place']}"
    notify_webhook(webhook_message)


# ============================================================================
# Event history (SQLite)
# ============================================================================

def init_history_db():
    """Create the event history database/tables if they don't already
    exist. Safe to call every startup -- CREATE TABLE IF NOT EXISTS.
    Also safely migrates existing databases (created before map path
    columns existed) by adding those columns if missing."""
    conn = sqlite3.connect(HISTORY_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time REAL,
            lat REAL,
            lon REAL,
            mag REAL,
            depth REAL,
            place TEXT,
            sources TEXT,
            region_name TEXT,
            tectonic_type TEXT,
            gmpe_name TEXT,
            caution_note TEXT,
            detected_at REAL,
            regional_map_path TEXT,
            street_map_path TEXT,
            zoom_street_map_path TEXT
        )
    """)
    # Migration for databases created before these columns existed --
    # ALTER TABLE fails harmlessly if the column is already present.
    for col in ("regional_map_path", "street_map_path", "zoom_street_map_path"):
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.execute("""
        CREATE TABLE IF NOT EXISTS shaking_estimates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER,
            target_name TEXT,
            distance_km REAL,
            vs30 REAL,
            pga_percent_g REAL,
            mmi TEXT,
            mmi_description TEXT,
            FOREIGN KEY(event_id) REFERENCES events(id)
        )
    """)
    conn.commit()
    conn.close()


def log_event_to_db(event, sources):
    """Log a newly detected event to the history database. Returns the
    new row's id (for linking shaking estimates), or None on failure."""
    try:
        conn = sqlite3.connect(HISTORY_DB_PATH)
        cursor = conn.execute("""
            INSERT INTO events (event_time, lat, lon, mag, depth, place,
                               sources, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (event["time"], event["lat"], event["lon"], event["mag"],
             event.get("depth"), event["place"], ",".join(sorted(sources)),
             time.time()))
        event_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return event_id
    except Exception as e:
        print(f"  [history] Failed to log event: {type(e).__name__}: {e}")
        return None


def log_shaking_estimate_to_db(event_id, region_name, tectonic_type,
                              gmpe_name, caution_note, results,
                              map_paths=None):
    """Update an event's region/GMPE info and log per-target shaking
    results to the history database. map_paths (optional) is the dict
    returned by generate_all_shakemaps(), e.g.
    {"regional": path, "street": path, "zoom_street": path}."""
    if event_id is None:
        return
    try:
        conn = sqlite3.connect(HISTORY_DB_PATH)
        conn.execute("""
            UPDATE events SET region_name=?, tectonic_type=?, gmpe_name=?,
                             caution_note=?
            WHERE id=?
        """, (region_name, tectonic_type, gmpe_name, caution_note, event_id))

        if map_paths:
            conn.execute("""
                UPDATE events SET regional_map_path=?, street_map_path=?,
                                 zoom_street_map_path=?
                WHERE id=?
            """, (map_paths.get("regional"), map_paths.get("street"),
                 map_paths.get("zoom_street"), event_id))

        for name, dist, vs30, pga in results:
            mmi, desc = pga_to_mmi_description(pga)
            conn.execute("""
                INSERT INTO shaking_estimates
                    (event_id, target_name, distance_km, vs30,
                    pga_percent_g, mmi, mmi_description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (event_id, name, dist, vs30, pga, mmi, desc))

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [history] Failed to log shaking estimate: {type(e).__name__}: {e}")


_lock = threading.Lock()
_known_events = []  # list of dicts: {time, lat, lon, mag, depth, place, sources:set()}


def in_bbox(lat, lon):
    if BBOX is None:
        return True
    minlat, maxlat, minlon, maxlon = BBOX
    return minlat <= lat <= maxlat and minlon <= lon <= maxlon


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def pga_to_mmi_description(pga_percent_g):
    """
    Convert PGA (%g) to an approximate Modified Mercalli Intensity level
    and a short human-readable damage description. Thresholds are
    commonly-cited approximate correspondences -- real PGA-to-MMI
    relationships are regionally calibrated and have real scatter, so
    treat this as illustrative, not precise.
    """
    levels = [
        (0.05, "I",      "Not felt"),
        (0.3,  "II-III", "Weak -- felt by some indoors"),
        (2.8,  "IV-V",   "Light -- felt widely, dishes/windows rattle"),
        (6.2,  "VI",     "Moderate -- felt by all, slight damage"),
        (12,   "VII",    "Strong -- damage to poorly built structures"),
        (22,   "VIII",   "Very strong -- moderate damage to ordinary buildings"),
        (40,   "IX",     "Violent -- considerable damage, buildings shifted"),
        (75,   "X",      "Extreme -- most masonry/frame structures destroyed"),
        (139,  "XI-XII", "Catastrophic -- near-total destruction"),
    ]
    mmi, desc = "I", "Not felt"
    for pga_thresh, mmi_val, description in levels:
        if pga_percent_g >= pga_thresh:
            mmi, desc = mmi_val, description
    return mmi, desc


# ============================================================================
# Ground motion computation (confirmed-working BSSA14 pattern from
# openquake_shaking_estimate.py)
# ============================================================================

def compute_shaking_at_targets(magnitude, depth_km, event_lat, event_lon):
    """Run the region-appropriate GMPE (selected via select_gmpe_for_location
    based on the event's actual location) for the real distance from the
    event to each configured target location that falls within
    MAX_VALID_DISTANCE_KM, using each location's REAL Vs30 (looked up from
    USGS's Global Vs30 Mosaic). Targets beyond MAX_VALID_DISTANCE_KM are
    returned separately, marked as out-of-range, since GMPEs aren't
    calibrated for such distances and a computed value there would be
    misleading rather than useful.

    Returns (in_range_results, out_of_range_results, region_info) where
    region_info = (region_name, tectonic_type, gmpe_name, caution_note)."""
    if not _HAZARDLIB_OK:
        raise RuntimeError("openquake.hazardlib failed to import at "
                          "startup -- check you're in the "
                          "openquake_conda environment")

    region_name, tectonic_type, gmpe, gmpe_name, caution_note = \
        select_gmpe_for_location(event_lat, event_lon)
    region_info = (region_name, tectonic_type, gmpe_name, caution_note)

    if gmpe is None:
        raise RuntimeError(
            f"No usable GMPE available for region '{region_name}' "
            f"(class '{gmpe_name}' failed to load -- see earlier "
            f"[gmpe] diagnostic message)")

    all_distances = [(name, tlat, tlon,
                      haversine_km(event_lat, event_lon, tlat, tlon))
                     for name, tlat, tlon in TARGET_LOCATIONS]

    in_range = [(name, tlat, tlon, dist) for name, tlat, tlon, dist in all_distances
               if dist <= MAX_VALID_DISTANCE_KM]
    out_of_range = [(name, dist) for name, tlat, tlon, dist in all_distances
                    if dist > MAX_VALID_DISTANCE_KM]

    if not in_range:
        return [], out_of_range, region_info

    distances = [dist for _, _, _, dist in in_range]
    vs30_values = [lookup_vs30(tlat, tlon) for _, tlat, tlon, _ in in_range]
    n = len(distances)

    ctx = RuptureContext()
    ctx.mag = magnitude
    ctx.rake = 0.0
    ctx.dip = 90.0
    ctx.ztor = depth_km
    ctx.rjb = np.array(distances, dtype=float)
    ctx.rrup = np.array(distances, dtype=float)
    ctx.vs30 = np.array(vs30_values, dtype=float)
    ctx.vs30measured = np.full(n, True)
    ctx.z1pt0 = np.full(n, -999.0)

    if not hasattr(gmpe, "compute"):
        raise RuntimeError(
            f"GMPE {gmpe_name} has no compute() method. Available "
            f"attrs: {[a for a in dir(gmpe) if not a.startswith('_')]}"
        )

    imts = [PGA()]
    mean = np.zeros((1, n))
    sig = np.zeros((1, n))
    tau = np.zeros((1, n))
    phi = np.zeros((1, n))
    gmpe.compute(ctx, imts, mean, sig, tau, phi)
    pga_percent_g = np.exp(mean[0]) * 100

    results = []
    for (name, _, _, dist), vs30, pga in zip(in_range, vs30_values, pga_percent_g):
        results.append((name, dist, vs30, float(pga)))
    return results, out_of_range, region_info


# SHAKEMAP_OUTPUT_DIR now comes from config.py (imported earlier)

_world_boundaries_cache = None
_world_boundaries_load_attempted = False


def _load_world_boundaries():
    """
    Try a couple of approaches to get world coastline/country boundary
    geometries for overlaying on the shake map, using geopandas (already
    installed as a dependency of openquake.engine in this environment).
    Returns a GeoDataFrame, or None if nothing worked -- caller falls
    back to a plain lat/lon grid without coastlines rather than failing.
    Result is cached after the first attempt so we don't retry/reload
    on every single map generated.
    """
    global _world_boundaries_cache, _world_boundaries_load_attempted

    if _world_boundaries_load_attempted:
        return _world_boundaries_cache
    _world_boundaries_load_attempted = True

    try:
        import geopandas as gpd
    except ImportError:
        print("  [basemap] geopandas not installed -- skipping coastlines")
        return None

    # Attempt 1: legacy bundled dataset (removed in geopandas >=0.14, but
    # worth trying since bundled/conda versions vary)
    try:
        path = gpd.datasets.get_path("naturalearth_lowres")
        gdf = gpd.read_file(path)
        print("  [basemap] Loaded world boundaries via geopandas legacy dataset")
        return gdf
    except Exception as e1:
        attempt1_error = e1

    # Attempt 2: geodatasets package (modern replacement, may not be installed)
    try:
        import geodatasets
        path = geodatasets.get_path("naturalearth.land")
        gdf = __import__("geopandas").read_file(path)
        print("  [basemap] Loaded world boundaries via geodatasets package")
        return gdf
    except Exception as e2:
        print(f"  [basemap] Could not load world boundaries.")
        print(f"    Legacy geopandas dataset failed: {type(attempt1_error).__name__}: {attempt1_error}")
        print(f"    geodatasets package failed: {type(e2).__name__}: {e2}")
        print(f"    Coastlines will be skipped (map still generated, just "
              f"without borders). To enable them, try: pip install geodatasets")
        return None


def generate_shakemap_plot(event, in_range_results, out_of_range_results,
                          center_lat=None, center_lon=None, span_deg=None,
                          provider_name=None, filename_suffix="",
                          map_label="Regional"):
    """
    Generate a visual shake map: a contour plot of estimated PGA around a
    center point (using a generic Vs30 for the background field, since
    querying real per-pixel Vs30 for every grid point would be very slow),
    with the actual configured target locations marked and labeled with
    their real, Vs30-adjusted PGA values from compute_shaking_at_targets().

    By default, centers on the epicenter with a span sized to
    MAX_VALID_DISTANCE_KM (the original regional-overview behavior).
    Pass center_lat/center_lon/span_deg to instead generate a tighter
    zoomed view (e.g. centered on a specific target city).

    Saves a PNG file and returns its path. Does not display an
    interactive window, since this can be called from a background
    thread during live monitoring -- open the saved file manually.
    """
    if not _HAZARDLIB_OK:
        return None

    os.makedirs(SHAKEMAP_OUTPUT_DIR, exist_ok=True)

    event_lat, event_lon = event["lat"], event["lon"]
    magnitude = event["mag"]
    depth_km = event.get("depth", 10.0)

    lat = center_lat if center_lat is not None else event_lat
    lon = center_lon if center_lon is not None else event_lon
    if span_deg is None:
        span_deg = max(1.0, MAX_VALID_DISTANCE_KM / 111.0 * 1.3)
    provider_name = provider_name or SHAKEMAP_CONTEXTILY_PROVIDER

    grid_n = 60
    lats = np.linspace(lat - span_deg, lat + span_deg, grid_n)
    lons = np.linspace(lon - span_deg, lon + span_deg, grid_n)

    generic_vs30 = 400.0  # flat background assumption -- see docstring
    pga_grid = np.zeros((grid_n, grid_n))

    region_name, tectonic_type, gmpe, gmpe_name, caution_note = \
        select_gmpe_for_location(event_lat, event_lon)
    if gmpe is None:
        return None  # region_info already printed a diagnostic upstream

    for i, la in enumerate(lats):
        row_dists = [haversine_km(event_lat, event_lon, la, lo) for lo in lons]
        ctx = RuptureContext()
        ctx.mag = magnitude
        ctx.rake = 0.0
        ctx.dip = 90.0
        ctx.ztor = depth_km
        ctx.rjb = np.array(row_dists, dtype=float)
        ctx.rrup = np.array(row_dists, dtype=float)
        ctx.vs30 = np.full(grid_n, generic_vs30, dtype=float)
        ctx.vs30measured = np.full(grid_n, True)
        ctx.z1pt0 = np.full(grid_n, -999.0)

        imts = [PGA()]
        mean = np.zeros((1, grid_n))
        sig = np.zeros((1, grid_n))
        tau = np.zeros((1, grid_n))
        phi = np.zeros((1, grid_n))
        gmpe.compute(ctx, imts, mean, sig, tau, phi)
        pga_grid[i, :] = np.exp(mean[0]) * 100

    fig, ax = plt.subplots(figsize=(9, 7))

    # Draw the basemap FIRST (contextily needs known axis extent to fetch
    # matching tiles), then layer semi-transparent contours on top so the
    # basemap underneath stays visible.
    ax.set_xlim(lons.min(), lons.max())
    ax.set_ylim(lats.min(), lats.max())

    used_contextily = False
    if SHAKEMAP_USE_CONTEXTILY:
        try:
            import contextily as ctx_basemap
            provider = ctx_basemap.providers
            for part in provider_name.split("."):
                provider = provider[part]

            # Tile providers have twice now silently degraded (blocked
            # OSM tiles, CartoDB's new API-key requirement) without
            # raising an actual exception -- just a warning while still
            # "succeeding" and rendering broken/watermarked tiles.
            # Capture warnings so we can detect this and fall back
            # properly instead of trusting a clean-looking return.
            import warnings
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                ctx_basemap.add_basemap(ax, crs="EPSG:4326", source=provider,
                                        zoom="auto")

                # Only treat a warning as a real tile-access failure if it
                # actually mentions access/blocking/key issues -- unrelated
                # internal deprecation warnings (e.g. numpy matmul syntax
                # notices from contextily's own reprojection code) are
                # harmless noise and shouldn't discard perfectly good tiles.
                blocking_keywords = ("api key", "blocked", "403", "401",
                                    "unauthorized", "rate limit", "denied")
                real_failures = [
                    w for w in caught
                    if any(kw in str(w.message).lower() for kw in blocking_keywords)
                ]
                for w in caught:
                    print(f"  [basemap] contextily warning: {w.message}")
                if real_failures:
                    raise RuntimeError(
                        "contextily raised a warning indicating blocked/"
                        "unauthorized tile access -- treating as failure")

            used_contextily = True
        except ImportError:
            print("  [basemap] contextily not installed -- "
                  "falling back to coastline outlines. "
                  "Install with: pip install contextily")
        except Exception as e:
            print(f"  [basemap] contextily fetch failed: "
                  f"{type(e).__name__}: {e} -- falling back to "
                  f"coastline outlines")
            # Clear any broken/watermarked tile image that may already
            # have been drawn before the failure was detected, and
            # restore the axis extent (cla() resets it).
            ax.cla()
            ax.set_xlim(lons.min(), lons.max())
            ax.set_ylim(lats.min(), lats.max())

    contour_alpha = 0.65 if used_contextily else 1.0

    # Build the color levels according to SHAKEMAP_VMAX / SHAKEMAP_LOG_SCALE
    grid_max = float(pga_grid.max())
    vmax = SHAKEMAP_VMAX if SHAKEMAP_VMAX is not None else max(grid_max, 1e-6)

    if SHAKEMAP_LOG_SCALE:
        # Log-spaced levels from a small floor up to vmax, so both tiny
        # and large events show meaningful color variation in one scale.
        floor = max(vmax * 1e-4, 1e-6)
        levels = np.logspace(np.log10(floor), np.log10(max(vmax, floor * 10)), 20)
        norm = matplotlib.colors.LogNorm(vmin=floor, vmax=levels[-1])
        contour = ax.contourf(lons, lats, np.clip(pga_grid, floor, None),
                              levels=levels, cmap="YlOrRd", norm=norm,
                              alpha=contour_alpha)
    else:
        levels = np.linspace(0, vmax, 21)
        contour = ax.contourf(lons, lats, pga_grid, levels=levels,
                              cmap="YlOrRd", vmin=0, vmax=vmax, extend="max",
                              alpha=contour_alpha)
    scale_note = f"vmax={SHAKEMAP_VMAX}" if SHAKEMAP_VMAX is not None else "auto-scaled"
    scale_type = "log" if SHAKEMAP_LOG_SCALE else "linear"
    cbar = plt.colorbar(contour, label=f"Estimated PGA (%g), generic Vs30=400 m/s "
                                     f"[{scale_type}, {scale_note}]")
    if SHAKEMAP_LOG_SCALE:
        # The default tick locator doesn't handle these log-spaced
        # discrete contourf levels well (was showing only one readable
        # tick). Force proper log-decade ticks instead.
        cbar.ax.yaxis.set_major_locator(matplotlib.ticker.LogLocator(base=10))
        cbar.ax.yaxis.set_major_formatter(matplotlib.ticker.LogFormatter(base=10))

    if not used_contextily:
        world = _load_world_boundaries()
        if world is not None:
            try:
                world.plot(ax=ax, color="none", edgecolor="black",
                          linewidth=0.6, zorder=5)
            except Exception as e:
                print(f"  [basemap] Failed to plot boundaries: "
                  f"{type(e).__name__}: {e}")

    ax.plot(event_lon, event_lat, "k*", markersize=22, label="Epicenter", zorder=6)

    for name, dist, vs30, pga in in_range_results:
        # Recover this target's actual lat/lon from TARGET_LOCATIONS
        tlat, tlon = next((la, lo) for n, la, lo in TARGET_LOCATIONS if n == name)
        mmi, desc = pga_to_mmi_description(pga)
        ax.plot(tlon, tlat, "bo", markersize=9)
        ax.annotate(f"{name}\n{dist:.0f}km, Vs30={vs30:.0f}, "
                   f"PGA~{pga:.3f}%g\nMMI {mmi}: {desc}",
                   (tlon, tlat), textcoords="offset points", xytext=(8, 8),
                   fontsize=7, color="blue",
                   bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="blue", alpha=0.8))

    # Note: out-of-range targets aren't plotted here since they fall
    # outside this map's extent.

    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(f"{map_label} Shake Map ({gmpe_name} estimate, "
                f"{region_name}) -- M{magnitude} "
                f"{event['place']}\n{datetime.fromtimestamp(event['time'], tz=timezone.utc).isoformat()}")
    ax.legend(loc="upper right")
    plt.tight_layout()

    safe_place = "".join(c if c.isalnum() else "_" for c in event["place"])[:40]
    timestamp = datetime.fromtimestamp(event["time"], tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    filename = os.path.join(SHAKEMAP_OUTPUT_DIR,
                            f"shakemap_{timestamp}_{safe_place}{filename_suffix}.png")
    plt.savefig(filename, dpi=120)
    plt.close(fig)

    return filename


def generate_all_shakemaps(event, in_range_results, out_of_range_results):
    """
    Generate all three shake map outputs:
      1. Regional overview (span based on MAX_VALID_DISTANCE_KM, centered
         on the epicenter) -- the original behavior.
      2. Street-level view (STREET_MAP_SPAN_DEG, ~55km) -- centered on
         the nearest in-range target if one exists, otherwise the
         epicenter.
      3. Zoomed street-level close-up (ZOOM_STREET_MAP_SPAN_DEG, ~3.3km,
         building/street scale) -- same centering logic as #2.

    Returns a dict of {label: filepath_or_None}.
    """
    results = {}

    # Map 1: regional overview, centered on the epicenter (unchanged
    # default behavior)
    results["regional"] = generate_shakemap_plot(
        event, in_range_results, out_of_range_results,
        filename_suffix="", map_label="Regional")

    # Pick a center for the two zoomed views: the closest in-range
    # target if any, otherwise fall back to the epicenter itself.
    if in_range_results:
        closest = min(in_range_results, key=lambda r: r[1])  # by distance
        closest_name = closest[0]
        center_lat, center_lon = next(
            (la, lo) for n, la, lo in TARGET_LOCATIONS if n == closest_name)
    else:
        center_lat, center_lon = event["lat"], event["lon"]

    # Map 2: street-level view
    results["street"] = generate_shakemap_plot(
        event, in_range_results, out_of_range_results,
        center_lat=center_lat, center_lon=center_lon,
        span_deg=STREET_MAP_SPAN_DEG,
        provider_name=SHAKEMAP_CONTEXTILY_PROVIDER,
        filename_suffix="_street", map_label="Street")

    # Map 3: zoomed street-level close-up (building/street scale)
    results["zoom_street"] = generate_shakemap_plot(
        event, in_range_results, out_of_range_results,
        center_lat=center_lat, center_lon=center_lon,
        span_deg=ZOOM_STREET_MAP_SPAN_DEG,
        provider_name=SHAKEMAP_CONTEXTILY_PROVIDER,
        filename_suffix="_zoom_street", map_label="Zoomed Street")

    return results


def print_shaking_estimate(event):
    try:
        results, out_of_range, (region_name, tectonic_type, gmpe_name,
                               caution_note) = compute_shaking_at_targets(
            event["mag"], event.get("depth", 10.0),
            event["lat"], event["lon"]
        )
    except Exception as e:
        print(f"\n  --- Estimated shaking for M{event['mag']} "
              f"{event['place']} ---")
        print(f"      Shaking computation failed: {type(e).__name__}: {e}")
        return

    print(f"\n  --- Estimated shaking ({gmpe_name}) for M{event['mag']} "
          f"{event['place']} ---")
    print(f"      Region: {region_name} ({tectonic_type})")
    if caution_note:
        print(f"      CAUTION: {caution_note}")

    for name, dist, vs30, pga in results:
        mmi, desc = pga_to_mmi_description(pga)
        print(f"      {name:<20} {dist:>8.0f} km   Vs30={vs30:>6.0f} m/s   "
              f"PGA ~{pga:>7.3f} %g   MMI {mmi:<7} {desc}")

    for name, dist in out_of_range:
        print(f"      {name:<20} {dist:>8.0f} km   "
              f"(beyond {MAX_VALID_DISTANCE_KM}km -- outside {gmpe_name}'s "
              f"valid range, skipped)")

    map_paths = {}
    try:
        map_paths = generate_all_shakemaps(event, results, out_of_range)
        for label, path in map_paths.items():
            if path:
                print(f"      Shake map ({label}) saved: {path}")
    except Exception as e:
        print(f"      Shake map generation failed: {type(e).__name__}: {e}")

    log_shaking_estimate_to_db(event.get("_db_id"), region_name,
                              tectonic_type, gmpe_name, caution_note, results,
                              map_paths=map_paths)

    print()


# ============================================================================
# Event detection + dedup (from global_earthquake_monitor.py)
# ============================================================================

def find_matching_event(t, lat, lon, mag):
    for ev in _known_events:
        if abs(ev["time"] - t) > DEDUP_TIME_SEC:
            continue
        if abs(ev["lat"] - lat) > DEDUP_DIST_DEG or abs(ev["lon"] - lon) > DEDUP_DIST_DEG:
            continue
        if mag is not None and ev["mag"] is not None and abs(ev["mag"] - mag) > DEDUP_MAG_DELTA:
            continue
        return ev
    return None


def report_event(source, t, lat, lon, mag, depth, place):
    if mag is not None and mag < MIN_MAGNITUDE:
        return
    if not in_bbox(lat, lon):
        return

    with _lock:
        match = find_matching_event(t, lat, lon, mag)
        if match:
            if source not in match["sources"]:
                match["sources"].add(source)
                print(f"  [confirmed by {source}] M{mag} {place} "
                      f"(now confirmed by: {sorted(match['sources'])})")
            return

        event = {
            "time": t, "lat": lat, "lon": lon, "mag": mag,
            "depth": depth, "place": place, "sources": {source},
        }
        _known_events.append(event)

    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    print(f"\n  *** NEW EVENT ({source}) ***")
    print(f"      M{mag}  {place}")
    print(f"      Time: {dt.isoformat()}")
    print(f"      Location: {lat:.4f}, {lon:.4f}  Depth: {depth} km\n")

    send_event_notifications(event)
    event["_db_id"] = log_event_to_db(event, {source})

    if mag is not None and mag >= SHAKING_MAGNITUDE_THRESHOLD:
        print_shaking_estimate(event)


# ============================================================================
# EMSC WebSocket
# ============================================================================

def emsc_on_message(ws, message):
    try:
        payload = json.loads(message)
        data = payload.get("data", {}).get("properties", payload.get("data", {}))
        lat = data.get("lat")
        lon = data.get("lon")
        mag = data.get("mag")
        depth = data.get("depth", 10.0)
        place = data.get("flynn_region") or data.get("region")
        time_str = data.get("time")

        if lat is None or lon is None or time_str is None:
            return

        t = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S.%fZ") \
            .replace(tzinfo=timezone.utc).timestamp()

        report_event("EMSC", t, lat, lon, mag, depth, place)
    except Exception as e:
        print(f"  [EMSC] failed to parse message: {e}")


def emsc_on_error(ws, error):
    print(f"  [EMSC] connection error: {error}")


def emsc_on_close(ws, close_status_code, close_msg):
    print("  [EMSC] connection closed")


def emsc_on_open(ws):
    print("  [EMSC] connected, listening for real-time events...")


def run_emsc_listener():
    while True:
        try:
            ws = websocket.WebSocketApp(
                EMSC_WS_URL,
                on_open=emsc_on_open,
                on_message=emsc_on_message,
                on_error=emsc_on_error,
                on_close=emsc_on_close,
            )
            ws.run_forever()
        except Exception as e:
            print(f"  [EMSC] listener crashed: {e}")
        print("  [EMSC] reconnecting in 10s...")
        time.sleep(10)


# ============================================================================
# USGS polling
# ============================================================================

def poll_usgs():
    try:
        resp = requests.get(USGS_FEED_URL, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [USGS] request failed: {e}")
        return

    data = resp.json()
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [None, None, None])
        lon, lat, depth = coords
        mag = props.get("mag")
        place = props.get("place")
        time_ms = props.get("time")

        if lat is None or lon is None or time_ms is None:
            continue

        t = time_ms / 1000
        report_event("USGS", t, lat, lon, mag, depth or 10.0, place)


def run_usgs_poller():
    while True:
        poll_usgs()
        time.sleep(USGS_POLL_INTERVAL_SEC)


# ============================================================================

def run_test_map(lat=19.5, lon=-155.3, mag=6.5, depth=10.0,
                 place="TEST EVENT near Hilo, Hawaii (synthetic, not real)"):
    """Generate one shake map immediately using a synthetic, high-magnitude
    event at the given location, so the full map (coastlines + strong
    contours + target markers) can be visually verified without waiting
    on real events of sufficient size to happen nearby. Defaults to a
    Hilo, Hawaii scenario if no location is given.

    Also logs the test event to the history database (tagged with
    source "TEST"), so it shows up in the web dashboard for testing that
    pipeline too -- this was missing initially, which is why test events
    weren't appearing in the dashboard."""
    if not _HAZARDLIB_OK:
        print("ERROR: openquake.hazardlib not found. Run inside the "
              "openquake_conda environment.")
        return

    init_history_db()

    test_event = {
        "time": time.time(),
        "lat": lat,
        "lon": lon,
        "mag": mag,
        "depth": depth,
        "place": place,
    }

    test_event["_db_id"] = log_event_to_db(test_event, {"TEST"})

    print(f"Generating a TEST shake map with a synthetic M{mag} event "
          f"at ({lat}, {lon}).")
    print("This event is NOT real -- purely to verify the map pipeline "
          "(coastlines, contours, target markers) AND notifications "
          "(email/webhook) end-to-end.\n")

    send_event_notifications(test_event)
    print_shaking_estimate(test_event)


def print_history_summary():
    """Print a quick summary of events logged in the history database:
    total count, most recent events, and highest-magnitude events."""
    if not os.path.exists(HISTORY_DB_PATH):
        print(f"No history database found at '{HISTORY_DB_PATH}' -- "
              f"run the monitor at least once first.")
        return

    conn = sqlite3.connect(HISTORY_DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"Total events logged: {total}\n")

    print("Most recent 10 events:")
    rows = conn.execute("""
        SELECT event_time, mag, place, region_name, gmpe_name
        FROM events ORDER BY event_time DESC LIMIT 10
    """).fetchall()
    for event_time, mag, place, region_name, gmpe_name in rows:
        dt = datetime.fromtimestamp(event_time, tz=timezone.utc).isoformat()
        region_str = f" [{region_name}, {gmpe_name}]" if region_name else ""
        print(f"  M{mag}  {place}  ({dt}){region_str}")

    print("\nHighest-magnitude 10 events logged:")
    rows = conn.execute("""
        SELECT event_time, mag, place FROM events
        ORDER BY mag DESC LIMIT 10
    """).fetchall()
    for event_time, mag, place in rows:
        dt = datetime.fromtimestamp(event_time, tz=timezone.utc).isoformat()
        print(f"  M{mag}  {place}  ({dt})")

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Real-time earthquake monitor with shaking estimation")
    parser.add_argument("--test-map", action="store_true",
                        help="Generate one shake map immediately using a "
                             "synthetic event, instead of running the "
                             "live monitor. Defaults to a M6.5 near Hilo, "
                             "Hawaii, unless overridden with the options "
                             "below or --test-preset.")
    parser.add_argument("--test-preset", choices=["hilo", "dead_sea"],
                        default="hilo",
                        help="Quick preset location for --test-map: "
                             "'hilo' (default, Hawaii/volcanic region) "
                             "or 'dead_sea' (Dead Sea Transform, near "
                             "Tel Aviv/Jerusalem -- tests Middle East "
                             "GMPE routing).")
    parser.add_argument("--test-lat", type=float, help="Override test "
                        "event latitude")
    parser.add_argument("--test-lon", type=float, help="Override test "
                        "event longitude")
    parser.add_argument("--test-mag", type=float, default=6.5,
                        help="Test event magnitude (default 6.5)")
    parser.add_argument("--test-depth", type=float, default=10.0,
                        help="Test event depth in km (default 10)")
    parser.add_argument("--history-summary", action="store_true",
                        help="Print a quick summary of events logged so "
                             "far in the history database, then exit "
                             "(doesn't run the monitor).")
    args = parser.parse_args()

    if args.history_summary:
        print_history_summary()
        return

    if args.test_map:
        presets = {
            "hilo": (19.5, -155.3,
                    "TEST EVENT near Hilo, Hawaii (synthetic, not real)"),
            "dead_sea": (31.6, 35.4,
                        "TEST EVENT on the Dead Sea Transform, near "
                        "Jericho (synthetic, not real)"),
        }
        preset_lat, preset_lon, preset_place = presets[args.test_preset]
        lat = args.test_lat if args.test_lat is not None else preset_lat
        lon = args.test_lon if args.test_lon is not None else preset_lon
        run_test_map(lat=lat, lon=lon, mag=args.test_mag,
                    depth=args.test_depth, place=preset_place)
        return

    init_history_db()
    print(f"Event history database: {HISTORY_DB_PATH}\n")

    print("Starting combined earthquake monitor + shaking estimator")
    print(f"Min magnitude tracked: {MIN_MAGNITUDE}")
    print(f"Shaking estimate threshold: M{SHAKING_MAGNITUDE_THRESHOLD}+")
    print(f"Target locations: {[name for name, _, _ in TARGET_LOCATIONS]}")
    print(f"Region filter: {'global' if BBOX is None else BBOX}\n")
    print("NOTE: this reports earthquakes that have already occurred, "
          "and estimates shaking using a real, region-appropriate GMPE "
          "(selected per event's location -- see [gmpe] output below). "
          "It cannot "
          "predict earthquakes before they happen.\n")

    if not _HAZARDLIB_OK:
        print("ERROR: openquake.hazardlib not found. This script must "
              "be run inside the openquake_conda environment:\n"
              "    conda activate openquake_conda\n"
              "    python earthquake_monitor_with_shaking.py")
        return

    emsc_thread = threading.Thread(target=run_emsc_listener, daemon=True)
    emsc_thread.start()

    try:
        run_usgs_poller()
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()