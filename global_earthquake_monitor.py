#!/usr/bin/env python3
"""
global_earthquake_monitor.py

Combined real-time-ish global earthquake monitor using two independent,
free, public catalog sources:

  1. EMSC (European-Mediterranean Seismological Centre) -- real-time
     WebSocket feed. Push-based: the server sends a message the moment
     a new event is published. This is the fastest source here.

  2. USGS (U.S. Geological Survey) -- polled GeoJSON feed, refreshed on
     their end roughly every ~60 seconds. Global coverage, independent
     detection/processing pipeline from EMSC.

Both sources are queried simultaneously (EMSC via a background thread
holding a persistent WebSocket connection, USGS via periodic polling on
the main thread). Because the *same* real earthquake is often reported
by both agencies -- with different event IDs, and often with slightly
different magnitude/location estimates -- this script deduplicates by
matching on approximate time + location + magnitude, so you get ONE
alert per real event rather than two.

WHY THIS APPROACH (vs raw-waveform STA/LTA on open FDSN networks)
-------------------------------------------------------------------
Global backbone/open networks (GE, IU, II, etc.) are often hundreds of
km apart, and FDSN archives typically have several minutes of latency
before very recent data becomes queryable (we confirmed this directly
earlier in this project). EMSC/USGS catalogs aggregate detections from
many countries' own dense national networks, so you indirectly benefit
from much better station density than you could reasonably assemble
yourself, with genuinely faster realistic latency for most regions.

THIS IS DETECTION, NOT PREDICTION
------------------------------------
Both sources report earthquakes that have already started/occurred.
Nothing here (or anywhere) predicts an earthquake before it happens.

REQUIREMENTS
------------
    pip install websocket-client requests

USAGE
-----
    python global_earthquake_monitor.py

Optional filters (edit the CONFIGURATION section below):
    MIN_MAGNITUDE   -- ignore smaller events
    BBOX            -- restrict to a geographic region (None = global)
"""

import json
import time
import threading
from datetime import datetime, timezone

import requests
import websocket

# --------------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------------

EMSC_WS_URL = "wss://www.seismicportal.eu/standing_order/websocket"
USGS_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
USGS_POLL_INTERVAL_SEC = 60

MIN_MAGNITUDE = 0.0     # set higher (e.g. 3.0) to reduce noise
BBOX = None              # None = global. Or (minlat, maxlat, minlon, maxlon)

# Deduplication thresholds: two reports are considered the SAME event if
# they're within all of these tolerances of each other.
DEDUP_TIME_SEC = 90
DEDUP_DIST_DEG = 1.0     # roughly ~100km at the equator; coarse but simple
DEDUP_MAG_DELTA = 1.0    # magnitude estimates can vary notably between agencies

# --------------------------------------------------------------------------

_lock = threading.Lock()
_known_events = []   # list of dicts: {time, lat, lon, mag, place, sources:set()}


def in_bbox(lat, lon):
    if BBOX is None:
        return True
    minlat, maxlat, minlon, maxlon = BBOX
    return minlat <= lat <= maxlat and minlon <= lon <= maxlon


def find_matching_event(t, lat, lon, mag):
    """Look for an already-known event that this report likely matches."""
    for ev in _known_events:
        if abs(ev["time"] - t) > DEDUP_TIME_SEC:
            continue
        if abs(ev["lat"] - lat) > DEDUP_DIST_DEG or abs(ev["lon"] - lon) > DEDUP_DIST_DEG:
            continue
        if mag is not None and ev["mag"] is not None and abs(ev["mag"] - mag) > DEDUP_MAG_DELTA:
            continue
        return ev
    return None


def report_event(source, t, lat, lon, mag, place):
    """Called by both the EMSC and USGS handlers whenever they see an
    event. Deduplicates against already-known events and prints an
    alert only for genuinely new ones; otherwise just notes the extra
    confirming source."""
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
            return  # already alerted on this event

        _known_events.append({
            "time": t, "lat": lat, "lon": lon, "mag": mag,
            "place": place, "sources": {source},
        })

    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    print(f"\n  *** NEW EVENT ({source}) ***")
    print(f"      M{mag}  {place}")
    print(f"      Time: {dt.isoformat()}")
    print(f"      Location: {lat:.4f}, {lon:.4f}\n")


# --------------------------------------------------------------------------
# EMSC WebSocket handler
# --------------------------------------------------------------------------

def emsc_on_message(ws, message):
    try:
        payload = json.loads(message)
        data = payload.get("data", {}).get("properties", payload.get("data", {}))
        # EMSC's websocket message shape has varied historically; handle
        # both a flat dict and a nested "properties" dict defensively.
        lat = data.get("lat")
        lon = data.get("lon")
        mag = data.get("mag")
        place = data.get("flynn_region") or data.get("region")
        time_str = data.get("time")

        if lat is None or lon is None or time_str is None:
            return

        t = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S.%fZ") \
            .replace(tzinfo=timezone.utc).timestamp()

        report_event("EMSC", t, lat, lon, mag, place)
    except Exception as e:
        print(f"  [EMSC] failed to parse message: {e}")


def emsc_on_error(ws, error):
    print(f"  [EMSC] connection error: {error}")


def emsc_on_close(ws, close_status_code, close_msg):
    print("  [EMSC] connection closed")


def emsc_on_open(ws):
    print("  [EMSC] connected, listening for real-time events...")


def run_emsc_listener():
    """Runs forever in a background thread, reconnecting on failure."""
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


# --------------------------------------------------------------------------
# USGS polling
# --------------------------------------------------------------------------

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
        lon, lat, _depth = coords
        mag = props.get("mag")
        place = props.get("place")
        time_ms = props.get("time")

        if lat is None or lon is None or time_ms is None:
            continue

        t = time_ms / 1000
        report_event("USGS", t, lat, lon, mag, place)


def run_usgs_poller():
    while True:
        poll_usgs()
        time.sleep(USGS_POLL_INTERVAL_SEC)


# --------------------------------------------------------------------------

def main():
    print("Starting combined global earthquake monitor (EMSC + USGS)")
    print(f"Min magnitude: {MIN_MAGNITUDE}")
    print(f"Region filter: {'global' if BBOX is None else BBOX}\n")
    print("NOTE: this reports earthquakes that have already occurred. "
          "It cannot predict earthquakes before they happen.\n")

    emsc_thread = threading.Thread(target=run_emsc_listener, daemon=True)
    emsc_thread.start()

    try:
        run_usgs_poller()  # blocks on main thread
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
