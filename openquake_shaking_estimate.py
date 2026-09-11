#!/usr/bin/env python3
"""
openquake_shaking_estimate.py

Scientifically rigorous ground-motion estimation using openquake.hazardlib
-- GEM Foundation's open-source seismic hazard library, which implements
real, published, peer-reviewed GMPEs (Ground Motion Prediction Equations)
used in actual seismic hazard practice worldwide.

This replaces the illustrative/made-up formula in shakemap_tool.py's
--estimate mode with a real, citable scientific model.

WHAT THIS USES
--------------
Boore, Stewart, Seyhan & Atkinson (2014) NGA-West2 GMPE ("BSSA14") -- a
widely used, well-validated model for shallow crustal earthquakes,
commonly applied in the western US and adaptable elsewhere for shallow
crustal settings. There are MANY other GMPEs in hazardlib appropriate
for different tectonic settings (subduction zones, stable continental
regions, etc.) -- BSSA14 is just a solid, well-known default to start
with.

IMPORTANT: pick a GMPE appropriate to your region's tectonic setting for
real work. hazardlib includes dozens; see:
https://docs.openquake.org/oq-engine/master/manual/openquake-tools/hazardlib/gsim/index.html

REQUIREMENTS
------------
    pip install openquake.hazardlib numpy matplotlib

NOTE ON API STABILITY
----------------------
openquake.hazardlib's context-building API has changed across versions
(older versions use separate SitesContext/RuptureContext/DistancesContext
objects; newer versions use a unified numpy recarray via ContextMaker).
This script targets the newer unified-context style. If you get an
AttributeError or TypeError related to contexts, tell me the exact error
and your installed version (`pip show openquake.hazardlib`) and I'll
adjust to match -- I have not been able to test-execute this against a
live install in this environment, and hazardlib's API is not something I
should claim certainty about without verifying against your actual
version.

USAGE
-----
    python openquake_shaking_estimate.py --mag 6.5 --depth 10 \\
        --distances 10 30 50 100 --vs30 400
"""

import argparse
import numpy as np


def compute_ground_motion(magnitude, depth_km, distances_km, vs30, rake=0.0, dip=90.0):
    """
    Compute expected PGA (in g) at each given distance from a rupture of
    the specified magnitude, using the BSSA14 GMPE.

    Parameters
    ----------
    magnitude : float
        Moment magnitude.
    depth_km : float
        Depth to top of rupture (simplified as hypocentral depth here).
    distances_km : list of float
        Joyner-Boore distances (roughly, horizontal distance to surface
        projection of the rupture) to compute shaking at.
    vs30 : float
        Average shear-wave velocity in top 30m of soil, m/s. Lower values
        = softer soil = more amplification. ~760 m/s is often used as a
        generic "rock" reference site; ~180-300 m/s represents soft soil.
    rake : float
        Fault rake angle in degrees (0 = strike-slip, 90 = reverse,
        -90 = normal). Affects radiated energy pattern.
    dip : float
        Fault dip angle in degrees.

    Returns
    -------
    dict with 'distances_km', 'pga_g', 'pga_percent_g'
    """
    try:
        from openquake.hazardlib.gsim.boore_2014 import BooreEtAl2014
        from openquake.hazardlib.imt import PGA
    except ImportError as e:
        raise ImportError(
            "openquake.hazardlib not installed or import path differs "
            "from expected. Run: pip install openquake.hazardlib\n"
            f"Original error: {e}"
        )

    gmpe = BooreEtAl2014()
    n = len(distances_km)

    # Build a recarray context with exactly the fields this GSIM declares
    # it needs (gmpe.REQUIRES_SITES_PARAMETERS / _RUPTURE_PARAMETERS /
    # _DISTANCES), then call compute() directly. This is the current
    # (post get_mean_and_stddevs) hazardlib GMPE calling convention: no
    # more separate SitesContext/RuptureContext/DistancesContext objects
    # or a get_mean_and_stddevs() method -- GMPE.compute(ctx, imts, mean,
    # sig, tau, phi) fills the output arrays in place.
    dtype = [("mag", float), ("rake", float), ("vs30", float),
             ("rjb", float), ("sids", int)]
    ctx = np.recarray(n, dtype=dtype)
    ctx.mag = magnitude
    ctx.rake = rake
    ctx.vs30 = np.full(n, vs30, dtype=float)
    ctx.rjb = np.array(distances_km, dtype=float)
    ctx.sids = np.arange(n)

    imts = [PGA()]
    mean = np.zeros((len(imts), n))
    sig = np.zeros((len(imts), n))
    tau = np.zeros((len(imts), n))
    phi = np.zeros((len(imts), n))

    gmpe.compute(ctx, imts, mean, sig, tau, phi)

    # hazardlib GMPEs return ln(PGA) in g; convert to g then %g
    pga_g = np.exp(mean[0])
    pga_percent_g = pga_g * 100

    return {
        "distances_km": list(distances_km),
        "pga_g": pga_g.tolist(),
        "pga_percent_g": pga_percent_g.tolist(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mag", type=float, required=True)
    parser.add_argument("--depth", type=float, default=10.0,
                        help="Depth to top of rupture, km (default 10)")
    parser.add_argument("--distances", type=float, nargs="+", required=True,
                        help="One or more Joyner-Boore distances in km")
    parser.add_argument("--vs30", type=float, default=400.0,
                        help="Site Vs30 in m/s (default 400 = generic "
                             "soil; try 760 for rock, 200 for soft soil)")
    args = parser.parse_args()

    print(f"Computing BSSA14 ground motion for M{args.mag}, "
          f"depth {args.depth}km, Vs30={args.vs30} m/s\n")

    try:
        result = compute_ground_motion(args.mag, args.depth,
                                       args.distances, args.vs30)
    except ImportError as e:
        print(str(e))
        return
    except Exception as e:
        print(f"Computation failed: {type(e).__name__}: {e}")
        print("\nThis is likely an API mismatch with your installed "
              "hazardlib version -- see the API NOTE in this file's "
              "docstring. Share this error and your version "
              "(pip show openquake.hazardlib) and it can be fixed.")
        return

    print(f"{'Distance (km)':>15} {'PGA (%g)':>12}")
    for dist, pga in zip(result["distances_km"], result["pga_percent_g"]):
        print(f"{dist:>15.1f} {pga:>12.3f}")


if __name__ == "__main__":
    main()
