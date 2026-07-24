"""Live sync with a shared CalTopo map — the team's multi-user, timestamped
entry layer.

Searchers log effort by dropping markers on the shared map (titles containing
search/check/looked/visited) and report sightings as markers titled
"SIGHTING <when/what>". This module pulls the full live feature set
(unauthenticated read works on URL-shared maps) and splits it into:

  * team features   -> fed to casefile.parse_features (search log, last seen)
  * our own features -> zones / NEXT markers this pipeline previously wrote
                        back or that were imported from caltopo_import.json;
                        these MUST be filtered out before parsing or every
                        "DONE Search spot N" would double-count real effort
  * sightings       -> never auto-adopted unless retitled "SIGHTING CONFIRMED"

The fingerprint over team features + sightings lets CI skip reruns when
nothing a human did has changed (our own write-backs are excluded on purpose).

CLI (used by the GitHub Actions change check):
    python3 caltopo_live.py MAP_ID --fingerprint
    python3 caltopo_live.py --case cases/maisy.json --fingerprint
    python3 caltopo_live.py MAP_ID --save tests/fixtures/caltopo_since0.json
"""
import hashlib
import json
import os
import re
import sys

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "state")
API_URL = "https://caltopo.com/api/v1/map/{map_id}/since/0"
USER_AGENT = "dog-finder/0.2 (ann.raiho@gmail.com)"

# "caltopo:F0M58FH" in a case file's search_log.import means live sync
MAP_REF_RE = re.compile(r"^caltopo:([A-Za-z0-9]+)$")

# Folder that caltopo_write.py owns on the map; anything inside is ours.
OWN_FOLDER_TITLE = "AUTO dog-finder"

# Belt to the folder's suspenders: titles this pipeline itself generates
# (make_map.save_caltopo / caltopo_write.py). Case-sensitive on purpose —
# our markers are ALL-CAPS ("DONE ...", "LAST SEEN"), searchers' are not.
OWN_TITLE_RE = re.compile(
    r"^(?:NEXT \d+$|DONE |LAST SEEN$|\d+% probability zone$"
    r"|50% find ring|seen running \d+ deg$)")

SIGHTING_RE = re.compile(r"^\s*SIGHTING\b(?P<confirmed>\s+CONFIRMED\b)?", re.I)
_BEARING_IN_TITLE_RE = re.compile(r"\brunning\s+(\d{1,3})\b", re.I)


def _t_ms(props):
    return props.get("timestamp") or props.get("-created-on") or props.get("updated")


def _creator(props):
    return props.get("creator") or props.get("-created-by") or ""


def fetch_features(map_id, timeout=30):
    """GET the full live feature set. Returns (features, server_timestamp)."""
    r = requests.get(API_URL.format(map_id=map_id), timeout=timeout,
                     headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    result = r.json()["result"]
    return result.get("state", {}).get("features", []), result.get("timestamp")


def _snapshot_path(map_id):
    return os.path.join(STATE_DIR, "caltopo_%s.json" % map_id)


def fetch_features_cached(map_id):
    """fetch_features with a last-good-snapshot fallback, so a CalTopo outage
    degrades to a stale (loudly flagged) run instead of a failed one."""
    try:
        feats, ts = fetch_features(map_id)
    except (requests.RequestException, KeyError, ValueError) as e:
        path = _snapshot_path(map_id)
        if not os.path.exists(path):
            raise RuntimeError(
                "CalTopo fetch failed and no snapshot exists (%s)" % e)
        with open(path) as f:
            snap = json.load(f)
        print("WARNING: CalTopo fetch failed (%s) — using snapshot from %s"
              % (e, path), file=sys.stderr)
        return snap["features"], snap.get("timestamp")
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_snapshot_path(map_id), "w") as f:
        json.dump({"features": feats, "timestamp": ts}, f)
    return feats, ts


def partition(features):
    """Split live features into (team_feats, own_feats, sightings).

    Own = inside the AUTO folder OR matching our generated titles. Sightings
    are pulled out of team_feats so the search-log regex never sees them."""
    own_folder_ids = {
        f.get("id") for f in features
        if f.get("properties", {}).get("class") == "Folder"
        and f.get("properties", {}).get("title") == OWN_FOLDER_TITLE}
    team, own, sightings = [], [], []
    for f in features:
        props = f.get("properties", {})
        title = props.get("title", "") or ""
        if props.get("class") == "Folder":
            (own if f.get("id") in own_folder_ids else team).append(f)
            continue
        if props.get("folderId") in own_folder_ids or OWN_TITLE_RE.match(title):
            own.append(f)
            continue
        m = SIGHTING_RE.match(title)
        geom = f.get("geometry") or {}
        if m and geom.get("type") == "Point":
            lon, lat = geom["coordinates"][:2]
            b = _BEARING_IN_TITLE_RE.search(title)
            sightings.append({
                "lat": lat, "lon": lon, "title": title,
                "t_ms": _t_ms(props), "creator": _creator(props),
                "confirmed": bool(m.group("confirmed")),
                "bearing_deg": float(b.group(1)) if b else None})
            continue
        team.append(f)
    sightings.sort(key=lambda s: s["t_ms"] or 0)
    return team, own, sightings


def fingerprint(team_feats, sightings):
    """Hash of everything humans control on the map. Stable across our own
    write-backs, so CI can skip runs when no searcher touched anything."""
    keys = ["%s:%s" % (f.get("id"), f.get("properties", {}).get("updated"))
            for f in team_feats]
    keys += ["%s:%s:%s" % (s.get("t_ms"), s["title"], s["confirmed"])
             for s in sightings]
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()


def map_id_from_case(case_path):
    with open(os.path.expanduser(case_path)) as f:
        imp = json.load(f).get("search_log", {}).get("import", "")
    m = MAP_REF_RE.match(imp or "")
    if not m:
        sys.exit("case %s has no caltopo:<MAPID> import" % case_path)
    return m.group(1)


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("map_id", nargs="?", help="CalTopo map id, e.g. F0M58FH")
    ap.add_argument("--case", help="read the map id from this case JSON")
    ap.add_argument("--fingerprint", action="store_true",
                    help="print the team-activity fingerprint and exit")
    ap.add_argument("--save", metavar="PATH",
                    help="save the raw live GeoJSON (fixture/debug)")
    args = ap.parse_args(argv)
    map_id = args.map_id or (args.case and map_id_from_case(args.case))
    if not map_id:
        ap.error("need MAP_ID or --case")
    feats, ts = fetch_features(map_id)
    if args.save:
        with open(args.save, "w") as f:
            json.dump({"result": {"state": {"features": feats},
                                  "timestamp": ts}}, f)
        print("saved %d features -> %s" % (len(feats), args.save))
    team, own, sightings = partition(feats)
    if args.fingerprint:
        print(fingerprint(team, sightings))
    else:
        print("map %s: %d features = %d team + %d own + %d sightings (%d unconfirmed)"
              % (map_id, len(feats), len(team), len(own), len(sightings),
                 sum(1 for s in sightings if not s["confirmed"])))


if __name__ == "__main__":
    main(sys.argv[1:])
