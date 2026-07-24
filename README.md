# bayes-retriever 🐕 — live lost-dog search prioritization

*It's Bayesian, and it retrieves dogs.*

Builds a **prioritized search map** for a missing dog from the last-seen
coordinate using only free data (USGS 3DEP 1m LiDAR + OpenStreetMap), and keeps
it **online and auto-updating** as the search team logs effort and sightings.

## Architecture (~the whole system)

```
searchers' phones                 GitHub Actions (every ~30 min)         anyone
      │                                     │                              │
      ▼                                     ▼                              ▼
shared CalTopo map  ──live API──▶  pull + fingerprint check   ──▶  GitHub Pages site
(markers: searched /               changed? rerun model:           (interactive map,
 SIGHTING, timestamped             terrain + movement + Bayesian    next-spots table,
 & attributed by CalTopo)          effort discounting               search log, GPX)
      ▲                                     │
      └────── optional signed write-back ───┘
              (zones + NEXT markers, CALTOPO_WRITE=1)
```

- **CalTopo is the multi-user entry layer**: every marker a searcher drops is
  timestamped and attributed automatically. Conventions in
  [docs/CASES.md](docs/CASES.md).
- **CI is the compute layer**: `.github/workflows/update.yml` pulls the live
  map, skips the run if nothing human-made changed (fingerprint vs. the
  deployed `status.json`), otherwise reruns `run.py` and redeploys Pages
  atomically (a failed run leaves the previous site up).
- **Pages is the read layer**: `output/<case>/index.html` wraps the folium map
  with the hotspot table, timestamped search log, sighting alerts, downloads,
  and searcher instructions.

## Local dev quickstart

```bash
pip install -r requirements.txt
python3 run.py cases/maisy.json        # full pipeline, ~7 s warm
python3 -m pytest tests/               # parsing/filtering regression tests
python3 caltopo_live.py F0M58FH        # inspect the live map partition
open output/maisy/index.html
```

Data caches live in `data/<case>/` with the coordinate in each filename — no
manual cache clearing when a coordinate changes, ever.

## Start a new case (the runbook)

1. Make a shared CalTopo map for the search team (share: anyone with the URL).
2. `cp cases/TEMPLATE.json cases/<dog>.json` and fill in `last_seen`,
   `lost_since`, `temperament`, and `search_log.import: "caltopo:<MAPID>"`
   (field reference: [docs/CASES.md](docs/CASES.md)).
3. Edit the one `CASE:` line at the top of `.github/workflows/update.yml`.
4. Commit + push. That's it — the site rebuilds on push and then follows the
   map every ~30 min.

## Ops recipes

- **Force a rebuild now**: Actions → update-map → Run workflow.
- **Confirm a sighting**: retitle its CalTopo marker to
  `SIGHTING CONFIRMED ...` (see the policy in docs/CASES.md).
- **Correct the last-seen / timeline**: edit the case JSON and push.
- **Enable CalTopo write-back** (zones + NEXT markers pushed into the team
  map): mint a personal credential (docstring of `caltopo_write.py`), test
  against a **throwaway map**, add repo secrets `CALTOPO_CREDENTIAL_ID` /
  `CALTOPO_KEY`, set repo variable `CALTOPO_WRITE=1`. One-time: delete any
  previously hand-imported NEXT/DONE/zone features from the team map.
- **Tuning**: model knobs are documented inline in `config.py`
  (temperaments, movement costs, diffusion, POD defaults).

## Repo layout

| file | role |
|---|---|
| `run.py` | end-to-end pipeline entrypoint |
| `casefile.py` | case JSON + search-log/sighting resolution |
| `caltopo_live.py` | live CalTopo pull, own-feature filtering, change fingerprint |
| `caltopo_write.py` | optional signed write-back (feature-flagged) |
| `fetch.py` | DEM (USGS 3DEP) + OSM (Overpass), per-case cached |
| `layers.py` / `diffusion.py` / `score.py` | terrain signals, movement resistance, time diffusion, POA + Bayesian effort discounting |
| `make_map.py` | map/file outputs (folium HTML, PNG, GeoTIFF, GPX/CSV, CalTopo import) |
| `make_site.py` | the public Pages page + `status.json` |
| `cases/` | one JSON per dog (`TEMPLATE.json` to copy) |
| `.github/workflows/update.yml` | the auto-update loop |

## How the model works

`fetch.py` (DEM + OSM + passages) → `layers.py` (hiding-spot signals +
movement-resistance reachability with flight-direction cone) → `diffusion.py`
(time-diffusion probable zone) → `score.py` (habitat × reachability →
Probability of Area; Koopman-style discounting of logged search effort;
greedy-peel ranking of next segments) → `make_map.py` / `make_site.py`.
Grounded in SAR search theory (POC/POA/POD) and lost-pet behavior literature
(shy dogs settle into a small range near water/cover; sightings > mass
grid-searching; no calling/chasing).

Note: everything in this repo (and on the CalTopo map it reads) is public.
