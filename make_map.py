"""Outputs: priority GeoTIFF, interactive folium HTML, static PNG, hotspot table."""
import json
import os

import folium
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from matplotlib.colors import LightSource
from rasterio.warp import transform as warp_transform

import config

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def _bounds_latlon(profile):
    t = profile["transform"]
    w, h = profile["width"], profile["height"]
    xs, ys = warp_transform(config.UTM_CRS, "EPSG:4326",
                            [t.c, t.c + w * t.a], [t.f + h * t.e, t.f])
    return [[ys[0], xs[0]], [ys[1], xs[1]]]  # [[south, west], [north, east]]


def _px_to_latlon(r, c, profile):
    t = profile["transform"]
    x, y = t * (c + 0.5, r + 0.5)
    xs, ys = warp_transform(config.UTM_CRS, "EPSG:4326", [x], [y])
    return ys[0], xs[0]


def save_geotiff(score, profile):
    os.makedirs(OUT_DIR, exist_ok=True)
    prof = profile.copy()
    prof.update(dtype="float32", count=1, nodata=None, compress="deflate")
    path = os.path.join(OUT_DIR, "priority.tif")
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(score.astype("float32"), 1)
    return path


def _score_rgba(score, thresh=0.12):
    """Inferno colormap, alpha ramping from a threshold up. Below `thresh` the
    overlay is FULLY transparent (shows the basemap cleanly) so low-priority
    ground doesn't render as a dirty wash; above it the alpha climbs quickly so
    the hot zone reads clearly over satellite imagery."""
    rgba = plt.get_cmap("inferno")(score)
    rgba[..., 3] = np.clip((score - thresh) / (1 - thresh), 0, 1) ** 0.8 * 0.95
    return rgba


def save_html(score, profile, hotspots, lat, lon, time_priority=None, passages=None,
              prior_display=None, searched=None, bearing=None, covered=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    bounds = _bounds_latlon(profile)
    overlay_png = os.path.join(OUT_DIR, "_overlay.png")
    plt.imsave(overlay_png, _score_rgba(score))

    m = folium.Map(location=[lat, lon], zoom_start=15, tiles="OpenStreetMap")
    folium.TileLayer(
        tiles=("https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Imagery/MapServer/tile/{z}/{y}/{x}"),
        attr="Esri World Imagery", name="Satellite").add_to(m)
    # Time-diffusion horizons as toggleable layers (default off).
    for h in sorted(time_priority or {}):
        png = os.path.join(OUT_DIR, "_overlay_%dh.png" % h)
        plt.imsave(png, _score_rgba(time_priority[h]))
        folium.raster_layers.ImageOverlay(
            image=png, bounds=bounds, opacity=0.75, show=False,
            name="Probable zone @ %dh" % h).add_to(m)
    # Raw priority BEFORE the searched-effort discount, for comparison.
    if prior_display is not None:
        ppng = os.path.join(OUT_DIR, "_overlay_prior.png")
        plt.imsave(ppng, _score_rgba(prior_display))
        folium.raster_layers.ImageOverlay(
            image=ppng, bounds=bounds, opacity=0.9, show=False,
            name="Priority ignoring effort spent").add_to(m)
    main_name = "WHERE TO LOOK NEXT (posterior)"
    if covered is not None:
        main_name += "  [~%.0f%% already covered]" % (100 * covered)
    folium.raster_layers.ImageOverlay(
        image=overlay_png, bounds=bounds, opacity=0.9, name=main_name).add_to(m)

    folium.Marker([lat, lon], tooltip="LAST SEEN",
                  icon=folium.Icon(color="red", icon="paw", prefix="fa")).add_to(m)
    # Flight bearing arrow (500 m ray along the reported heading).
    if bearing is not None:
        import math
        end_lat = lat + (500 * math.cos(math.radians(bearing))) / 111320.0
        end_lon = lon + (500 * math.sin(math.radians(bearing))) / (
            111320.0 * math.cos(math.radians(lat)))
        folium.PolyLine([[lat, lon], [end_lat, end_lon]], color="#d63031",
                        weight=3, dash_array="8 6",
                        tooltip="Seen running %.0f° (chased)" % bearing).add_to(m)
    # Evidence ring: 50% of lost dogs are found within this radius.
    folium.Circle([lat, lon], radius=config.PRIOR_DECAY_M * config._RING50_TO_EFOLD,
                  color="#0984e3", weight=2, fill=False, dash_array="4 8",
                  tooltip="50%% find ring (%.0f m)"
                          % (config.PRIOR_DECAY_M * config._RING50_TO_EFOLD)).add_to(m)
    # Effort already spent: searched visits (numbered like the user's own map),
    # each with its discount-kernel sweep radius, dimming the posterior beneath.
    if searched:
        import re
        fg = folium.FeatureGroup(name="Already searched (discounted)", show=True)
        for s in searched:
            num = re.search(r"\d+", s.get("note", ""))
            label = num.group(0) if num else "S"
            tip = "%s — POD %.0f%%, sweep r=%.0fm" % (
                s.get("note", "searched"), 100 * s["pod"], s["radius_m"])
            folium.Circle([s["lat"], s["lon"]], radius=s["radius_m"],
                          color="#00b894", weight=1.5, fill=True,
                          fill_opacity=0.12, tooltip=tip).add_to(fg)
            folium.Marker(
                [s["lat"], s["lon"]], tooltip=tip,
                icon=folium.DivIcon(html=(
                    '<div style="background:#00b894;color:#fff;border-radius:50%;'
                    'width:22px;height:22px;line-height:22px;text-align:center;'
                    'font-weight:bold;font-size:11px;border:2px solid #fff;'
                    'box-shadow:0 0 3px #000">{}</div>'.format(label)))).add_to(fg)
        fg.add_to(m)

    for i, h in enumerate(hotspots, 1):
        hlat, hlon = _px_to_latlon(h["row"], h["col"], profile)
        h["lat"], h["lon"] = hlat, hlon
        top = sorted(h["breakdown"].items(), key=lambda kv: -kv[1])[:3]
        why = ", ".join("{} {:.2f}".format(k, v) for k, v in top)
        eta = ("reachable by ~{}h".format(h["eta_h"]) if h.get("eta_h")
               else "far (late horizon)")
        folium.Marker(
            [hlat, hlon],
            tooltip="#{} — {:.1f}% ({})".format(i, 100 * h.get("prob", h["score"]), eta),
            popup=folium.Popup(
                "<b>#{}</b> {:.1f}% probability &middot; {}<br>{:.6f}, {:.6f}<br>{}".format(
                    i, 100 * h.get("prob", h["score"]), eta, hlat, hlon, why),
                max_width=300),
            icon=folium.DivIcon(html=(
                '<div style="background:#d63031;color:#fff;border-radius:50%;'
                'width:26px;height:26px;line-height:26px;text-align:center;'
                'font-weight:bold;border:2px solid #fff;box-shadow:0 0 4px #000">'
                '{}</div>'.format(i)))).add_to(m)

    # Culverts / underpasses — grade-separated passages a scared dog slips through
    if passages:
        colors = {"culvert": "#00b894", "rail underpass": "#0984e3",
                  "trail bridge": "#b2bec3"}
        fg = folium.FeatureGroup(name="Passages (culverts/underpasses)", show=True)
        for p in passages:
            folium.CircleMarker(
                [p["lat"], p["lon"]], radius=5,
                color=colors.get(p["kind"], "#636e72"), fill=True, fill_opacity=0.9,
                tooltip="{}{}".format(p["kind"], " — " + p["name"] if p["name"] else ""),
            ).add_to(fg)
        fg.add_to(m)

    folium.LayerControl().add_to(m)
    path = os.path.join(OUT_DIR, "search_map.html")
    m.save(path)
    return path


def save_png(score, dem, hotspots, ctx):
    os.makedirs(OUT_DIR, exist_ok=True)
    ls = LightSource(azdeg=315, altdeg=45)
    fig, ax = plt.subplots(figsize=(11, 11))
    ax.imshow(ls.hillshade(dem, vert_exag=2, dx=ctx["res"], dy=ctx["res"]),
              cmap="gray", alpha=0.9)
    im = ax.imshow(np.ma.masked_less(score, 0.15), cmap="inferno",
                   alpha=0.7, vmin=0, vmax=1)
    r0, c0 = ctx["center_rc"]
    ax.plot(c0, r0, "*", color="cyan", ms=22, mec="k", label="Last seen")
    if ctx.get("bearing_deg") is not None:
        import math
        b = math.radians(ctx["bearing_deg"])
        L = 400 / ctx["res"]
        ax.annotate("", xy=(c0 + L * math.sin(b), r0 - L * math.cos(b)),
                    xytext=(c0, r0),
                    arrowprops=dict(color="cyan", width=2, headwidth=10))
    searched = ctx.get("searched") or []
    for s in searched:
        ax.plot(s["col"], s["row"], "x", color="#00e690", ms=9, mew=2.5,
                label="Searched" if s is searched[0] else None)
    for i, h in enumerate(hotspots, 1):
        ax.plot(h["col"], h["row"], "o", color="red", ms=10, mec="w")
        ax.annotate(str(i), (h["col"], h["row"]), color="w", fontsize=9,
                    ha="center", va="center", fontweight="bold")
    ax.set_title("Where to look NEXT — posterior after searched effort discounted\n"
                 "(2km x 2km, 1m 3DEP LiDAR)")
    ax.legend(loc="upper right")
    fig.colorbar(im, ax=ax, shrink=0.7, label="priority")
    ax.set_axis_off()
    path = os.path.join(OUT_DIR, "search_map.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def save_spots(hotspots, lat, lon):
    """Field files: GPX waypoints (load into any phone GPS/map app) + CSV. Call
    AFTER save_html so each hotspot already has lat/lon."""
    os.makedirs(OUT_DIR, exist_ok=True)

    def why(h):
        top = sorted(h["breakdown"].items(), key=lambda kv: -kv[1])[:3]
        return ", ".join("%s %.2f" % (k, v) for k, v in top)

    def eta(h):
        return ("reach ~%dh" % h["eta_h"]) if h.get("eta_h") else "far"

    gpx = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="dog-finder" '
           'xmlns="http://www.topografix.com/GPX/1/1">',
           '  <wpt lat="%.6f" lon="%.6f"><name>LAST SEEN</name>'
           '<sym>Flag, Red</sym></wpt>' % (lat, lon)]
    csv = ["rank,probability_pct,lat,lon,reach_by,top_signals"]
    for i, h in enumerate(hotspots, 1):
        p = 100 * h.get("prob", h["score"])
        gpx.append('  <wpt lat="%.6f" lon="%.6f"><name>#%d %.0f%%</name>'
                   '<desc>%s; %s</desc></wpt>'
                   % (h["lat"], h["lon"], i, p, eta(h), why(h)))
        csv.append('%d,%.1f,%.6f,%.6f,%s,"%s"'
                   % (i, p, h["lat"], h["lon"], eta(h), why(h)))
    gpx.append('</gpx>')
    gpath = os.path.join(OUT_DIR, "search_spots.gpx")
    cpath = os.path.join(OUT_DIR, "search_spots.csv")
    with open(gpath, "w") as f:
        f.write("\n".join(gpx))
    with open(cpath, "w") as f:
        f.write("\n".join(csv))
    return gpath, cpath


def save_caltopo(post, profile, hotspots, lat, lon, searched=None, bearing=None):
    """CalTopo-importable GeoJSON (Add > Import on an existing map): probability
    zones as colored polygons (contours enclosing 50/75/90% of the posterior
    mass), numbered NEXT-search markers, DONE markers for logged effort, the
    flight-bearing line, and the median find-distance ring. Vector-only on
    purpose — raster (GeoTIFF) import needs a paid CalTopo tier."""
    import math

    from scipy import ndimage
    from skimage import measure

    os.makedirs(OUT_DIR, exist_ok=True)
    t = profile["transform"]

    def rc_to_ll(rr, cc):
        xs = [t.c + (c + 0.5) * t.a for c in cc]
        ys = [t.f + (r + 0.5) * t.e for r in rr]
        lons, lats = warp_transform(config.UTM_CRS, "EPSG:4326", xs, ys)
        return lats, lons

    def marker(la, lo, title, color, desc=""):
        return {"type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lo, la]},
                "properties": {"class": "Marker", "title": title,
                               "marker-symbol": "point", "marker-color": color,
                               "description": desc}}

    def shape(coords, title, color, fill_opacity=0.0, geom="LineString"):
        return {"type": "Feature",
                "geometry": {"type": geom,
                             "coordinates": [coords] if geom == "Polygon" else coords},
                "properties": {"class": "Shape", "title": title,
                               "stroke": color, "stroke-width": 2,
                               "stroke-opacity": 1, "fill": color,
                               "fill-opacity": fill_opacity}}

    feats = []
    # Cumulative-mass probability zones. Heavy smoothing is deliberate: the
    # suppression ribbons (roads/trails) slice fine-scale contours into dozens
    # of specks; the zones are ENVELOPES for planning, the fine structure lives
    # in the HTML/PNG overlays.
    dens = ndimage.gaussian_filter(post, sigma=25)
    dens /= dens.sum() + 1e-12
    flat = np.sort(dens.ravel())[::-1]
    csum = np.cumsum(flat)
    for frac, color in [(0.50, "#d63031"), (0.75, "#e17055"), (0.90, "#fdcb6e")]:
        level = flat[min(np.searchsorted(csum, frac), flat.size - 1)]
        for ring in measure.find_contours(dens, level):
            if len(ring) < 250:                     # skip speck contours
                continue
            lats, lons = rc_to_ll(ring[::4, 0], ring[::4, 1])
            coords = [[lo, la] for la, lo in zip(lats, lons)]
            coords.append(coords[0])
            feats.append(shape(coords, "%d%% probability zone" % (100 * frac),
                               color, fill_opacity=0.12, geom="Polygon"))

    feats.append(marker(lat, lon, "LAST SEEN", "#0984e3"))
    if bearing is not None:
        b = math.radians(bearing)
        end = [lon + 500 * math.sin(b) / (111320 * math.cos(math.radians(lat))),
               lat + 500 * math.cos(b) / 111320]
        feats.append(shape([[lon, lat], end],
                           "seen running %.0f deg" % bearing, "#d63031"))
    ring_m = config.PRIOR_DECAY_M * config._RING50_TO_EFOLD
    circ = [[lon + ring_m * math.sin(a) / (111320 * math.cos(math.radians(lat))),
             lat + ring_m * math.cos(a) / 111320]
            for a in np.linspace(0, 2 * math.pi, 65)]
    feats.append(shape(circ, "50%% find ring (%.0f m)" % ring_m, "#0984e3"))

    for i, h in enumerate(hotspots, 1):
        top = sorted(h["breakdown"].items(), key=lambda kv: -kv[1])[:3]
        desc = "P=%.1f%% within ~%dm; %s" % (
            100 * h["prob"], config.SEGMENT_RADIUS_M,
            ", ".join("%s %.2f" % kv for kv in top))
        feats.append(marker(h["lat"], h["lon"], "NEXT %d" % i, "#d63031", desc))
    for s in searched or []:
        feats.append(marker(s["lat"], s["lon"],
                            "DONE %s" % s.get("note", "searched"), "#00b894",
                            "POD %.0f%%, sweep r=%.0fm" % (100 * s["pod"],
                                                           s["radius_m"])))

    path = os.path.join(OUT_DIR, "caltopo_import.json")
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    return path


def save_time_png(time_priority, dem, hotspots, ctx):
    """Side-by-side panels of the search priority at each diffusion horizon,
    showing how the probable zone grows over time."""
    os.makedirs(OUT_DIR, exist_ok=True)
    hours = sorted(time_priority)
    ls = LightSource(azdeg=315, altdeg=45)
    hs = ls.hillshade(dem, vert_exag=2, dx=ctx["res"], dy=ctx["res"])
    r0, c0 = ctx["center_rc"]
    fig, axs = plt.subplots(1, len(hours), figsize=(6.5 * len(hours), 6.8))
    if len(hours) == 1:
        axs = [axs]
    for ax, h in zip(axs, hours):
        ax.imshow(hs, cmap="gray", alpha=0.9)
        ax.imshow(np.ma.masked_less(time_priority[h], 0.30), cmap="inferno",
                  alpha=0.7, vmin=0, vmax=1)
        ax.plot(c0, r0, "*", color="cyan", ms=18, mec="k")
        for i, sp in enumerate(hotspots, 1):
            reached = sp.get("eta_h") and sp["eta_h"] <= h
            ax.plot(sp["col"], sp["row"], "o", ms=8,
                    color="red" if reached else "none",
                    mec="w" if reached else "gray", mew=1)
        ax.set_title("%d hours missing" % h)
        ax.set_axis_off()
    fig.suptitle("Probable search zone growth (diffusion on movement-resistance surface)",
                 fontsize=13)
    path = os.path.join(OUT_DIR, "search_zones_time.png")
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return path
