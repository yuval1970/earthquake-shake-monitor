#!/usr/bin/env python3
"""
shakemap_tool.py

Two distinct capabilities, both under one script:

  1. --official EVENT_ID
     Pulls USGS's REAL, officially-computed ShakeMap product for a real
     cataloged earthquake (if USGS generated one for it -- they only do
     this for significant events, mostly M4.5+ or felt/damaging events).
     This is the authoritative, scientifically-produced shaking map --
     not an approximation.

  2. --estimate --lat LAT --lon LON --mag M [--depth KM]
     Generates a DIY, simplified estimated shaking-intensity grid around
     ANY hypothetical epicenter (real or made up), using a basic
     empirical attenuation relation. This lets you explore "what if a
     M6 happened here" for events that don't exist / weren't catalogued,
     but it is NOT a scientifically rigorous GMPE and should not be
     treated as an authoritative hazard estimate -- see the caveat in
     estimate_shaking() below.

REQUIREMENTS
------------
    pip install requests matplotlib numpy

USAGE
-----
    # Find recent significant (M6+) events with real ShakeMaps available
    python shakemap_tool.py --list-recent

    # Fetch and save the official ShakeMap images for a specific event
    python shakemap_tool.py --official us7000abcd

    # Generate a DIY estimated shaking map for a hypothetical M6 at
    # a given location, and see the estimated intensity at one or more
    # target cities
    python shakemap_tool.py --estimate --lat 34.0 --lon -118.2 --mag 6.5 \\
        --depth 10 --targets "Los Angeles:34.05:-118.24" "San Diego:32.72:-117.16"
"""

import argparse
import math
import os
import requests
import numpy as np
import matplotlib.pyplot as plt

USGS_SUMMARY_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson"
USGS_DETAIL_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/detail/{event_id}.geojson"

OUTPUT_DIR = "shakemap_output"


# ============================================================================
# PART 1: Official USGS ShakeMap fetching
# ============================================================================

def list_recent_significant_events():
    """List recent M-significant events, flagging which ones have a real
    ShakeMap product available, so you can pick a valid EVENT_ID."""
    print("Fetching recent significant events from USGS...\n")
    resp = requests.get(USGS_SUMMARY_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    for feature in data.get("features", []):
        event_id = feature.get("id")
        props = feature.get("properties", {})
        mag = props.get("mag")
        place = props.get("place")

        has_shakemap = "shakemap" in (props.get("types") or "")
        flag = "[ShakeMap available]" if has_shakemap else "[no ShakeMap]"
        print(f"  {event_id:<20} M{mag:<5} {place}  {flag}")

    print("\nUse: python shakemap_tool.py --official EVENT_ID")


def fetch_official_shakemap(event_id):
    print(f"Fetching event detail for '{event_id}'...")
    url = USGS_DETAIL_URL.format(event_id=event_id)
    resp = requests.get(url, timeout=15)

    if resp.status_code == 404:
        print(f"Event ID '{event_id}' not found. Try --list-recent to "
              f"find a valid ID.")
        return

    resp.raise_for_status()
    detail = resp.json()

    props = detail.get("properties", {})
    products = props.get("products", {})
    shakemap_list = products.get("shakemap")

    if not shakemap_list:
        print(f"No ShakeMap product available for this event. USGS only "
              f"generates ShakeMaps for significant events (typically "
              f"M4.5+ or notably felt/damaging ones).")
        return

    shakemap = shakemap_list[0]  # most recent version
    contents = shakemap.get("contents", {})

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"\nEvent: M{props.get('mag')} {props.get('place')}")
    print(f"Found ShakeMap with {len(contents)} associated file(s). "
          f"Downloading images...\n")

    downloaded = []
    for key, info in contents.items():
        if key.lower().endswith((".jpg", ".png")):
            file_url = info.get("url")
            if not file_url:
                continue
            fname = os.path.join(OUTPUT_DIR, f"{event_id}_{os.path.basename(key)}")
            try:
                img_resp = requests.get(file_url, timeout=30)
                img_resp.raise_for_status()
                with open(fname, "wb") as f:
                    f.write(img_resp.content)
                downloaded.append(fname)
                print(f"  Saved: {fname}")
            except requests.RequestException as e:
                print(f"  Failed to download {key}: {e}")

    if downloaded:
        print(f"\n{len(downloaded)} image(s) saved to '{OUTPUT_DIR}/'.")
    else:
        print("\nNo image files found in this ShakeMap product -- it may "
              "only contain data grids (grid.xml) rather than rendered "
              "images. Check the raw product contents if you need the "
              "underlying data instead of pictures.")


# ============================================================================
# PART 2: DIY simplified shaking-intensity estimate
# ============================================================================

def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two lat/lon points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def estimate_pga(magnitude, distance_km, depth_km):
    """
    Very simplified empirical attenuation relation, loosely in the
    style of common PGA GMPEs (e.g. Boore-type functional form), with
    generic illustrative coefficients.

    *** IMPORTANT CAVEAT ***
    This is NOT a validated, region-specific, peer-reviewed GMPE. Real
    ground motion depends heavily on local geology, fault mechanism,
    rupture directivity, and site amplification (soil vs. bedrock),
    NONE of which this simple formula accounts for. Treat this purely
    as an illustrative/educational approximation -- for anything real
    (engineering, planning, risk assessment), use USGS's actual
    ShakeMap/PAGER products (Part 1 above) or a proper hazard engine
    like OpenQuake with a region-appropriate GMPE.

    Returns estimated PGA in %g (percent of gravity).
    """
    r_hyp = math.sqrt(distance_km ** 2 + depth_km ** 2)  # hypocentral distance
    r_eff = max(r_hyp, 1.0)  # avoid log(0) right at the epicenter

    # Illustrative coefficients only -- NOT from a specific published study
    log_pga = -1.5 + 0.5 * magnitude - 1.2 * math.log10(r_eff) - 0.002 * r_eff
    pga_g = 10 ** log_pga
    return pga_g * 100  # convert to %g


def pga_to_mmi(pga_percent_g):
    """
    Rough conversion from PGA to Modified Mercalli Intensity, based on
    commonly cited approximate correspondence tables (illustrative,
    not a precise scientific conversion -- real PGA-to-MMI relationships
    are regionally calibrated and have significant scatter).
    """
    thresholds = [
        (0.05, 1), (0.3, 2), (2.8, 4), (6.2, 5),
        (12, 6), (22, 7), (40, 8), (75, 9), (139, 10),
    ]
    mmi = 1
    for pga_thresh, mmi_val in thresholds:
        if pga_percent_g >= pga_thresh:
            mmi = mmi_val
    return mmi


def generate_estimate(lat, lon, magnitude, depth_km, targets):
    print(f"Generating DIY shaking estimate for M{magnitude} at "
          f"({lat}, {lon}), depth {depth_km}km")
    print("*** This is a simplified illustrative estimate, NOT a "
          "validated GMPE. See caveat in estimate_pga(). ***\n")

    # Build a grid around the epicenter
    span_deg = 3.0  # roughly a few hundred km, adjust for bigger/smaller M
    grid_n = 100
    lats = np.linspace(lat - span_deg, lat + span_deg, grid_n)
    lons = np.linspace(lon - span_deg, lon + span_deg, grid_n)
    pga_grid = np.zeros((grid_n, grid_n))

    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            dist = haversine_km(lat, lon, la, lo)
            pga_grid[i, j] = estimate_pga(magnitude, dist, depth_km)

    fig, ax = plt.subplots(figsize=(8, 7))
    contour = ax.contourf(lons, lats, pga_grid, levels=20, cmap="YlOrRd")
    plt.colorbar(contour, label="Estimated PGA (%g)")
    ax.plot(lon, lat, "k*", markersize=20, label="Epicenter")

    for name, tlat, tlon in targets:
        dist = haversine_km(lat, lon, tlat, tlon)
        pga = estimate_pga(magnitude, dist, depth_km)
        mmi = pga_to_mmi(pga)
        ax.plot(tlon, tlat, "bo", markersize=8)
        ax.annotate(f"{name}\n{dist:.0f}km, PGA~{pga:.1f}%g, MMI~{mmi}",
                   (tlon, tlat), textcoords="offset points", xytext=(8, 8),
                   fontsize=8, color="blue")
        print(f"  {name}: {dist:.0f} km away -> "
              f"estimated PGA ~{pga:.2f}%g, MMI ~{mmi}")

    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(f"DIY Estimated Shaking -- M{magnitude} "
                f"(illustrative only, not a real GMPE)")
    ax.legend()
    plt.tight_layout()
    plt.show()


def parse_target(s):
    """Parse 'Name:lat:lon' strings from the command line."""
    parts = s.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"Target must be 'Name:lat:lon', got: {s}")
    name, lat, lon = parts
    return (name, float(lat), float(lon))


# ============================================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list-recent", action="store_true",
                        help="List recent significant events and whether "
                             "they have a real ShakeMap available")
    parser.add_argument("--official", metavar="EVENT_ID",
                        help="Download the real USGS ShakeMap for this "
                             "event ID")
    parser.add_argument("--estimate", action="store_true",
                        help="Generate a DIY simplified shaking estimate "
                             "instead of using real USGS data")
    parser.add_argument("--lat", type=float, help="Epicenter latitude "
                                                   "(for --estimate)")
    parser.add_argument("--lon", type=float, help="Epicenter longitude "
                                                   "(for --estimate)")
    parser.add_argument("--mag", type=float, help="Magnitude "
                                                   "(for --estimate)")
    parser.add_argument("--depth", type=float, default=10.0,
                        help="Depth in km (for --estimate, default 10)")
    parser.add_argument("--targets", nargs="*", type=parse_target, default=[],
                        help="Target locations as 'Name:lat:lon', space "
                             "separated (for --estimate)")

    args = parser.parse_args()

    if args.list_recent:
        list_recent_significant_events()
    elif args.official:
        fetch_official_shakemap(args.official)
    elif args.estimate:
        if args.lat is None or args.lon is None or args.mag is None:
            parser.error("--estimate requires --lat, --lon, and --mag")
        generate_estimate(args.lat, args.lon, args.mag, args.depth, args.targets)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
