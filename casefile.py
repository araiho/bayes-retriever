"""Case file: everything known about ONE lost-dog case, in one JSON.

Keeps the pipeline general (webtool-ready): the model code never hard-codes a
case; new sightings / search effort are added to the case JSON (or to the
imported map export) and the pipeline is re-run.

Case JSON format (explicit fields OVERRIDE anything pulled from an import):
{
  "name": "Maisy",
  "temperament": "xenophobic",          # key into config.TEMPERAMENTS
  "median_found_mi": 0.18,              # evidence: 50% of dogs found within this
  "chased": true,                       # panicked bolt (e.g. chased by a car):
                                        #   direction prior sharpened + stretched
  "last_seen": {"lat": .., "lon": .., "bearing_deg": 270},
  "search_log": {
      "import": "caltopo:F0M58FH",      # LIVE shared CalTopo map (preferred),
                                        #   or a path to a GeoJSON export
      "default_pod": 0.4,               # P(detect | dog was in that segment)
      "default_radius_m": 60            # effective sweep radius of one visit
  },
  "searched": [                         # extra effort not in the export
      {"lat": .., "lon": .., "pod": 0.5, "radius_m": 80, "note": "owner hours"}
  ]
}

Map-export adapter (Gaia/Avenza/Caltopo-style GeoJSON FeatureCollection):
  * Point titled like "search/check spot N"  -> searched visit (with timestamp)
  * Point titled "last seen"                 -> last-seen candidate (newest wins)
  * LineString titled "last seen running"   -> flight bearing (from its coords)

Live CalTopo import additionally understands sightings:
  * Point titled "SIGHTING <when/what>"           -> shown on the site, NOT
                                                     adopted by the model
  * Point retitled "SIGHTING CONFIRMED <...>"     -> if newer than lost_since,
    becomes the new last_seen and resets lost_since to the marker's creation
    time ("running NNN" in the title sets the bearing). Explicit case-file
    fields win over anything older; to correct a bad confirmation, set the
    case file's lost_since at or after the sighting time.
"""
import json
import math
import os
import re
from datetime import datetime

import caltopo_live
import config

_SEARCH_RE = re.compile(r"\b(search|check|looked|visited)\b", re.I)
_LAST_SEEN_RE = re.compile(r"last\s*seen", re.I)


def _bearing_deg(lon1, lat1, lon2, lat2):
    """Compass bearing (deg, 0=N) from point 1 to point 2, flat-earth approx
    (fine at these scales)."""
    de = (lon2 - lon1) * math.cos(math.radians(lat1))
    dn = lat2 - lat1
    return math.degrees(math.atan2(de, dn)) % 360


def _feat_time_ms(props):
    return props.get("timestamp") or props.get("-created-on") or props.get("updated")


def parse_features(features):
    """Pull searched visits, last-seen point, and flight bearing out of a list
    of map features (export file or live CalTopo, ALREADY filtered of our own
    write-backs). When several "last seen" candidates exist, newest wins."""
    searched, last_seen, bearing = [], None, None
    for feat in features:
        props = feat.get("properties", {})
        title = props.get("title", "") or ""
        geom = feat.get("geometry") or {}
        if geom.get("type") == "Point":
            lon, lat = geom["coordinates"][:2]
            if _LAST_SEEN_RE.search(title):
                cand = {"lat": lat, "lon": lon, "t_ms": _feat_time_ms(props)}
                if last_seen is None or (cand["t_ms"] or 0) >= (last_seen.get("t_ms") or 0):
                    last_seen = cand
            elif _SEARCH_RE.search(title):
                searched.append({"lat": lat, "lon": lon, "note": title,
                                 "t_ms": _feat_time_ms(props),
                                 "creator": props.get("creator")
                                            or props.get("-created-by") or ""})
        elif geom.get("type") == "LineString" and _LAST_SEEN_RE.search(title):
            (lon1, lat1), (lon2, lat2) = geom["coordinates"][0][:2], geom["coordinates"][-1][:2]
            bearing = _bearing_deg(lon1, lat1, lon2, lat2)
    return searched, last_seen, bearing


def parse_map_export(path):
    """Back-compat wrapper: parse a phone-map GeoJSON export file."""
    with open(os.path.expanduser(path)) as f:
        gj = json.load(f)
    return parse_features(gj.get("features", []))


def _lost_since_ms(iso):
    """Epoch ms of a lost_since ISO datetime (naive = local time)."""
    return datetime.fromisoformat(iso).timestamp() * 1000


def load_case(path):
    """Resolve a case JSON (+ imported map export or live CalTopo map) into one
    flat dict: name, slug, temperament, ring50_m, chased,
    last_seen{lat,lon,bearing_deg}, home{lat,lon},
    searched[{lat,lon,pod,radius_m,t_ms,note}], sightings, map_id, fingerprint."""
    with open(os.path.expanduser(path)) as f:
        case = json.load(f)

    log = case.get("search_log", {})
    pod0 = log.get("default_pod", config.SEARCH_POD_DEFAULT)
    rad0 = log.get("default_radius_m", config.SEARCH_RADIUS_M_DEFAULT)
    searched, imp_last_seen, imp_bearing = [], None, None
    sightings, map_id, fp = [], None, None
    imp = log.get("import")
    live = caltopo_live.MAP_REF_RE.match(imp or "")
    if live:
        map_id = live.group(1)
        feats, _ = caltopo_live.fetch_features_cached(map_id)
        team, _own, sightings = caltopo_live.partition(feats)
        fp = caltopo_live.fingerprint(team, sightings)
        searched, imp_last_seen, imp_bearing = parse_features(team)
    elif imp:
        searched, imp_last_seen, imp_bearing = parse_map_export(imp)
    searched += case.get("searched", [])
    for s in searched:
        s.setdefault("pod", pod0)
        s.setdefault("radius_m", rad0)

    last_seen = dict(imp_last_seen or {})
    last_seen.update(case.get("last_seen", {}))       # explicit wins
    if last_seen.get("bearing_deg") is None and imp_bearing is not None:
        last_seen["bearing_deg"] = imp_bearing
    lost_since = case.get("lost_since")   # ISO datetime of escape/last sighting

    # A CONFIRMED sighting newer than lost_since supersedes everything: it IS
    # the freshest ground truth. (Unconfirmed ones are display-only.)
    for s in sightings:                   # sorted oldest -> newest
        if not s["confirmed"] or not s["t_ms"]:
            continue
        if lost_since and s["t_ms"] <= _lost_since_ms(lost_since):
            continue
        last_seen = {"lat": s["lat"], "lon": s["lon"], "t_ms": s["t_ms"],
                     "note": "adopted from CalTopo: %s" % s["title"]}
        if s.get("bearing_deg") is not None:
            last_seen["bearing_deg"] = s["bearing_deg"]
        lost_since = datetime.fromtimestamp(s["t_ms"] / 1000).astimezone().isoformat()
        print("ADOPTED confirmed sighting as last_seen: %.6f, %.6f (%s)"
              % (s["lat"], s["lon"], s["title"]))
    if "lat" not in last_seen:
        raise ValueError("case needs a last_seen (explicit or in the import)")

    ring50_m = None
    if case.get("median_found_mi"):
        ring50_m = case["median_found_mi"] * 1609.34   # median find dist IS the 50% ring

    hours_missing = None
    if lost_since:
        t = datetime.fromisoformat(lost_since)
        now = datetime.now(t.tzinfo)  # naive in, naive (local) now
        hours_missing = max(0.0, (now - t).total_seconds() / 3600.0)

    slug = os.path.splitext(os.path.basename(path))[0]
    return {
        "hours_missing": hours_missing,
        "busy_trails": case.get("busy_trails", []),
        "name": case.get("name", slug),
        "slug": slug,
        "temperament": case.get("temperament"),
        "ring50_m": ring50_m,
        "chased": bool(case.get("chased")),
        "last_seen": last_seen,
        "home": case.get("home"),
        "searched": searched,
        "sightings": sightings,
        "map_id": map_id,
        "fingerprint": fp,
        "lost_since": lost_since,
    }
