"""
config.py

All configurable parameters for the Earthquake Monitor + Shaking Estimator,
in one place. Edit values here rather than inside earthquake_monitor_with_shaking.py
itself, so upgrades to the main script don't require re-applying your
personal settings each time.

See README.md for a full explanation of each section.
"""

# ============================================================================
# DETECTION
# ============================================================================

EMSC_WS_URL = "wss://www.seismicportal.eu/standing_order/websocket"
USGS_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
USGS_POLL_INTERVAL_SEC = 60

MIN_MAGNITUDE = 0.0      # minimum magnitude to even track/report at all
BBOX = None                # None = global. Or (minlat, maxlat, minlon, maxlon)

# Deduplication thresholds (same event reported by both EMSC and USGS)
DEDUP_TIME_SEC = 90
DEDUP_DIST_DEG = 1.0
DEDUP_MAG_DELTA = 1.0

# ============================================================================
# SHAKING ESTIMATION
# ============================================================================

# Only run the (heavier) shaking computation for events at or above this
# magnitude -- avoids wasting time computing shaking estimates for tiny,
# clearly-inconsequential events.
SHAKING_MAGNITUDE_THRESHOLD = 5.0

# Locations to estimate shaking at, whenever a qualifying event occurs.
# (name, lat, lon) -- EDIT THIS to your own locations of interest.
TARGET_LOCATIONS = [
    ("Tel Aviv", 32.0853, 34.7818),
    ("Jerusalem", 31.7683, 35.2137),
    ("Los Angeles", 34.0522, -118.2437),
    ("Athens", 37.9838, 23.7275),
    ("Nuku'alofa, Tonga", -21.1394, -175.2049),
    ("Hilo, Hawaii", 19.7297, -155.0900),  # near frequent Big Island seismicity
]

# Most GMPEs are empirically calibrated from real recordings typically only
# out to a few hundred km. Beyond this range you're not just getting "very
# small" numbers -- you're extrapolating the model far outside where it was
# ever validated. Skip the computation entirely beyond this distance rather
# than showing misleading precision on a physically meaningless number.
MAX_VALID_DISTANCE_KM = 300

DEFAULT_VS30 = 400.0  # fallback ONLY if the real USGS Vs30 lookup fails

# ----------------------------------------------------------------------
# Region-aware GMPE selection
# ----------------------------------------------------------------------
# A single fixed GMPE will still compute *a* number for an earthquake
# anywhere on Earth -- it has no built-in awareness of geography -- but the
# result silently degrades in accuracy the further the event is from the
# tectonic setting the model was actually calibrated on. This table maps
# rough regional bounding boxes to an appropriate model.
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

SHAKEMAP_OUTPUT_DIR = "shakemaps"

# By default, each map auto-scales its color range to that event's own
# min/max PGA -- which means a tiny M2 event and a large M6.5 event will
# use completely different scales, making them NOT visually comparable to
# each other. Set SHAKEMAP_VMAX to a fixed number (in %g) to force every
# map to use the same scale instead. Leave as None to keep auto-scaling
# (best per-event detail, but not comparable across events).
SHAKEMAP_VMAX = None  # e.g. 30.0 for a fixed 0-30%g scale on every map

# PGA can span orders of magnitude between small and large events. A
# logarithmic color scale shows meaningful detail across that whole range
# in one map, instead of a linear scale where small events look almost
# entirely blank -- but it also visually compresses severity differences.
SHAKEMAP_LOG_SCALE = False

# Use contextily to draw real web-map basemap tiles (streets, terrain, or
# satellite imagery) underneath the shake contours, instead of just bare
# coastline outlines. Requires internet access at runtime and
# `pip install contextily`. Falls back to the geopandas coastline layer
# if contextily isn't installed or a tile fetch fails.
SHAKEMAP_USE_CONTEXTILY = True

# Pick a tile provider. Both OpenStreetMap (strict automated-usage policy)
# and CartoDB (now requires an API key) have proven unreliable for this
# kind of use. Esri's public basemap tiles work without any API key.
# "WorldStreetMap" shows the actual road network clearly (unlike
# WorldTopoMap, which is terrain/elevation-focused and de-emphasizes
# roads). Other options: "Esri.WorldImagery" (satellite photo).
SHAKEMAP_CONTEXTILY_PROVIDER = "Esri.WorldStreetMap"

# Span (in degrees lat/lon) for the two additional zoomed map outputs.
# Regional map span is calculated automatically from MAX_VALID_DISTANCE_KM.
STREET_MAP_SPAN_DEG = 0.5        # ~55km -- metro-area street-level view
ZOOM_STREET_MAP_SPAN_DEG = 0.03  # ~3.3km -- close-up, building/street scale

# ============================================================================
# NOTIFICATIONS
# ============================================================================

# Magnitude threshold for ALL notification channels below (separate from
# SHAKING_MAGNITUDE_THRESHOLD, so you can be notified about smaller events
# than the ones that trigger a full shaking computation, or vice versa).
NOTIFY_MAGNITUDE_THRESHOLD = 4.0

# Email notifications, via standard SMTP (Python's built-in smtplib --
# no extra dependency). Works with Gmail, Outlook, or any SMTP provider.
# Set NOTIFY_EMAIL = True and fill in the settings below to enable.
#
# For Gmail specifically: you cannot use your normal account password --
# Google requires a 16-character "App Password" instead. Generate one at
# https://myaccount.google.com/apppasswords (requires 2-Step Verification
# to be enabled on the account first; when creating the password, choose
# "Other (Custom name)" as the app).
NOTIFY_EMAIL = True
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587  # 587 = STARTTLS (most providers); 465 = implicit SSL
SMTP_USERNAME = "ytavron@gmail.com"          # e.g. "youraddress@gmail.com"
SMTP_PASSWORD = "atetycqclzjwmqwx"          # app password, NOT your normal account password
EMAIL_FROM = "ytavron@gmail.com"             # usually same as SMTP_USERNAME
EMAIL_TO = "ytavron@gmail.com"               # where alerts get sent -- can be the same address

# Optional webhook notification (Slack or Discord). Leave WEBHOOK_URL as
# None to disable. WEBHOOK_TYPE picks the correct JSON payload shape.
WEBHOOK_URL = None  # e.g. "https://hooks.slack.com/services/..." or a Discord webhook URL
WEBHOOK_TYPE = "slack"  # "slack" or "discord"

# ============================================================================
# EVENT HISTORY (SQLite)
# ============================================================================

# Every detected event (and its shaking estimate, if computed) is logged
# to a local SQLite database, so history survives restarts and can be
# queried/analyzed later without needing the script to be running.
HISTORY_DB_PATH = "earthquake_history.db"
