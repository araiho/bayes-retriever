"""Regression tests for the one fragile boundary: classifying live CalTopo
features (team effort vs. our own write-backs vs. sightings). The fixture is a
real snapshot of the Maisy map taken 2026-07-24, when it held 16 team features
and 30 of our own imported NEXT/DONE/zone features."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import caltopo_live
import casefile

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "caltopo_since0.json")


def _features():
    with open(FIXTURE) as f:
        return json.load(f)["result"]["state"]["features"]


def _sighting(title, t_ms=1785000000000, lat=40.05, lon=-75.21):
    return {"id": "test-%s" % title, "geometry":
            {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"class": "Marker", "title": title,
                           "creator": "TESTER", "updated": t_ms,
                           "-created-on": t_ms}}


def test_partition_covers_everything():
    feats = _features()
    team, own, sightings = caltopo_live.partition(feats)
    assert len(team) + len(own) + len(sightings) == len(feats) == 46
    assert len(team) == 16
    assert len(own) == 30


def test_own_features_fully_claimed():
    _, own, _ = caltopo_live.partition(_features())
    titles = [f["properties"].get("title", "") for f in own]
    assert sum(t.startswith("NEXT ") for t in titles) == 10
    assert sum(t.startswith("DONE ") for t in titles) == 13
    assert sum("probability zone" in t for t in titles) == 5
    assert sum(t.startswith("50% find ring") for t in titles) == 1
    assert sum(t.startswith("seen running") for t in titles) == 1


def test_team_features_never_ours():
    team, _, _ = caltopo_live.partition(_features())
    for f in team:
        t = f["properties"].get("title", "")
        assert not t.startswith(("NEXT ", "DONE "))
        assert "probability zone" not in t


def test_search_log_no_double_count():
    team, _, _ = caltopo_live.partition(_features())
    searched, last_seen, bearing = casefile.parse_features(team)
    # 12 genuine team visits: Search spot 0-11 with spot 8 logged as "Check
    # spot 8". The DONE mirrors of the same spots must NOT inflate this.
    assert len(searched) == 12
    assert last_seen is not None
    assert bearing is not None
    for s in searched:
        assert s["t_ms"], "every visit must carry a timestamp"
        assert s["creator"], "every visit must carry a contributor id"


def test_unfiltered_parse_would_double_count():
    # Documents WHY partition() must run before parse_features: the raw map
    # contains "DONE Search spot N" mirrors that match the search regex.
    searched, _, _ = casefile.parse_features(_features())
    assert len(searched) > 12


def test_newest_last_seen_wins():
    team, _, _ = caltopo_live.partition(_features())
    _, last_seen, _ = casefile.parse_features(team)
    by_time = max(
        (f for f in team
         if (f.get("geometry") or {}).get("type") == "Point"
         and casefile._LAST_SEEN_RE.search(f["properties"].get("title", ""))),
        key=lambda f: f["properties"].get("updated") or 0)
    lon, lat = by_time["geometry"]["coordinates"][:2]
    assert (last_seen["lat"], last_seen["lon"]) == (lat, lon)


def test_fingerprint_ignores_own_features():
    feats = _features()
    team, own, sightings = caltopo_live.partition(feats)
    fp_all = caltopo_live.fingerprint(team, sightings)
    own_ids = {f["id"] for f in own}
    team2, _, s2 = caltopo_live.partition(
        [f for f in feats if f["id"] not in own_ids])
    assert caltopo_live.fingerprint(team2, s2) == fp_all


def test_fingerprint_changes_on_team_edit():
    team, _, sightings = caltopo_live.partition(_features())
    fp = caltopo_live.fingerprint(team, sightings)
    team[0]["properties"]["updated"] += 1
    assert caltopo_live.fingerprint(team, sightings) != fp


def test_sightings_partitioned_not_searched():
    feats = _features() + [
        _sighting("SIGHTING 7:15am trotting north on rail line"),
        _sighting("SIGHTING CONFIRMED running 45 near bridge", t_ms=1785000001000)]
    team, _, sightings = caltopo_live.partition(feats)
    assert len(sightings) == 2
    searched, _, _ = casefile.parse_features(team)
    assert len(searched) == 12          # sightings never count as effort
    unconf, conf = sightings
    assert not unconf["confirmed"] and unconf["bearing_deg"] is None
    assert conf["confirmed"] and conf["bearing_deg"] == 45.0
    assert conf["creator"] == "TESTER"


def test_own_folder_membership_claims_features():
    folder = {"id": "FOLDER1", "properties":
              {"class": "Folder", "title": caltopo_live.OWN_FOLDER_TITLE}}
    inside = {"id": "M1", "geometry":
              {"type": "Point", "coordinates": [-75.2, 40.0]},
              "properties": {"class": "Marker", "title": "Searched by bot",
                             "folderId": "FOLDER1", "updated": 1}}
    team, own, _ = caltopo_live.partition(_features() + [folder, inside])
    own_ids = {f.get("id") for f in own}
    assert {"FOLDER1", "M1"} <= own_ids
    searched, _, _ = casefile.parse_features(team)
    assert len(searched) == 12          # despite the search-word title
