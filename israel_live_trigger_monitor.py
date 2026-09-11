#!/usr/bin/env python3
"""
israel_live_trigger_monitor.py

Near-real-time earthquake trigger monitor for the Israel Seismic Network (IS)
using ObsPy's FDSN client and STA/LTA coincidence triggering.

HOW IT WORKS
------------
1. Every polling cycle, pulls the most recent N minutes of waveform data
   for a set of stations in network "IS" (served via GFZ/GEOFON, which
   hosts the Geophysical Institute of Israel's data).
2. Runs classic STA/LTA on each station's trace.
3. Uses coincidence_trigger logic: only reports a detection if enough
   stations trigger within the same time window (reduces false positives
   from local noise at a single station).
4. Sleeps, then repeats.

REQUIREMENTS
------------
    pip install obspy

USAGE
-----
    python israel_live_trigger_monitor.py

Adjust STATIONS, POLL_INTERVAL_SEC, WINDOW_MINUTES, and the STA/LTA
thresholds below to tune sensitivity for your use case.

NOTES
-----
- This script needs live internet access to reach the FDSN data center;
  it will not work in a sandboxed/offline environment.
- FDSN data centers typically have some latency (often several minutes)
  between a station recording and that data being queryable — this is
  "near real-time" polling, not a true streaming feed. For true streaming,
  look into ObsPy's SeedLink client (obspy.clients.seedlink) if the data
  center provides a SeedLink server.
- Station codes below are common GII broadband stations as of research in
  2026; verify current availability with the discovery step at the bottom
  of this file (run with --list-stations) since networks add/retire
  stations over time.
"""

import sys
import time
import argparse
from obspy import UTCDateTime
from obspy.clients.fdsn import Client
from obspy.signal.trigger import classic_sta_lta, trigger_onset

# --------------------------------------------------------------------------
# CONFIGURATION - adjust these for your needs
# --------------------------------------------------------------------------

NETWORK = "IS"                  # Israel Seismic Network (GII)
DATA_CENTER = "GFZ"             # GII data is served via GFZ/GEOFON

# A handful of broadband stations spread around Israel, VERIFIED against
# a live get_stations(network="IS") query against GFZ (2026-08). Using
# multiple stations lets us require coincidence (multiple stations
# agreeing) before flagging a real detection, instead of trusting a
# single station.
#
# Note: true SeedLink streaming (obspy.clients.seedlink) currently does
# NOT work against GEOFON's server -- it has migrated to a newer "HMB
# SeedLink v0.2" protocol that ObsPy's classic SeedLink client can't
# negotiate with (confirmed: every station gets "station not accepted",
# regardless of validity). This polling/FDSN approach is the working
# fallback until ObsPy's SeedLink client supports the new protocol.
STATIONS = ["EIL", "JER", "BGIO", "MRNI", "MDBI"]
CHANNEL = "HHZ"                 # high-gain broadband vertical component; all 5 confirmed to have it
LOCATION = "*"

WINDOW_MINUTES = 10             # how much recent data to pull each cycle
POLL_INTERVAL_SEC = 300         # how often to check (5 minutes)

# STA/LTA parameters (seconds)
STA_SECONDS = 1
LTA_SECONDS = 30

# Trigger thresholds
ON_THRESHOLD = 3.5
OFF_THRESHOLD = 0.5

# How many stations must trigger within COINCIDENCE_WINDOW_SEC of each
# other to count as a real detection (vs. single-station local noise)
MIN_STATIONS_COINCIDENT = 2
COINCIDENCE_WINDOW_SEC = 30

# --------------------------------------------------------------------------


def list_available_stations(client):
    """Print all currently available stations in the IS network."""
    print(f"Querying available stations in network '{NETWORK}'...\n")
    inv = client.get_stations(network=NETWORK, station="*", level="channel")
    for net in inv:
        for sta in net:
            channels = sorted(set(cha.code for cha in sta))
            print(f"  {sta.code:<6} lat={sta.latitude:.4f} "
                  f"lon={sta.longitude:.4f} start={sta.start_date} "
                  f"channels={channels}")
    print("\nUpdate the STATIONS list in this script with codes you want "
          "to monitor.")


def get_recent_triggers(client, station, channel, window_minutes):
    """
    Fetch the last `window_minutes` of data for one station and return
    a list of trigger onset times (UTCDateTime), or an empty list if
    no trigger or no data available.
    """
    now = UTCDateTime()
    start = now - window_minutes * 60

    try:
        st = client.get_waveforms(NETWORK, station, LOCATION, channel,
                                  start, now)
    except Exception as e:
        print(f"  [{station}] no data / request failed: {e}")
        return []

    if len(st) == 0:
        print(f"  [{station}] empty stream returned")
        return []

    tr = st[0]
    tr.detrend("demean")
    df = tr.stats.sampling_rate

    sta_samples = max(1, int(STA_SECONDS * df))
    lta_samples = max(sta_samples + 1, int(LTA_SECONDS * df))

    if len(tr.data) < lta_samples * 2:
        print(f"  [{station}] not enough samples for LTA window, skipping")
        return []

    cft = classic_sta_lta(tr.data, sta_samples, lta_samples)
    onsets = trigger_onset(cft, ON_THRESHOLD, OFF_THRESHOLD)

    trigger_times = [tr.stats.starttime + onset[0] / df for onset in onsets]
    return trigger_times


def check_coincidence(all_triggers):
    """
    Given a dict of {station: [trigger_times]}, find groups of triggers
    across different stations that fall within COINCIDENCE_WINDOW_SEC of
    each other. Returns a list of (time, [stations]) coincident groups
    that meet MIN_STATIONS_COINCIDENT.
    """
    # Flatten to (time, station) pairs, sorted by time
    flat = []
    for station, times in all_triggers.items():
        for t in times:
            flat.append((t, station))
    flat.sort(key=lambda x: x[0])

    groups = []
    used = [False] * len(flat)

    for i in range(len(flat)):
        if used[i]:
            continue
        t0, sta0 = flat[i]
        group_times = [t0]
        group_stations = {sta0}
        used[i] = True

        for j in range(i + 1, len(flat)):
            if used[j]:
                continue
            tj, staj = flat[j]
            if tj - t0 <= COINCIDENCE_WINDOW_SEC:
                group_times.append(tj)
                group_stations.add(staj)
                used[j] = True
            else:
                break

        if len(group_stations) >= MIN_STATIONS_COINCIDENT:
            groups.append((t0, sorted(group_stations)))

    return groups


def run_monitor():
    client = Client(DATA_CENTER)
    print(f"Starting live trigger monitor for network '{NETWORK}' "
          f"via {DATA_CENTER}")
    print(f"Stations: {STATIONS}")
    print(f"Polling every {POLL_INTERVAL_SEC}s, checking last "
          f"{WINDOW_MINUTES} min each time")
    print(f"Requiring {MIN_STATIONS_COINCIDENT}+ stations to agree within "
          f"{COINCIDENCE_WINDOW_SEC}s to flag a detection\n")

    try:
        while True:
            now = UTCDateTime()
            print(f"--- Poll at {now} ---")

            all_triggers = {}
            for station in STATIONS:
                triggers = get_recent_triggers(client, station, CHANNEL,
                                               WINDOW_MINUTES)
                if triggers:
                    print(f"  [{station}] {len(triggers)} single-station "
                          f"trigger(s): {triggers}")
                all_triggers[station] = triggers

            coincident = check_coincidence(all_triggers)
            if coincident:
                print("\n  *** POSSIBLE EVENT DETECTED ***")
                for t, stations in coincident:
                    print(f"      time~{t}  stations={stations}")
                print()
            else:
                print("  No coincident detections this cycle.\n")

            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-stations", action="store_true",
                        help="List currently available IS network "
                             "stations and exit (use this first to "
                             "verify/update the STATIONS list above)")
    args = parser.parse_args()

    client = Client(DATA_CENTER)

    if args.list_stations:
        list_available_stations(client)
        sys.exit(0)

    run_monitor()
