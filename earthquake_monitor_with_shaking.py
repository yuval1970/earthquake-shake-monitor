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


# (region_name, minlat, maxlat, minlon, maxlon, tectonic_type,
#  module_path, class_name, caution_note)
# Checked in order; first bounding-box match wins. Longitude ranges that
# cross the antimeridian (e.g. the Pacific) are split into two entries.
_REGION_GMPE_TABLE = [
    ("Middle East / Mediterranean", 25, 45, 20, 50,
     "Active Shallow Crust",
     "openquake.hazardlib.gsim.akkar_2014", "AkkarEtAlRjb2014", None),

    ("Western North America (California-like)", 30, 50, -125, -110,
     "Active Shallow Crust (California-calibrated)",
     "openquake.hazardlib.gsim.boore_2014", "BooreEtAl2014", None),

    ("Hawaii", 18, 23, -161, -154,
     "Volcanic",
     "openquake.hazardlib.gsim.boore_2014", "BooreEtAl2014",
     "Volcanic/flank earthquakes are NOT well represented by standard "
     "tectonic GMPEs -- this is a rough crustal-model proxy, treat "
     "with extra caution"),

    ("Alaska / Aleutians (subduction)", 50, 72, -180, -130,
     "Subduction Interface",
     "openquake.hazardlib.gsim.zhao_2006", "ZhaoEtAl2006SInter", None),

    ("Tonga / Kermadec (subduction)", -30, -14, -180, -170,
     "Subduction Interface",
     "openquake.hazardlib.gsim.zhao_2006", "ZhaoEtAl2006SInter", None),

    ("Japan (subduction)", 24, 46, 122, 148,
     "Subduction Interface",
     "openquake.hazardlib.gsim.zhao_2006", "ZhaoEtAl2006SInter", None),

    ("Indonesia (subduction)", -11, 6, 95, 141,
     "Subduction Interface",
     "openquake.hazardlib.gsim.zhao_2006", "ZhaoEtAl2006SInter", None),

    ("Central/Eastern US (stable continental)", 25, 50, -105, -65,
     "Stable Continental",
     "openquake.hazardlib.gsim.atkinson_boore_2006", "AtkinsonBoore2006", None),
]

_DEFAULT_REGION = ("Unclassified (default fallback)", "Active Shallow Crust "
                  "(generic default)", "openquake.hazardlib.gsim.akkar_2014",
                  "AkkarEtAlRjb2014",
                  "Event location didn't match any known region -- using "
                  "a generic default model. Treat with extra caution.")


def select_gmpe_for_location(lat, lon):
    """
    Return (region_name, tectonic_type, gmpe_instance, gmpe_name,
    caution_note_or_None) for the given location, based on
    _REGION_GMPE_TABLE. Falls back to _DEFAULT_REGION if nothing matches.
    """
    for (name, minlat, maxlat, minlon, maxlon, tectonic_type,
        module_path, class_name, note) in _REGION_GMPE_TABLE:
        if minlat <= lat <= maxlat and minlon <= lon <= maxlon:
            instance = _get_gmpe_instance(module_path, class_name)
            if instance is not None:
                return name, tectonic_type, instance, class_name, note

    # Fallback: default region, or a matched region whose GMPE class
    # failed to load
    name, tectonic_type, module_path, class_name, note = _DEFAULT_REGION
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
        _DEFAULT_REGION[2], _DEFAULT_REGION[3])
    _HAZARDLIB_OK = _DEFAULT_GMPE_INSTANCE is not None
except ImportError:
    _HAZARDLIB_OK = False
    _GMPE_NAME = "unknown"

# --------------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------------

EMSC_WS_URL = "wss://www.seismicportal.eu/standing_order/websocket"
USGS_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
USGS_POLL_INTERVAL_SEC = 60

MIN_MAGNITUDE = 0.0      # minimum magnitude to even track/report at all
BBOX = None                # None = global. Or (minlat, maxlat, minlon, maxlon)

# Deduplication thresholds (same event reported by both EMSC and USGS)
DEDUP_TIME_SEC = 90
DEDUP_DIST_DEG = 1.0
DEDUP_MAG_DELTA = 1.0

# Only run the (heavier) shaking computation for events at or above this
# magnitude -- avoids wasting time computing shaking estimates for tiny,
# clearly-inconsequential events.
SHAKING_MAGNITUDE_THRESHOLD = 5.0

# Locations to estimate shaking at, whenever a qualifying event occurs.
# (name, lat, lon)
TARGET_LOCATIONS = [
    ("Tel Aviv", 32.0853, 34.7818),
    ("Jerusalem", 31.7683, 35.2137),
    ("Los Angeles", 34.0522, -118.2437),
    ("Athens", 37.9838, 23.7275),
    ("Nuku'alofa, Tonga", -21.1394, -175.2049),
    ("Hilo, Hawaii", 19.7297, -155.0900),  # near frequent Big Island seismicity
]

# BSSA14 (like most GMPEs) is empirically calibrated from real recordings
# typically only out to a few hundred km. Beyond this range you're not
# just getting "very small" numbers -- you're extrapolating the model
# far outside where it was ever validated, and the physical mechanism
# of any distant shaking (long-period surface waves) isn't what these
# near-field body-wave models represent anyway. Skip the computation
# entirely beyond this distance rather than showing misleading precision
# on a physically meaningless number.
MAX_VALID_DISTANCE_KM = 300

# Shake map color scale. By default, each map auto-scales its color range
# to that event's own min/max PGA -- which means a tiny M2 event and a
# large M6.5 event will use completely different scales (as you likely
# noticed comparing two test maps), making them NOT visually comparable
# to each other. Set SHAKEMAP_VMAX to a fixed number (in %g) to force
# every map to use the same scale instead, so events become visually
# comparable to one another. Leave as None to keep auto-scaling
# (best per-event detail, but not comparable across events).
SHAKEMAP_VMAX = None  # e.g. 30.0 for a fixed 0-30%g scale on every map

# PGA can span orders of magnitude between small and large events. A
# logarithmic color scale shows meaningful detail across that whole
# range in one map, instead of a linear scale where small events look
# almost entirely blank. Only affects the color mapping, not the
# underlying computed values.
SHAKEMAP_LOG_SCALE = False

# Use contextily to draw real web-map basemap tiles (streets, terrain, or
# satellite imagery) underneath the shake contours, instead of just bare
# coastline outlines. Requires internet access at runtime (fetches map
# tiles live) and `pip install contextily`. Falls back to the geopandas
# coastline layer if contextily isn't installed or a tile fetch fails.
SHAKEMAP_USE_CONTEXTILY = True
# Pick a tile provider. Both OpenStreetMap (strict automated-usage policy)
# and CartoDB (now requires an API key) have proven unreliable for this
# kind of use. Esri's public basemap tiles work without any API key.
# "WorldStreetMap" shows the actual road network clearly (unlike
# WorldTopoMap, which is terrain/elevation-focused and de-emphasizes
# roads). Other options: "Esri.WorldImagery" (satellite photo).
SHAKEMAP_CONTEXTILY_PROVIDER = "Esri.WorldStreetMap"

# Span (in degrees lat/lon) for the two additional zoomed outputs.
# Regional map span is calculated automatically from MAX_VALID_DISTANCE_KM.
STREET_MAP_SPAN_DEG = 0.5     # ~55km -- metro-area street-level view
ZOOM_STREET_MAP_SPAN_DEG = 0.03  # ~3.3km -- close-up, building/street scale

DEFAULT_VS30 = 400.0  # fallback ONLY if the real USGS lookup fails

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


SHAKEMAP_OUTPUT_DIR = "shakemaps"

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

    try:
        map_paths = generate_all_shakemaps(event, results, out_of_range)
        for label, path in map_paths.items():
            if path:
                print(f"      Shake map ({label}) saved: {path}")
    except Exception as e:
        print(f"      Shake map generation failed: {type(e).__name__}: {e}")

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
    Hilo, Hawaii scenario if no location is given."""
    if not _HAZARDLIB_OK:
        print("ERROR: openquake.hazardlib not found. Run inside the "
              "openquake_conda environment.")
        return

    test_event = {
        "time": time.time(),
        "lat": lat,
        "lon": lon,
        "mag": mag,
        "depth": depth,
        "place": place,
    }

    print(f"Generating a TEST shake map with a synthetic M{mag} event "
          f"at ({lat}, {lon}).")
    print("This event is NOT real -- purely to verify the map pipeline "
          "(coastlines, contours, target markers) end-to-end.\n")

    print_shaking_estimate(test_event)


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
    args = parser.parse_args()

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