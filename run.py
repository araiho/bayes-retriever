"""End-to-end pipeline.

    python3 run.py cases/maisy.json          # case-file driven (preferred)
    python3 run.py LAT LON [temperament]     # quick one-off, no case file

Outputs land in output/<case-slug>/: search_map.html + index.html (the public
site page) + .png + priority.tif + GPX/CSV waypoints + caltopo_import.json.
"""
import os
import sys
import time

import numpy as np
import rasterio.features
from rasterio.warp import transform as warp_transform

import casefile
import config
import diffusion
import fetch
import layers
import make_map
import make_site
import score as scoring

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def build_masks(osm, profile):
    shape = (profile["height"], profile["width"])
    t = profile["transform"]
    masks = {}
    for cat, geoms in osm.items():
        if geoms:
            masks[cat] = rasterio.features.rasterize(
                [(g, 1) for g in geoms], out_shape=shape, transform=t,
                fill=0, all_touched=True, dtype="uint8").astype(bool)
        else:
            masks[cat] = np.zeros(shape, dtype=bool)
    return masks


def _latlon_to_rc(lat, lon, profile):
    t = profile["transform"]
    xs, ys = warp_transform("EPSG:4326", config.UTM_CRS, [lon], [lat])
    return int((ys[0] - t.f) / t.e), int((xs[0] - t.c) / t.a)


def main(case):
    t0 = time.time()
    slug = case.get("slug", "adhoc")
    fetch.DATA_DIR = os.path.join(BASE_DIR, "data", slug)
    make_map.OUT_DIR = os.path.join(BASE_DIR, "output", slug)
    lat, lon = case["last_seen"]["lat"], case["last_seen"]["lon"]
    bearing = case["last_seen"].get("bearing_deg")
    config.apply_temperament(case.get("temperament"), case.get("ring50_m"))
    hours = case.get("hours_missing")
    if hours:
        config.DIFFUSION_HOURS = sorted(set(
            h for h in config.DIFFUSION_HOURS + [max(1, int(hours))] if h <= hours))
        if hours > config.SETTLED_AFTER_H:
            config.WEIGHTS["water"] *= config.SETTLED_WATER_MULT
    print("Case: %s  temperament=%s  50%%-ring %.0fm (e-fold %.0fm)  bearing=%s%s"
          % (case["name"], config.TEMPERAMENT,
             config.PRIOR_DECAY_M * config._RING50_TO_EFOLD, config.PRIOR_DECAY_M,
             "%.0f°" % bearing if bearing is not None else "none",
             "  CHASED (bolt)" if case.get("chased") else ""))
    if hours:
        print("Missing ~%.0fh (%.1f days)%s" % (
            hours, hours / 24,
            "  SETTLED phase: water weight x%.1f" % config.SETTLED_WATER_MULT
            if hours > config.SETTLED_AFTER_H else ""))
    print("[1/6] Downloading 3DEP 1m DEM (2km x 2km @ %.6f, %.6f)..." % (lat, lon))
    bbox, (cx, cy) = fetch.utm_window(lat, lon)
    dem, profile = fetch.download_dem(bbox)
    print("      DEM %dx%d px, elev %.1f-%.1f m  (%.1fs)"
          % (dem.shape[1], dem.shape[0], dem.min(), dem.max(), time.time() - t0))

    print("[2/6] Fetching OSM features (buildings/roads/water/veg) + passages...")
    osm = fetch.fetch_osm(lat, lon, busy_trail_names=case.get("busy_trails"))
    print("      " + ", ".join("%s:%d" % (k, len(v)) for k, v in osm.items()))
    passages = fetch.fetch_passages(lat, lon)
    from collections import Counter
    pc = Counter(p["kind"] for p in passages)
    print("      passages: " + (", ".join("%s:%d" % (k, v) for k, v in pc.items()) or "none"))

    print("[3/6] Rasterizing masks + computing signal layers...")
    masks = build_masks(osm, profile)
    t = profile["transform"]
    col0 = int((cx - t.c) / t.a)
    row0 = int((cy - t.f) / t.e)
    searched = []
    for s in case.get("searched", []):
        r, c = _latlon_to_rc(s["lat"], s["lon"], profile)
        searched.append(dict(s, row=r, col=c))
    home = case.get("home") or {}
    home_rc = None
    if home.get("lat") is not None:
        home_rc = _latlon_to_rc(home["lat"], home["lon"], profile)
        d_home = np.hypot((home_rc[0] - row0), (home_rc[1] - col0)) * config.RESOLUTION_M
        print("      home anchor %.0f m from last seen (weak pull, floor %.2f)"
              % (d_home, config.HOME_PULL_FLOOR))
    ctx = {"dem": dem, "masks": masks, "center_rc": (row0, col0),
           "res": config.RESOLUTION_M, "bearing_deg": bearing,
           "chased": case.get("chased"), "searched": searched,
           "home_rc": home_rc}
    base, stack = scoring.habitat_base(ctx)
    prior = layers.prior(ctx)            # includes flight-bearing cone/stretch
    stack["prior"] = prior
    priority, poa = scoring.poa_and_display(base, prior)

    # Bayesian effort update: discount what the crew has already covered.
    now_ms = time.time() * 1000
    discount = scoring.search_discount(ctx, now_ms)
    post, post_display, covered = scoring.posterior(poa, discount)
    print("      %d searched visits logged -> ~%.0f%% of probability mass covered"
          % (len(searched), 100 * covered))

    print("[4/6] Diffusing probable zone over time (%s h)..."
          % ",".join(str(h) for h in config.DIFFUSION_HOURS))
    diff_probs, dx = diffusion.diffuse(ctx, config.DIFFUSION_HOURS)
    dirw = layers.direction_weight(ctx) * layers.home_pull(ctx)
    # diffusion solve has no advection term; cone + home pull applied here
    time_priority = {h: scoring.combine_display(base, diff_probs[h] * dirw)
                     for h in diff_probs}
    for h in config.DIFFUSION_HOURS:
        s = diffusion.reach_stats(diff_probs[h], ctx)
        print("      %3dh zone: ~%.0f m reach (%.0f m north)  [%dm grid]"
              % (h, s["max_m"], s["north_m"], dx))

    print("[5/6] Ranking NEXT search segments on the posterior (after effort)...")
    hotspots = scoring.find_hotspots(post, stack, ctx)
    for hs in hotspots:
        hs["eta_h"] = next((h for h in config.DIFFUSION_HOURS
                            if diff_probs[h][hs["row"], hs["col"]] > 0.05), None)

    print("[6/6] Writing outputs...")
    tif = make_map.save_geotiff(post_display, profile)
    html = make_map.save_html(post_display, profile, hotspots, lat, lon,
                              time_priority, passages, prior_display=priority,
                              searched=searched, bearing=bearing, covered=covered)
    png = make_map.save_png(post_display, dem, hotspots, ctx)
    tpng = make_map.save_time_png(time_priority, dem, hotspots, ctx)
    gpx, csvf = make_map.save_spots(hotspots, lat, lon)
    cal = make_map.save_caltopo(post, profile, hotspots, lat, lon,
                                searched=searched, bearing=bearing)
    site = make_site.save_site(case, hotspots, searched=searched,
                               covered=covered)

    cum = sum(h["prob"] for h in hotspots)
    print("\n=== TOP NEXT-SEARCH SEGMENTS (%.1fs total) ===" % (time.time() - t0))
    print("      P(seg) = probability the dog is within ~%dm of that point,"
          % config.SEGMENT_RADIUS_M)
    print("      given everywhere already searched came up empty")
    print("rank  P(seg)  lat, lon              reach-by  top signals")
    for i, h in enumerate(hotspots, 1):
        top = sorted(h["breakdown"].items(), key=lambda kv: -kv[1])[:3]
        why = ", ".join("%s %.2f" % (k, v) for k, v in top)
        eta = "%2dh" % h["eta_h"] if h["eta_h"] else ">%dh" % config.DIFFUSION_HOURS[-1]
        print("%4d  %4.1f%%  %.6f, %.6f   %-5s  %s"
              % (i, 100 * h["prob"], h["lat"], h["lon"], eta, why))
    print("      (top %d segments hold %.0f%% of the remaining probability)"
          % (len(hotspots), 100 * cum))
    print("\nOutputs:\n  %s\n  %s\n  %s\n  %s\n  %s  (GPS waypoints)\n  %s"
          "\n  %s  (CalTopo: Add > Import)\n  %s  (public site page)"
          % (html, tpng, png, tif, gpx, csvf, cal, site))


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        main(casefile.load_case(sys.argv[1]))
    elif len(sys.argv) in (3, 4):
        main({"name": "adhoc",
              "temperament": sys.argv[3] if len(sys.argv) == 4 else None,
              "last_seen": {"lat": float(sys.argv[1]), "lon": float(sys.argv[2])},
              "searched": []})
    else:
        sys.exit("usage: python3 run.py CASE.json   |   python3 run.py LAT LON "
                 "[temperament: " + "|".join(config.TEMPERAMENTS) + "]")
