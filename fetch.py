"""Data acquisition: USGS 3DEP 1m DEM + OSM features via Overpass.

Cached in data/<case-slug>/ (run.py points DATA_DIR at the case's subdir) with
the coordinate baked into each filename, so changing a case's last-seen point
or switching cases never needs a manual cache clear."""
import json
import os
import time

import numpy as np
import rasterio
import requests
from rasterio.transform import from_origin
from rasterio.warp import transform as warp_transform

import config

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _cache_path(name):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, name)

# OSM highway values that carry motor traffic (trails/footpaths excluded)
MOTOR_ROADS = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "residential",
    "unclassified", "service", "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link", "living_street",
}
# Busy roads a scared dog is unlikely to cross (movement barriers).
# tertiary included: neighborhood collectors like W Allens Lane carry real
# traffic and read as barriers to a panicked dog.
BARRIER_ROADS = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "tertiary_link",
}
# Trails/footpaths: travel corridors but not hiding spots (graded beds show up
# as false concave/flat signals in the LiDAR)
TRAILS = {"path", "footway", "steps", "bridleway", "cycleway", "track"}


def utm_window(lat, lon):
    """Square UTM window (xmin, ymin, xmax, ymax) centered on lat/lon."""
    xs, ys = warp_transform("EPSG:4326", config.UTM_CRS, [lon], [lat])
    cx, cy = xs[0], ys[0]
    h = config.HALF_WIDTH_M
    return (cx - h, cy - h, cx + h, cy + h), (cx, cy)


def download_dem(bbox, cache=True):
    """Fetch 3DEP best-available DEM as GeoTIFF on a 1m UTM grid. Returns (array, profile)."""
    path = _cache_path("dem_%d_%d.tif" % (round(bbox[0]), round(bbox[1])))
    if not (cache and os.path.exists(path)):
        n = int(round((bbox[2] - bbox[0]) / config.RESOLUTION_M))
        params = {
            "bbox": ",".join(str(v) for v in bbox),
            "bboxSR": config.UTM_CRS.split(":")[1],
            "imageSR": config.UTM_CRS.split(":")[1],
            "size": "{},{}".format(n, n),
            "format": "tiff",
            "pixelType": "F32",
            "interpolation": "RSP_BilinearInterpolation",
            "f": "image",
        }
        for attempt in range(6):
            try:
                r = requests.get(config.DEM_URL, params=params, timeout=120)
                if r.ok and r.content[:4] in (b"II*\x00", b"MM\x00*"):
                    with open(path, "wb") as f:
                        f.write(r.content)
                    break
            except requests.RequestException:
                pass
            time.sleep(2 + 3 * attempt)
        else:
            raise RuntimeError("3DEP DEM download failed: %s" % r.text[:300])

    with rasterio.open(path) as src:
        dem = src.read(1).astype("float64")
        profile = src.profile.copy()
    # Clean nodata (large negatives / NaN) with the median terrain value
    bad = ~np.isfinite(dem) | (dem < -1000)
    if bad.any():
        dem[bad] = np.median(dem[~bad])
    # Ensure profile carries our expected georeferencing
    profile.update(crs=config.UTM_CRS,
                   transform=from_origin(bbox[0], bbox[3],
                                         config.RESOLUTION_M, config.RESOLUTION_M))
    return dem, profile


def _overpass(query, cache_path):
    """POST an Overpass query, retrying across mirrors; cache raw text."""
    last = None
    for url in config.OVERPASS_URLS:
        for _ in range(2):
            try:
                r = requests.post(url, data={"data": query}, timeout=120,
                                  headers={"User-Agent": "lost-dog-search-poc/0.1 (ann.raiho@gmail.com)"})
                if r.ok and r.text.lstrip().startswith("{"):
                    with open(cache_path, "w") as f:
                        f.write(r.text)
                    return
                last = "HTTP %s: %s" % (r.status_code, r.text[:150])
            except requests.RequestException as e:
                last = str(e)
            time.sleep(3)
    raise RuntimeError("Overpass failed on all mirrors: %s" % last)


def _bbox_str(lat, lon):
    dlat = (config.HALF_WIDTH_M + 100) / 111000.0
    dlon = dlat / np.cos(np.radians(lat))
    return "{},{},{},{}".format(lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def fetch_passages(lat, lon, cache=True):
    """Culverts, rail underpasses, and trail bridges — grade-separated PASSAGES a
    scared dog can slip through (documented travel/hiding features). Free source:
    OpenStreetMap. Returns [{lat, lon, kind, name}], never raises (best-effort)."""
    path = _cache_path("passages_%.4f_%.4f.json" % (lat, lon))
    bb = _bbox_str(lat, lon)
    query = """[out:json][timeout:90];
(
  way["man_made"="culvert"]({bb});
  way["tunnel"="culvert"]({bb});
  way["waterway"]["tunnel"]({bb});
  node["man_made"="culvert"]({bb});
  way["bridge"]["railway"]({bb});
  way["bridge"]["highway"~"footway|path|cycleway"]({bb});
);
out geom tags;""".format(bb=bb)
    try:
        if not (cache and os.path.exists(path)):
            _overpass(query, path)
        with open(path) as f:
            elements = json.load(f)["elements"]
    except (RuntimeError, OSError, ValueError):
        return []

    out = []
    for el in elements:
        tags = el.get("tags", {})
        if tags.get("man_made") == "culvert" or tags.get("tunnel") == "culvert" \
                or (tags.get("waterway") and "tunnel" in tags):
            kind = "culvert"
        elif tags.get("bridge") and "railway" in tags:
            kind = "rail underpass"
        elif tags.get("bridge"):
            kind = "trail bridge"
        else:
            continue
        # midpoint of the feature
        if el.get("type") == "node":
            la, lo = el["lat"], el["lon"]
        elif el.get("geometry"):
            g = el["geometry"][len(el["geometry"]) // 2]
            la, lo = g["lat"], g["lon"]
        else:
            continue
        out.append({"lat": la, "lon": lo, "kind": kind,
                    "name": tags.get("name", "")})
    return out


def fetch_osm(lat, lon, cache=True, busy_trail_names=None):
    """Fetch buildings/roads/water/vegetation features in the bbox. Returns dict of
    {category: [geojson geometries in UTM coords]}. Trails whose OSM name matches
    an entry in busy_trail_names also land in 'busy_trails' (people pressure)."""
    path = _cache_path("osm_%.4f_%.4f.json" % (lat, lon))
    # bbox in lat/lon with a small margin
    dlat = (config.HALF_WIDTH_M + 100) / 111000.0
    dlon = dlat / np.cos(np.radians(lat))
    bb = "{},{},{},{}".format(lat - dlat, lon - dlon, lat + dlat, lon + dlon)
    query = """[out:json][timeout:90];
(
  way["building"]({bb});
  relation["building"]({bb});
  way["highway"]({bb});
  way["railway"]({bb});
  way["waterway"]({bb});
  way["natural"="water"]({bb});
  relation["natural"="water"]({bb});
  way["natural"~"wood|scrub"]({bb});
  relation["natural"~"wood|scrub"]({bb});
  way["landuse"~"forest|meadow"]({bb});
  relation["landuse"~"forest|meadow"]({bb});
);
out geom;""".format(bb=bb)

    if not (cache and os.path.exists(path)):
        _overpass(query, path)

    with open(path) as f:
        elements = json.load(f)["elements"]
    return _categorize(elements, busy_trail_names)


def _to_utm_coords(pts):
    """[{lat,lon},...] -> [[x,y],...] in UTM."""
    xs, ys = warp_transform("EPSG:4326", config.UTM_CRS,
                            [p["lon"] for p in pts], [p["lat"] for p in pts])
    return [[x, y] for x, y in zip(xs, ys)]


def _way_geoms(el):
    """Yield coordinate lists for a way or a relation's outer members."""
    if el["type"] == "way" and "geometry" in el:
        yield el["geometry"]
    elif el["type"] == "relation":
        for m in el.get("members", []):
            if m.get("role") in ("outer", "") and m.get("geometry"):
                yield m["geometry"]


def _closed(coords):
    """True if the way is a genuinely closed ring (a real area). Open relation
    members are NOT closed — force-closing them joins distant endpoints into a
    giant bogus fill (e.g. a creek segment blanketing the whole valley)."""
    return len(coords) >= 4 and coords[0] == coords[-1]


def _categorize(elements, busy_trail_names=None):
    busy_names = [n.lower() for n in (busy_trail_names or [])]
    cats = {"buildings": [], "roads": [], "barriers": [], "trails": [],
            "busy_trails": [], "rails": [], "water": [], "veg": []}
    for el in elements:
        tags = el.get("tags", {})
        for pts in _way_geoms(el):
            if len(pts) < 2:
                continue
            coords = _to_utm_coords(pts)
            if "building" in tags:
                if _closed(coords):
                    cats["buildings"].append({"type": "Polygon", "coordinates": [coords]})
            elif tags.get("highway") in MOTOR_ROADS:
                line = {"type": "LineString", "coordinates": coords}
                cats["roads"].append(line)
                # bridge segments are passable UNDERNEATH -> not barriers
                if (tags["highway"] in BARRIER_ROADS
                        and tags.get("bridge") not in ("yes", "viaduct")):
                    cats["barriers"].append(line)
            elif tags.get("highway") in TRAILS:
                line = {"type": "LineString", "coordinates": coords}
                cats["trails"].append(line)
                name = tags.get("name", "").lower()
                if name and any(b in name for b in busy_names):
                    cats["busy_trails"].append(line)
            elif "railway" in tags:
                # rail corridors: dog travel routes, but the bed isn't a
                # hiding spot (and active track is dangerous to send searchers)
                cats["rails"].append({"type": "LineString", "coordinates": coords})
            elif "waterway" in tags:
                cats["water"].append({"type": "LineString", "coordinates": coords})
            elif tags.get("natural") == "water":
                # real closed water body -> fill; open relation member -> thin
                # line (the creek edge), never a force-closed blanket polygon
                if _closed(coords):
                    cats["water"].append({"type": "Polygon", "coordinates": [coords]})
                else:
                    cats["water"].append({"type": "LineString", "coordinates": coords})
            elif (tags.get("natural") in ("wood", "scrub")
                  or tags.get("landuse") in ("forest", "meadow")):
                if _closed(coords):
                    cats["veg"].append({"type": "Polygon", "coordinates": [coords]})
    return cats
