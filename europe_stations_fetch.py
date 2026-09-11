#!/usr/bin/env python3
"""
europe_stations_fetch.py

Discover seismic stations across Europe and fetch waveform data for them,
using ObsPy's FDSN client.

WHY THIS SHOULD ACTUALLY WORK (unlike the Israel/IS case)
-------------------------------------------------------------
Earlier in this project we confirmed that GFZ/GEOFON's dataselect service
only serves waveform data for networks it actually archives itself (not
just mirrors metadata for). Networks like "GE" (GEOFON's own global
network, with many European stations) and other well-established European
networks (e.g. "GR" - Germany, "CH" - Switzerland via ETH, "FR" - France
via RESIF, "II"/"IU" - global backbone networks with European stations)
ARE genuinely archived at their home data centers and do have public
waveform data available. This script defaults to "GE" for that reason.

REQUIREMENTS
------------
    pip install obspy

USAGE
-----
    python europe_stations_fetch.py --list-stations
        -> discover stations in the configured network + region

    python europe_stations_fetch.py --fetch STATION_CODE
        -> download and plot recent waveform data for one station

    python europe_stations_fetch.py --fetch STATION_CODE --hours-ago 24
        -> fetch a window starting 24 hours ago instead of "now"
"""

import argparse
from obspy.clients.fdsn import Client
from obspy import UTCDateTime

# --------------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------------

DATA_CENTER = "GFZ"        # GFZ archives GE directly (confirmed working)
NETWORK = "GE"              # GEOFON's own global network, many EU stations
CHANNEL = "BH*"              # broadband vertical; GE mostly uses BH, not HH

# Rough bounding box covering continental Europe
# minlatitude, maxlatitude, minlongitude, maxlongitude
EUROPE_BBOX = dict(minlatitude=35.0, maxlatitude=71.0,
                   minlongitude=-25.0, maxlongitude=40.0)

WINDOW_SECONDS = 3600  # 1 hour of data per fetch by default

# --------------------------------------------------------------------------


def list_stations(client):
    """List stations in NETWORK that fall within the Europe bounding box."""
    print(f"Querying stations in network '{NETWORK}' within Europe...\n")
    inv = client.get_stations(network=NETWORK, station="*",
                              level="channel", **EUROPE_BBOX)

    if len(inv) == 0:
        print("No stations found. Try widening EUROPE_BBOX or changing "
              "NETWORK.")
        return

    for net in inv:
        for sta in net:
            channels = sorted(set(c.code for c in sta))
            print(f"  {sta.code:<6} lat={sta.latitude:>8.4f} "
                  f"lon={sta.longitude:>9.4f} "
                  f"elev={sta.elevation:>6.1f}m "
                  f"channels={channels}")

    print(f"\n{sum(len(net) for net in inv)} station(s) found. "
          f"Use --fetch STATION_CODE to pull waveform data for one.")


def fetch_and_plot(client, station, hours_ago):
    """Fetch and plot a window of waveform data for one station."""
    end = UTCDateTime() - hours_ago * 3600 if hours_ago else UTCDateTime()
    start = end - WINDOW_SECONDS

    print(f"Fetching {NETWORK}.{station}.*.{CHANNEL} "
          f"from {start} to {end} ...")

    try:
        st = client.get_waveforms(NETWORK, station, "*", CHANNEL, start, end)
    except Exception as e:
        print(f"Request failed: {e}")
        print("If this is a 204/no-data error, try a different "
              "--hours-ago value (data may not be available for the "
              "most recent minutes yet), or a different station.")
        return

    print(f"\nGot {len(st)} trace(s):")
    print(st)

    st.plot()

    out_path = f"{NETWORK}_{station}_{CHANNEL}.mseed"
    st.write(out_path, format="MSEED")
    print(f"\nSaved to {out_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-stations", action="store_true",
                        help="List available stations in the configured "
                             "network within the Europe bounding box")
    parser.add_argument("--fetch", metavar="STATION_CODE",
                        help="Fetch and plot recent waveform data for "
                             "this station code")
    parser.add_argument("--hours-ago", type=float, default=1.0,
                        help="How many hours back to end the fetch "
                             "window (default: 1.0, i.e. up to ~1 hour "
                             "ago rather than exactly now, to avoid "
                             "requesting data that hasn't propagated "
                             "to the archive yet)")
    args = parser.parse_args()

    client = Client(DATA_CENTER)

    if args.list_stations:
        list_stations(client)
    elif args.fetch:
        fetch_and_plot(client, args.fetch, args.hours_ago)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
