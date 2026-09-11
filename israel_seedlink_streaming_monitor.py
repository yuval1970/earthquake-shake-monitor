#!/usr/bin/env python3
"""
israel_seedlink_streaming_monitor.py

TRUE real-time earthquake trigger monitor for the Israel Seismic Network (IS)
using ObsPy's SeedLink client (as opposed to the FDSN-polling version, this
one holds an open streaming connection and processes packets as they arrive).

HOW IT WORKS
------------
1. Opens a persistent SeedLink connection to a real-time data server (GEOFON,
   which mirrors many EIDA-affiliated networks including IS).
2. As each new packet (~a few seconds of data) arrives for a subscribed
   station/channel, it's appended to a rolling in-memory buffer for that
   station.
3. On every new packet, STA/LTA is recomputed on that station's buffer.
4. A background thread periodically checks all stations' recent triggers
   for coincidence (multiple stations agreeing within a short window)
   before reporting a likely real event.

REQUIREMENTS
------------
    pip install obspy

BEFORE YOU RUN THIS: VERIFY THE STREAM EXISTS
-----------------------------------------------
Not every network archived at a data center is also pushed to its
real-time SeedLink feed. GII/IS data is served via GFZ/GEOFON's FDSN
archive, but real-time SeedLink availability for network "IS" specifically
needs to be confirmed. Check with:

    python israel_seedlink_streaming_monitor.py --list-streams

This subscribes to the configured stations and listens for ~45s to see if
any real packets arrive -- a practical (if indirect) way to confirm
whether the stream is live here, without needing any extra tools beyond
ObsPy itself.

If nothing arrives, it likely means GII isn't contributing real-time
streams to this particular server (only archived data), and you'd need
to either find whether GII/ISN operates its own SeedLink server, or fall
back to the polling (FDSN) version of this monitor instead.

USAGE
-----
    python israel_seedlink_streaming_monitor.py --list-streams   # check availability first
    python israel_seedlink_streaming_monitor.py                  # start streaming monitor
"""

import sys
import time
import argparse
import threading
from collections import defaultdict

from obspy import UTCDateTime, Stream
from obspy.clients.seedlink.easyseedlink import create_client
from obspy.signal.trigger import classic_sta_lta, trigger_onset

# --------------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------------

SEEDLINK_SERVER = "geofon.gfz.de:18000"   # GEOFON real-time SeedLink server

NETWORK = "IS"
STATIONS = ["EIL", "KFSB", "MTLA", "JEM"]   # update after --list-streams check
#STATIONS =['BHE', 'BHN', 'BHZ', 'HHE', 'HHN', 'HHZ', 'LHE', 'LHN', 'LHZ', 'SHE', 'SHN', 'SHZ', 'VHE', 'VHN', 'VHZ']
CHANNEL = "HHZ"

# How much buffered history to keep per station for STA/LTA (seconds)
BUFFER_SECONDS = 300  # 5 minutes

# STA/LTA parameters (seconds)
STA_SECONDS = 1
LTA_SECONDS = 30

# Trigger thresholds
ON_THRESHOLD = 3.5
OFF_THRESHOLD = 0.5

# Coincidence logic
MIN_STATIONS_COINCIDENT = 2
COINCIDENCE_WINDOW_SEC = 30
COINCIDENCE_CHECK_INTERVAL_SEC = 10   # how often the checker thread runs

# --------------------------------------------------------------------------

# Shared state, protected by a lock since packets arrive on the SeedLink
# client's own thread while the coincidence checker runs on another.
_lock = threading.Lock()
_buffers = defaultdict(Stream)          # station -> Stream (rolling buffer)
_recent_triggers = defaultdict(list)    # station -> [UTCDateTime, ...]


LISTEN_SECONDS = 45  # how long to wait for at least one packet in --list-streams


def list_streams():
    """
    Practical availability check: subscribe to the configured
    NETWORK/STATIONS/CHANNEL and listen for a short window. If any real
    data packets arrive, the stream is live here. If nothing arrives, it
    likely means either these station codes aren't pushed to this
    SeedLink server in real time, or they happen to be quiet/down right
    now (try increasing LISTEN_SECONDS, or try a well-known-active
    network like "GE" station "MALT" as a sanity check that the server
    connection itself works).
    """
    print(f"Connecting to {SEEDLINK_SERVER} and subscribing to "
          f"{NETWORK}.{STATIONS}.{CHANNEL} ...")
    print(f"Listening for up to {LISTEN_SECONDS}s for any real packets...\n")

    received = []

    def on_data(trace):
        received.append(trace)
        print(f"  Got packet: {trace}")

    try:
        client = create_client(SEEDLINK_SERVER, on_data=on_data)
        for station in STATIONS:
            client.select_stream(NETWORK, station, CHANNEL)
    except Exception as e:
        print(f"Failed to connect or subscribe: {e}")
        return

    runner = threading.Thread(target=client.run, daemon=True)
    runner.start()
    runner.join(timeout=LISTEN_SECONDS)

    print()
    if received:
        print(f"Received {len(received)} packet(s) -- network '{NETWORK}' "
              f"IS live-streaming on this server for at least one of "
              f"{STATIONS}.")
    else:
        print(f"No packets received in {LISTEN_SECONDS}s.")
        print("This could mean:")
        print(f"  - '{NETWORK}' isn't pushed to this SeedLink server in "
              f"real time (only archived/FDSN), or")
        print(f"  - these specific station codes are wrong/inactive, or")
        print(f"  - the stations are just quiet right now (unlikely to "
              f"matter -- SeedLink usually sends *something* periodically)")
        print("\nSanity check: try a known-active GEOFON station to "
              "confirm the server connection itself works, e.g. "
              "temporarily set NETWORK='GE', STATIONS=['MALT'].")


def trim_buffer(station):
    """Keep only the last BUFFER_SECONDS of data for a station's stream."""
    st = _buffers[station]
    if len(st) == 0:
        return
    st.merge(method=1, fill_value="interpolate")
    end = max(tr.stats.endtime for tr in st)
    start = end - BUFFER_SECONDS
    st.trim(starttime=start, endtime=end)
    _buffers[station] = st


def run_sta_lta_on_buffer(station):
    """Run STA/LTA on a station's current buffer and record any new
    trigger onset times."""
    st = _buffers[station]
    if len(st) == 0:
        return

    tr = st[0].copy()
    tr.detrend("demean")
    df = tr.stats.sampling_rate

    sta_samples = max(1, int(STA_SECONDS * df))
    lta_samples = max(sta_samples + 1, int(LTA_SECONDS * df))

    if len(tr.data) < lta_samples * 2:
        return  # not enough data yet to compute a meaningful LTA

    cft = classic_sta_lta(tr.data, sta_samples, lta_samples)
    onsets = trigger_onset(cft, ON_THRESHOLD, OFF_THRESHOLD)

    for onset in onsets:
        t = tr.stats.starttime + onset[0] / df
        # Avoid re-logging the same trigger repeatedly as the buffer slides
        if not any(abs(t - existing) < 5 for existing in _recent_triggers[station]):
            _recent_triggers[station].append(t)
            print(f"  [{station}] single-station trigger at {t}")


def make_data_handler():
    """Returns the on_data callback used by the SeedLink client. Called
    once per incoming packet (roughly every few seconds per station)."""
    def on_data(trace):
        station = trace.stats.station
        with _lock:
            _buffers[station] += trace
            trim_buffer(station)
            run_sta_lta_on_buffer(station)
    return on_data


def coincidence_checker_loop():
    """Background thread: periodically scan _recent_triggers across all
    stations for coincident detections and report them."""
    while True:
        time.sleep(COINCIDENCE_CHECK_INTERVAL_SEC)
        with _lock:
            now = UTCDateTime()
            # Drop triggers older than the coincidence window many cycles ago
            for station in list(_recent_triggers.keys()):
                _recent_triggers[station] = [
                    t for t in _recent_triggers[station]
                    if now - t < BUFFER_SECONDS
                ]

            flat = []
            for station, times in _recent_triggers.items():
                for t in times:
                    flat.append((t, station))
            flat.sort(key=lambda x: x[0])

            i = 0
            while i < len(flat):
                t0, sta0 = flat[i]
                group_stations = {sta0}
                j = i + 1
                while j < len(flat) and flat[j][0] - t0 <= COINCIDENCE_WINDOW_SEC:
                    group_stations.add(flat[j][1])
                    j += 1

                if len(group_stations) >= MIN_STATIONS_COINCIDENT:
                    print(f"\n  *** POSSIBLE EVENT DETECTED at ~{t0} "
                          f"(stations: {sorted(group_stations)}) ***\n")
                    # Clear those triggers so we don't re-report every cycle
                    for station in group_stations:
                        _recent_triggers[station] = [
                            t for t in _recent_triggers[station] if t != t0
                        ]
                i = j if j > i else i + 1


def run_monitor():
    print(f"Connecting to SeedLink server {SEEDLINK_SERVER} ...")
    print(f"Subscribing to: network={NETWORK}, "
          f"stations={STATIONS}, channel={CHANNEL}\n")

    client = create_client(SEEDLINK_SERVER, on_data=make_data_handler())

    for station in STATIONS:
        client.select_stream(NETWORK, station, CHANNEL)

    checker_thread = threading.Thread(target=coincidence_checker_loop,
                                      daemon=True)
    checker_thread.start()

    print("Streaming... press Ctrl+C to stop.\n")
    try:
        client.run()  # blocks, processes packets forever
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-streams", action="store_true",
                        help="Query the SeedLink server for available "
                             "streams matching the configured NETWORK, "
                             "then exit. Run this first to confirm 'IS' "
                             "is actually streamed in real time here.")
    args = parser.parse_args()

    if args.list_streams:
        list_streams()
        sys.exit(0)

    run_monitor()
