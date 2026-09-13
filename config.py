"""
config.py

All configurable parameters for the Earthquake Monitor + Shaking Estimator,
in one place. Every scalar setting can be overridden via an environment
variable of the same name, without editing this file or rebuilding the
Docker image -- e.g.:

    docker run -e SHAKING_MAGNITUDE_THRESHOLD=3.0 -e NOTIFY_EMAIL=true ... earthquake-monitor

Or, more conveniently for several settings at once, put them in a file and
use Docker's built-in --env-file flag (no extra Python dependency needed):

    docker run --env-file .env ... earthquake-monitor

.env file format (one per line, no quotes needed):
    SHAKING_MAGNITUDE_THRESHOLD=3.0
    NOTIFY_EMAIL=true
    SMTP_USERNAME=you@gmail.com
    SMTP_PASSWORD=your16charapppassword
    EMAIL_TO=you@gmail.com

If a setting has no matching environment variable, the default value
below is used -- nothing changes for existing usage that doesn't set any
environment variables at all.

TARGET_LOCATIONS and REGION_GMPE_TABLE are structured data (lists of
tuples), not simple scalars -- they keep their Python defaults below, but
TARGET_LOCATIONS can optionally be fully overridden via a JSON environment
variable (see that section) for advanced use without editing this file.

See README.md for a full explanation of each section.
"""

import os
import json


def _env_str(name, default):
    """Read a string environment variable, falling back to default if unset."""
    return os.environ.get(name, default)


def _env_float(name, default):
    """Read a float environment variable, falling back to default if unset
    or unparseable (with a printed warning in the latter case)."""
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        print(f"  [config] Warning: env var {name}='{val}' isn't a valid "
              f"number -- using default {default} instead.")
        return default


def _env_float_or_none(name, default):
    """Like _env_float, but treats the literal string 'none' (any case)
    as None -- used for settings whose default is None (e.g. SHAKEMAP_VMAX,
    BBOX)."""
    val = os.environ.get(name)
    if val is None:
        return default
    if val.strip().lower() == "none":
        return None
    try:
        return float(val)
    except ValueError:
        print(f"  [config] Warning: env var {name}='{val}' isn't a valid "
              f"number or 'none' -- using default {default} instead.")
        return default


def _env_bool(name, default):
    """Read a boolean environment variable. Accepts true/false/1/0/yes/no
    (case-insensitive). Falls back to default if unset or unrecognized."""
    val = os.environ.get(name)
    if val is None:
        return default
    val_lower = val.strip().lower()
    if val_lower in ("true", "1", "yes", "on"):
        return True
    if val_lower in ("false", "0", "no", "off"):
        return False
    print(f"  [config] Warning: env var {name}='{val}' isn't a recognized "
          f"boolean (use true/false) -- using default {default} instead.")
    return default


def _env_int(name, default):
    """Read an integer environment variable, falling back to default if
    unset or unparseable."""
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        print(f"  [config] Warning: env var {name}='{val}' isn't a valid "
              f"integer -- using default {default} instead.")
        return default


# ============================================================================
# DETECTION
# ============================================================================

EMSC_WS_URL = _env_str("EMSC_WS_URL",
                      "wss://www.seismicportal.eu/standing_order/websocket")
USGS_FEED_URL = _env_str("USGS_FEED_URL",
                        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson")
USGS_POLL_INTERVAL_SEC = _env_int("USGS_POLL_INTERVAL_SEC", 60)

MIN_MAGNITUDE = _env_float("MIN_MAGNITUDE", 0.0)

# BBOX: None = global, or "minlat,maxlat,minlon,maxlon" (comma-separated,
# no spaces) via the BBOX environment variable, e.g.
# BBOX="25,45,20,50" for a Middle East box.
_bbox_env = os.environ.get("BBOX")
if _bbox_env is None:
    BBOX = None
elif _bbox_env.strip().lower() == "none":
    BBOX = None
else:
    try:
        BBOX = tuple(float(x) for x in _bbox_env.split(","))
        if len(BBOX) != 4:
            raise ValueError("BBOX must have exactly 4 comma-separated values")
    except ValueError as e:
        print(f"  [config] Warning: env var BBOX='{_bbox_env}' is invalid "
              f"({e}) -- using default (global, None) instead.")
        BBOX = None

# Deduplication thresholds (same event reported by both EMSC and USGS)
DEDUP_TIME_SEC = _env_float("DEDUP_TIME_SEC", 90)
DEDUP_DIST_DEG = _env_float("DEDUP_DIST_DEG", 1.0)
DEDUP_MAG_DELTA = _env_float("DEDUP_MAG_DELTA", 1.0)

# ============================================================================
# SHAKING ESTIMATION
# ============================================================================

# Only run the (heavier) shaking computation for events at or above this
# magnitude -- avoids wasting time computing shaking estimates for tiny,
# clearly-inconsequential events.
SHAKING_MAGNITUDE_THRESHOLD = _env_float("SHAKING_MAGNITUDE_THRESHOLD", 5.0)

# Locations to estimate shaking at, whenever a qualifying event occurs.
# (name, lat, lon) -- edit the default list below, OR override entirely
# via the TARGET_LOCATIONS_JSON environment variable, e.g.:
#   TARGET_LOCATIONS_JSON='[["Tel Aviv",32.0853,34.7818],["Athens",37.9838,23.7275]]'
_default_target_locations = [
    ("Tel Aviv", 32.0853, 34.7818),
    ("Jerusalem", 31.7683, 35.2137),
    ("Los Angeles", 34.0522, -118.2437),
    ("Athens", 37.9838, 23.7275),
    ("Nuku'alofa, Tonga", -21.1394, -175.2049),
    ("Hilo, Hawaii", 19.7297, -155.0900),  # near frequent Big Island seismicity
]
_target_locations_json = os.environ.get("TARGET_LOCATIONS_JSON")
if _target_locations_json:
    try:
        parsed = json.loads(_target_locations_json)
        TARGET_LOCATIONS = [tuple(entry) for entry in parsed]
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"  [config] Warning: TARGET_LOCATIONS_JSON is invalid "
              f"({e}) -- using default target list instead.")
        TARGET_LOCATIONS = _default_target_locations
else:
    TARGET_LOCATIONS = _default_target_locations

# Most GMPEs are empirically calibrated from real recordings typically only
# out to a few hundred km. Beyond this range you're not just getting "very
# small" numbers -- you're extrapolating the model far outside where it was
# ever validated. Skip the computation entirely beyond this distance rather
# than showing misleading precision on a physically meaningless number.
MAX_VALID_DISTANCE_KM = _env_float("MAX_VALID_DISTANCE_KM", 300)

DEFAULT_VS30 = _env_float("DEFAULT_VS30", 400.0)  # fallback ONLY if all Vs30 lookups fail

# Real global Vs30 data (Heath et al. 2020, USGS, CC0 public domain),
# served as a Cloud-Optimized GeoTIFF from S3. Used to give the shake
# map's background CONTOUR real per-pixel site conditions, instead of
# the flat generic assumption previously used there (only the labeled
# target markers had real Vs30 before this). GDAL reads just the small
# windowed region each map needs via HTTP range requests -- the full
# 631MB file is never downloaded.
VS30_RASTER_URL = _env_str(
    "VS30_RASTER_URL",
    "https://prod-is-usgs-sb-prod-publish.s3.amazonaws.com/67be4ac3d34e8876fcbfbd89/vs30_mosaic_median_30c.tif"
)
USE_REAL_VS30_RASTER_FOR_BACKGROUND = _env_bool("USE_REAL_VS30_RASTER_FOR_BACKGROUND", True)

# ----------------------------------------------------------------------
# Region-aware GMPE selection
# ----------------------------------------------------------------------
# A single fixed GMPE will still compute *a* number for an earthquake
# anywhere on Earth -- it has no built-in awareness of geography -- but the
# result silently degrades in accuracy the further the event is from the
# tectonic setting the model was actually calibrated on. This table maps
# rough regional bounding boxes to an appropriate model.
#
# This structured table is NOT environment-variable overridable (too
# complex to represent cleanly as a string) -- edit it directly below if
# you need to add/change regions, then rebuild the Docker image.
#
# (region_name, minlat, maxlat, minlon, maxlon, tectonic_type,
#  module_path, class_name, caution_note)
# Checked in order; first bounding-box match wins. Longitude ranges that
# cross the antimeridian (e.g. the Pacific) are split into two entries.
REGION_GMPE_TABLE = [
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

# Used when no entry in REGION_GMPE_TABLE matches the event's location, or
# when a matched region's GMPE class fails to load.
DEFAULT_REGION = ("Unclassified (default fallback)", "Active Shallow Crust "
                 "(generic default)", "openquake.hazardlib.gsim.akkar_2014",
                 "AkkarEtAlRjb2014",
                 "Event location didn't match any known region -- using "
                 "a generic default model. Treat with extra caution.")

# ============================================================================
# MAPS / VISUALIZATION
# ============================================================================

SHAKEMAP_OUTPUT_DIR = _env_str("SHAKEMAP_OUTPUT_DIR", "shakemaps")

# By default, each map auto-scales its color range to that event's own
# min/max PGA -- which means a tiny M2 event and a large M6.5 event will
# use completely different scales, making them NOT visually comparable to
# each other. Set SHAKEMAP_VMAX to a fixed number (in %g) to force every
# map to use the same scale instead. Leave as None to keep auto-scaling
# (best per-event detail, but not comparable across events).
SHAKEMAP_VMAX = _env_float_or_none("SHAKEMAP_VMAX", None)  # e.g. "30.0" via env for a fixed 0-30%g scale

# PGA can span orders of magnitude between small and large events. A
# logarithmic color scale shows meaningful detail across that whole range
# in one map, instead of a linear scale where small events look almost
# entirely blank -- but it also visually compresses severity differences.
SHAKEMAP_LOG_SCALE = _env_bool("SHAKEMAP_LOG_SCALE", False)

# Use contextily to draw real web-map basemap tiles (streets, terrain, or
# satellite imagery) underneath the shake contours, instead of just bare
# coastline outlines. Requires internet access at runtime and
# `pip install contextily`. Falls back to the geopandas coastline layer
# if contextily isn't installed or a tile fetch fails.
SHAKEMAP_USE_CONTEXTILY = _env_bool("SHAKEMAP_USE_CONTEXTILY", True)

# Pick a tile provider. Both OpenStreetMap (strict automated-usage policy)
# and CartoDB (now requires an API key) have proven unreliable for this
# kind of use. Esri's public basemap tiles work without any API key.
# "WorldStreetMap" shows the actual road network clearly (unlike
# WorldTopoMap, which is terrain/elevation-focused and de-emphasizes
# roads). Other options: "Esri.WorldImagery" (satellite photo).
SHAKEMAP_CONTEXTILY_PROVIDER = _env_str("SHAKEMAP_CONTEXTILY_PROVIDER",
                                       "Esri.WorldStreetMap")

# Span (in degrees lat/lon) for the two additional zoomed map outputs.
# Regional map span is calculated automatically from MAX_VALID_DISTANCE_KM.
STREET_MAP_SPAN_DEG = _env_float("STREET_MAP_SPAN_DEG", 0.5)        # ~55km
ZOOM_STREET_MAP_SPAN_DEG = _env_float("ZOOM_STREET_MAP_SPAN_DEG", 0.03)  # ~3.3km

# ============================================================================
# NOTIFICATIONS
# ============================================================================

# Magnitude threshold for ALL notification channels below (separate from
# SHAKING_MAGNITUDE_THRESHOLD, so you can be notified about smaller events
# than the ones that trigger a full shaking computation, or vice versa).
NOTIFY_MAGNITUDE_THRESHOLD = _env_float("NOTIFY_MAGNITUDE_THRESHOLD", 4.0)

# Email notifications, via standard SMTP (Python's built-in smtplib --
# no extra dependency). Works with Gmail, Outlook, or any SMTP provider.
# Set NOTIFY_EMAIL=true (via env or below) and fill in the settings to
# enable.
#
# For Gmail specifically: you cannot use your normal account password --
# Google requires a 16-character "App Password" instead. Generate one at
# https://myaccount.google.com/apppasswords (requires 2-Step Verification
# to be enabled on the account first; when creating the password, choose
# "Other (Custom name)" as the app).
#
# RECOMMENDED: set these via a --env-file rather than editing this file
# directly, so credentials never end up committed to git by accident.
NOTIFY_EMAIL = _env_bool("NOTIFY_EMAIL", False)
SMTP_HOST = _env_str("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _env_int("SMTP_PORT", 587)  # 587 = STARTTLS (most providers); 465 = implicit SSL
SMTP_USERNAME = _env_str("SMTP_USERNAME", "")          # e.g. "youraddress@gmail.com"
SMTP_PASSWORD = _env_str("SMTP_PASSWORD", "")          # app password, NOT your normal account password
EMAIL_FROM = _env_str("EMAIL_FROM", "")                # usually same as SMTP_USERNAME
EMAIL_TO = _env_str("EMAIL_TO", "")                    # where alerts get sent -- can be the same address

# Optional webhook notification (Slack or Discord). Leave WEBHOOK_URL unset
# to disable. WEBHOOK_TYPE picks the correct JSON payload shape.
WEBHOOK_URL = _env_str("WEBHOOK_URL", None) or None  # e.g. Slack/Discord webhook URL
WEBHOOK_TYPE = _env_str("WEBHOOK_TYPE", "slack")  # "slack" or "discord"

# ============================================================================
# EVENT HISTORY (SQLite)
# ============================================================================

# Every detected event (and its shaking estimate, if computed) is logged
# to a local SQLite database, so history survives restarts and can be
# queried/analyzed later without needing the script to be running.
HISTORY_DB_PATH = _env_str("HISTORY_DB_PATH", "earthquake_history.db")
