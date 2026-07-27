"""Public site page (GitHub Pages): a phone-friendly index.html wrapping the
interactive map, plus machine-readable status.json (the CI change check reads
its fingerprint). Plain strings-in-Python on purpose — no build step."""
import html
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import make_map

LOCAL_TZ = ZoneInfo("America/New_York")

_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     margin:0;background:#f5f6fa;color:#2d3436}
header{background:#2d3436;color:#fff;padding:14px 18px}
header h1{margin:0 0 4px;font-size:1.4em}
header .sub{color:#b2bec3;font-size:.9em}
a{color:#0984e3}
header a{color:#74b9ff}
section{margin:14px auto;max-width:1000px;padding:0 12px}
h2{font-size:1.05em;border-bottom:2px solid #dfe6e9;padding-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:.9em;background:#fff}
th,td{border:1px solid #dfe6e9;padding:6px 8px;text-align:left}
th{background:#dfe6e9}
tr:nth-child(even){background:#fafbfc}
iframe{width:100%;height:72vh;border:1px solid #b2bec3;border-radius:4px;
       background:#fff}
.alert{background:#d63031;color:#fff;padding:12px 16px;border-radius:6px;
       margin:14px auto;max-width:1000px;font-weight:600}
.alert a{color:#ffeaa7}
.note{color:#636e72;font-size:.85em}
.dl a{display:inline-block;background:#0984e3;color:#fff;padding:8px 12px;
      border-radius:5px;margin:3px 6px 3px 0;text-decoration:none;font-size:.9em}
code{background:#dfe6e9;padding:1px 5px;border-radius:3px;font-size:.9em}
li{margin:5px 0}
"""


def _fmt_t(t_ms):
    if not t_ms:
        return "&mdash;"
    t = datetime.fromtimestamp(t_ms / 1000, tz=LOCAL_TZ)
    return t.strftime("%a %b %-d, %-I:%M %p ET")


def _hotspot_rows(hotspots):
    rows = []
    for i, h in enumerate(hotspots, 1):
        top = sorted(h["breakdown"].items(), key=lambda kv: -kv[1])[:3]
        why = ", ".join("%s %.2f" % kv for kv in top)
        eta = ("~%dh" % h["eta_h"]) if h.get("eta_h") else "far"
        rows.append(
            "<tr><td>%d</td><td>%.1f%%</td><td><code>%.6f, %.6f</code></td>"
            "<td>%s</td><td>%s</td></tr>"
            % (i, 100 * h.get("prob", h.get("score", 0)), h["lat"], h["lon"],
               eta, html.escape(why)))
    return "\n".join(rows)


def _log_rows(searched):
    rows = []
    for s in sorted(searched, key=lambda s: -(s.get("t_ms") or 0)):
        note = html.escape(s.get("note", "searched"))
        if s.get("stale"):
            note += " <i>(before latest sighting &mdash; not counted)</i>"
        rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%.0f%%</td><td>%.0f m</td></tr>"
            % (_fmt_t(s.get("t_ms")), note,
               html.escape(s.get("creator") or "&mdash;", quote=False),
               100 * s["pod"], s["radius_m"]))
    return "\n".join(rows)


def _sighting_items(sightings):
    items = []
    for s in sorted(sightings, key=lambda s: -(s.get("t_ms") or 0)):
        tag = ("&#9989; CONFIRMED" if s["confirmed"]
               else "&#9888;&#65039; unconfirmed")
        items.append("<li>%s &mdash; <b>%s</b> (%s) at <code>%.6f, %.6f</code></li>"
                     % (_fmt_t(s.get("t_ms")), html.escape(s["title"]), tag,
                        s["lat"], s["lon"]))
    return "\n".join(items)


def save_site(case, hotspots, searched=None, covered=None):
    """Write OUT_DIR/index.html + status.json, plus the output-root redirect
    page and .nojekyll. Call after the other make_map writers (hotspots need
    their lat/lon filled in by save_html)."""
    out = make_map.OUT_DIR
    root = os.path.dirname(out)
    os.makedirs(out, exist_ok=True)
    searched = searched or []
    sightings = case.get("sightings") or []
    unconfirmed = [s for s in sightings if not s["confirmed"]]
    now_utc = datetime.now(timezone.utc)
    name = html.escape(case.get("name", case.get("slug", "?")))
    map_url = ("https://caltopo.com/m/%s" % case["map_id"]
               if case.get("map_id") else None)

    banner = ""
    if unconfirmed:
        banner = ('<div class="alert">&#9888;&#65039; %d UNCONFIRMED sighting%s '
                  'reported — see the sightings list below. An admin confirms one '
                  'by retitling its CalTopo marker to '
                  '"SIGHTING CONFIRMED ...".</div>'
                  % (len(unconfirmed), "s" if len(unconfirmed) > 1 else ""))

    hours = case.get("hours_missing")
    missing = ("missing ~%.0f h (%.1f days)" % (hours, hours / 24)) if hours else ""
    covered_txt = (" &middot; ~%.0f%% of probability mass already searched"
                   % (100 * covered)) if covered is not None else ""
    caltopo_link = (' &middot; <a href="%s">team CalTopo map</a>' % map_url
                    if map_url else "")

    how_to_help = """
<h2>How to help (searchers)</h2>
<ol>
<li>All coordination happens on the <a href="{map_url}">shared CalTopo map</a>
    &mdash; open it on your phone (works in the browser; the CalTopo app is
    nicer).</li>
<li><b>After you search an area</b>, drop a marker there titled with the word
    <i>searched</i> or <i>checked</i> (e.g. <code>Searched spot 14</code>,
    <code>checked creek bed under bridge</code>). It is timestamped
    automatically and this page + the model update within ~30&nbsp;minutes:
    your area is discounted and the next-best spots recomputed.</li>
<li><b>If you see the dog</b>, do NOT chase or call &mdash; drop a marker titled
    <code>SIGHTING</code> plus when/what you saw (e.g. <code>SIGHTING 7:15am
    trotting north on rail line</code>). If you saw the travel direction, include
    <code>running &lt;degrees&gt;</code>. A search admin will confirm it.</li>
<li>Please don't rename or move the <code>NEXT n</code> / <code>DONE</code>
    markers or the colored probability zones &mdash; the model owns those and
    rewrites them on every update.</li>
</ol>
""".format(map_url=map_url or "#")

    page = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Find {name} &mdash; live search map</title>
<style>{css}</style>
</head><body>
<header>
<h1>Find {name}</h1>
<div class="sub">{missing}{covered_txt}{caltopo_link}<br>
Updated {upd_et} ({upd_utc} UTC) &mdash; auto-refreshes every ~30 min from the
team CalTopo map</div>
</header>
{banner}
<section>
<h2>Where to look next</h2>
<p class="note">Heat = probability the dog is there NOW, given everywhere
already searched came up empty. Toggle layers (satellite, time zones, searched
areas) with the control in the top right of the map.</p>
<iframe src="search_map.html" title="interactive search map"></iframe>
</section>
<section>
<h2>Top next-search segments</h2>
<table>
<tr><th>#</th><th>P(dog)</th><th>lat, lon</th><th>reach by</th><th>why</th></tr>
{hotspot_rows}
</table>
</section>
<section>
<h2>Sightings</h2>
{sightings_block}
</section>
<section>
<h2>Search log ({n_searched} entries, newest first)</h2>
<table>
<tr><th>when</th><th>entry</th><th>by</th><th>POD</th><th>sweep</th></tr>
{log_rows}
</table>
<p class="note">"by" is the CalTopo account id of whoever dropped the marker.
POD = probability the dog would have been detected if it was there.</p>
</section>
<section class="dl">
<h2>Downloads</h2>
<a href="search_spots.gpx" download>GPX waypoints</a>
<a href="search_spots.csv" download>CSV spot list</a>
<a href="caltopo_import.json" download>CalTopo import file</a>
<a href="priority.tif" download>GeoTIFF raster</a>
<a href="search_zones_time.png">Zone growth over time</a>
<p class="note">CalTopo import: on the map, Add &gt; Import, pick the file.
Delete the previous import's zones/markers first so they don't stack up.</p>
</section>
<section>
{how_to_help}
<p class="note">Model: terrain (USGS 1m LiDAR) + OSM habitat/movement layers +
lost-pet behavior literature; Bayesian discounting of logged search effort.
Generated by the <a href="https://github.com/araiho/bayes-retriever">bayes-retriever</a> pipeline.</p>
</section>
</body></html>
""".format(
        name=name, css=_CSS, missing=missing, covered_txt=covered_txt,
        caltopo_link=caltopo_link,
        upd_et=now_utc.astimezone(LOCAL_TZ).strftime("%a %b %-d, %-I:%M %p ET"),
        upd_utc=now_utc.strftime("%Y-%m-%d %H:%M"),
        banner=banner, hotspot_rows=_hotspot_rows(hotspots),
        sightings_block=("<ul>%s</ul>" % _sighting_items(sightings)
                         if sightings else
                         '<p class="note">None reported on the map yet.</p>'),
        n_searched=len(searched), log_rows=_log_rows(searched),
        how_to_help=how_to_help)

    index = os.path.join(out, "index.html")
    with open(index, "w") as f:
        f.write(page)

    status = {
        "fingerprint": case.get("fingerprint"),
        "generated_utc": now_utc.isoformat(),
        "case": case.get("name"), "slug": case.get("slug"),
        "map_id": case.get("map_id"),
        "hours_missing": hours, "covered": covered,
        "n_searched": len(searched), "n_hotspots": len(hotspots),
        "n_sightings": len(sightings), "n_unconfirmed": len(unconfirmed),
    }
    with open(os.path.join(out, "status.json"), "w") as f:
        json.dump(status, f, indent=1)

    slug = case.get("slug", "adhoc")
    with open(os.path.join(root, "index.html"), "w") as f:
        f.write('<!DOCTYPE html><html><head><meta charset="utf-8">'
                '<meta http-equiv="refresh" content="0; url=%s/">'
                '<title>dog-finder</title></head>'
                '<body><a href="%s/">Open the %s search map</a></body></html>'
                % (slug, slug, name))
    open(os.path.join(root, ".nojekyll"), "w").close()
    return index
