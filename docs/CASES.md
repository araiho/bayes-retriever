# Case files & CalTopo conventions

One lost dog = one JSON file in `cases/`. Everything the pipeline needs comes
from that file plus the live CalTopo map it points at. Copy
`cases/TEMPLATE.json` to start a new case; the authoritative field reference is
the docstring at the top of [`casefile.py`](../casefile.py).

## Case-file fields

| field | meaning |
|---|---|
| `name` | Dog's name (shown on the site). |
| `temperament` | `gregarious` / `aloof` / `xenophobic` — sets dispersion ring, mobility, and signal weights (see `TEMPERAMENTS` in `config.py`). Shy/scared dogs are `xenophobic`. |
| `median_found_mi` | Optional. Median find distance in miles (literature/evidence); overrides the temperament's 50%-ring. |
| `chased` | `true` if the dog bolted in a panic (chased, fireworks): sharpens and stretches the flight-direction cone. |
| `lost_since` | ISO datetime (with UTC offset) of the escape **or the latest confirmed sighting**. Drives the diffusion clock and the settled-phase behavior switch. |
| `busy_trails` | OSM way names (e.g. `"Forbidden Drive"`) with heavy people/dog/bike traffic — treated as people-pressure, not habitat. |
| `home` | Optional `{lat, lon}` of the dog's home (useful when lost elsewhere, e.g. by a sitter). Adds a weak homeward pull — brightens ground toward home without masking the far side. |
| `last_seen` | `{lat, lon, bearing_deg, note}`. Explicit values override anything parsed from the map. `bearing_deg` = compass direction the dog was moving (0 = north), `null` if unknown. |
| `search_log.import` | `caltopo:<MAPID>` for a live shared CalTopo map (preferred), or a path to a GeoJSON export. |
| `search_log.default_pod` | Probability a searcher would have detected the dog if present (default 0.4). |
| `search_log.default_radius_m` | Effective sweep radius of one logged visit (default 60 m). |
| `searched` | Extra effort not on the map: `[{lat, lon, pod, radius_m, note}]` (e.g. the owner's continuously-monitored yard). |

## CalTopo marker conventions (what searchers do)

The shared CalTopo map is the **only** tool searchers need. Every marker is
automatically timestamped and attributed by CalTopo.

| marker | effect |
|---|---|
| Point titled with **search / check / looked / visited** (e.g. `Searched spot 14`) | Logged as search effort — that area is probability-discounted on the next update. |
| Point titled `SIGHTING <when/what>` | Shown on the site as **unconfirmed** — never moves the model on its own. |
| Point retitled `SIGHTING CONFIRMED <...>` | If newer than the case's `lost_since`, becomes the new last-seen and resets the clock. Include `running <degrees>` in the title to set the flight bearing. |
| Point titled `last seen` | Last-seen candidate (newest one wins; the case file's explicit `last_seen` overrides). |
| LineString titled `last seen running...` | Flight bearing, from the line's first to last point. |

**Bot-owned features** (never rename/move; the pipeline rewrites them):
`NEXT n` markers, `DONE ...` markers, the `NN% probability zone` polygons, the
`50% find ring`, the `seen running NNN deg` line, and anything inside the
`AUTO dog-finder` folder.

## Sighting confirmation policy

A wrong last-seen silently redirects the whole search, so adoption is
deliberately manual: an admin verifies the report (talk to the witness, check
photos/direction), then retitles the marker to `SIGHTING CONFIRMED ...`. For a
precise time/coordinate, or to undo a bad confirmation, edit the case file's
`last_seen` + `lost_since` directly and push — the explicit case file always
wins over anything older.
