"""OPTIONAL write-back: push fresh probability zones + NEXT markers straight
into the team's CalTopo map, replacing the manual Add > Import step.

Feature-flagged (CI runs it only when repo variable CALTOPO_WRITE=1) because
CalTopo's write API is unofficial. Requests are HMAC-SHA256 signed with a
personal-account credential (free): sign in at caltopo.com, visit
  https://caltopo.com/app/activate/offline?redirect=localhost
grab the 8-char code from the network tab, then
  https://caltopo.com/api/v1/activate?code=<code>
returns {"code": <12-char credential id>, "key": <44-char base64 key>}.
Export as CALTOPO_CREDENTIAL_ID and CALTOPO_KEY.

Safety model: everything this module writes lives in a Folder titled
"AUTO dog-finder" (caltopo_live.OWN_FOLDER_TITLE). Each sync deletes ONLY
features inside that folder (ownership recovered from the live map itself,
no state file), then re-creates the current set. Features searchers created
are never touched. DONE markers are deliberately NOT mirrored (they'd stack
on the searchers' own markers); they remain in the downloadable import file.

Usage:
    python3 caltopo_write.py MAP_ID output/<slug>/caltopo_import.json [--dry-run]

Verify on a THROWAWAY map before ever pointing at the team map.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time

import requests

import caltopo_live

BASE = "https://caltopo.com"


class Session:
    def __init__(self, cred_id=None, key=None):
        self.cred_id = cred_id or os.environ.get("CALTOPO_CREDENTIAL_ID")
        self.key = key or os.environ.get("CALTOPO_KEY")
        if not (self.cred_id and self.key):
            sys.exit("need CALTOPO_CREDENTIAL_ID and CALTOPO_KEY in the "
                     "environment (see module docstring)")

    def _signed_params(self, method, path, payload):
        expires = int(time.time() * 1000) + 120_000
        token = "%s %s\n%d\n%s" % (method, path, expires, payload)
        sig = base64.b64encode(
            hmac.new(base64.b64decode(self.key), token.encode(),
                     hashlib.sha256).digest()).decode()
        return {"id": self.cred_id, "expires": expires,
                "signature": sig, "json": payload}

    def post(self, path, obj):
        params = self._signed_params("POST", path,
                                     json.dumps(obj, separators=(",", ":")))
        r = requests.post(BASE + path, data=params, timeout=30,
                          headers={"User-Agent": caltopo_live.USER_AGENT})
        return self._check(r, "POST", path)

    def delete(self, path):
        params = self._signed_params("DELETE", path, "")
        r = requests.delete(BASE + path, params=params, timeout=30,
                            headers={"User-Agent": caltopo_live.USER_AGENT})
        return self._check(r, "DELETE", path)

    @staticmethod
    def _check(r, method, path):
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not r.ok or body.get("status") not in (None, "ok"):
            raise RuntimeError("%s %s failed: HTTP %s %s"
                               % (method, path, r.status_code, r.text[:300]))
        return body.get("result")


def ensure_folder(sess, map_id, features):
    """Find-or-create our AUTO folder; returns its feature id."""
    for f in features:
        p = f.get("properties", {})
        if p.get("class") == "Folder" and p.get("title") == caltopo_live.OWN_FOLDER_TITLE:
            return f["id"]
    result = sess.post("/api/v1/map/%s/Folder" % map_id,
                       {"properties": {"title": caltopo_live.OWN_FOLDER_TITLE,
                                       "visible": True, "labelVisible": True}})
    return result["id"]


def sync(map_id, import_path, dry_run=False):
    features, _ = caltopo_live.fetch_features(map_id)
    with open(import_path) as f:
        feats = json.load(f)["features"]
    feats = [f for f in feats
             if not f["properties"].get("title", "").startswith("DONE")]

    if dry_run:
        own = [f for f in features if f.get("properties", {}).get("folderId")
               and f.get("properties", {}).get("class") != "Folder"]
        print("DRY RUN: would ensure folder %r, would post %d features to map %s"
              % (caltopo_live.OWN_FOLDER_TITLE, len(feats), map_id))
        for f in feats:
            print("  +", f["properties"].get("class"), f["properties"].get("title"))
        return

    sess = Session()
    folder_id = ensure_folder(sess, map_id, features)

    deleted = 0
    for f in features:
        p = f.get("properties", {})
        if p.get("folderId") == folder_id and p.get("class") in ("Marker", "Shape"):
            sess.delete("/api/v1/map/%s/%s/%s" % (map_id, p["class"], f["id"]))
            deleted += 1

    created = 0
    for f in feats:
        f["properties"]["folderId"] = folder_id
        sess.post("/api/v1/map/%s/%s" % (map_id, f["properties"]["class"]), f)
        created += 1
    print("caltopo_write: map %s — deleted %d stale, created %d features in %r"
          % (map_id, deleted, created, caltopo_live.OWN_FOLDER_TITLE))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if len(args) != 2:
        sys.exit("usage: python3 caltopo_write.py MAP_ID CALTOPO_IMPORT.json [--dry-run]")
    sync(args[0], args[1], dry_run="--dry-run" in sys.argv)
