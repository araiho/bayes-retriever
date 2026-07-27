"""Combine signal layers into the priority raster + extract ranked hotspots.

Two separable pieces:
  * habitat_base()  -> WHERE a dog could hide (terrain/veg/water + suppression)
  * a reachability gate -> WHERE it could be by now (static movement prior, OR a
                           time-diffusion snapshot from diffusion.py)
priority = habitat_base gated by reachability. Keeping them separate lets the
time-diffusion reuse the exact same habitat map at every horizon.
"""
import numpy as np
from scipy import ndimage

import config
import layers

# Extension point: add (name, function) here + a weight in config.WEIGHTS.
SIGNALS = {
    "concavity":  layers.concavity,
    "depression": layers.depression,
    "flat_bench": layers.flat_bench,
    "vegetation": layers.vegetation,
    "water":      layers.water,
    "road_quiet": layers.road_quiet,
    "downhill":   layers.downhill,
}


def _suppression(ctx):
    """Multiplicative knock-downs for places the dog can't hide / isn't a hiding
    spot: inside buildings, in the water, on trail/road/rail beds."""
    res = ctx["res"]
    m = np.ones_like(ctx["dem"])
    m[ctx["masks"]["buildings"]] *= config.BUILDING_MULTIPLIER
    m[layers.dilate(ctx["masks"]["water"], config.WATER_EXCLUDE_BUFFER_M, res)] \
        *= config.WATER_MULTIPLIER
    m[layers.dilate(ctx["masks"]["trails"], config.TRAIL_BUFFER_M, res)] \
        *= config.TRAIL_MULTIPLIER
    m[layers.dilate(ctx["masks"]["roads"], config.ROAD_BUFFER_M, res)] \
        *= config.ROAD_MULTIPLIER
    m[layers.dilate(ctx["masks"]["rails"], config.RAIL_BUFFER_M, res)] \
        *= config.RAIL_MULTIPLIER
    # people pressure: hiding near a BUSY trail is de-prioritized (never
    # masked), ramping back to neutral by PEOPLE_PRESSURE_M
    busy = ctx["masks"].get("busy_trails")
    if busy is not None and busy.any():
        dist = ndimage.distance_transform_edt(~busy) * res
        floor = config.PEOPLE_PRESSURE_FLOOR
        m *= floor + (1 - floor) * np.clip(dist / config.PEOPLE_PRESSURE_M, 0, 1)
    return m


def habitat_base(ctx):
    """WHERE a dog could hide, independent of how far it has traveled.
    Returns (base 0..1 array, {signal: layer})."""
    stack = {}
    total = np.zeros_like(ctx["dem"])
    wsum = 0.0
    for name, fn in SIGNALS.items():
        w = config.WEIGHTS[name]
        layer = fn(ctx)
        stack[name] = layer
        total += w * layer
        wsum += w
    base = (total / wsum) * _suppression(ctx)
    return base, stack


def combine_display(base, reach):
    """Habitat × reachability, scaled to 0..1 for rendering (not a probability)."""
    x = base * reach
    return x / (x.max() + 1e-9)


def poa_and_display(base, prior):
    """Probability of Area (SAR): POA = habitat × reachability, normalized so it
    integrates to 1 (a real probability distribution over the box). Returns
    (display 0..1 for rendering, poa summing to 1 for ranking). The display is
    scaled to a high percentile with a gamma lift (not the single max) so the
    whole hot zone is visible instead of one bright pixel."""
    dens = base * prior
    poa = dens / (dens.sum() + 1e-12)
    pos = dens[dens > 0]
    hi = np.percentile(pos, 99) if pos.size else 1.0
    display = np.clip(dens / (hi + 1e-12), 0, 1) ** 0.6
    return display, poa


def search_discount(ctx, now_ms=None):
    """Koopman posterior update for effort already spent: each logged search
    visit multiplies the local probability by (1 - POD), with a Gaussian
    detection kernel of the visit's sweep radius. Old effort decays (the dog
    moves; a spot cleared two days ago isn't cleared now). Returns the 0..1
    'unsearched-ness' field; posterior = POA * this, renormalized."""
    m = np.ones_like(ctx["dem"])
    H, W = m.shape
    for s in ctx.get("searched", []):
        if s.get("stale"):     # visit predates the last sighting
            continue
        r, c = s["row"], s["col"]
        if not (0 <= r < H and 0 <= c < W):
            continue
        pod = s["pod"]
        if now_ms and s.get("t_ms"):
            age_h = max(0.0, (now_ms - s["t_ms"]) / 3.6e6)
            pod *= np.exp(-age_h / config.SEARCH_DECAY_H)
        rad_px = s["radius_m"] / ctx["res"]
        k = int(3 * rad_px)
        r0, r1 = max(0, r - k), min(H, r + k + 1)
        c0, c1 = max(0, c - k), min(W, c + k + 1)
        rr, cc = np.ogrid[r0:r1, c0:c1]
        d2 = (rr - r) ** 2 + (cc - c) ** 2
        m[r0:r1, c0:c1] *= 1 - pod * np.exp(-0.5 * d2 / rad_px ** 2)
    return m


def posterior(poa, discount):
    """(posterior summing to 1, display 0..1, fraction of probability mass the
    logged effort has already 'consumed')."""
    dens = poa * discount
    covered = 1.0 - float(dens.sum() / (poa.sum() + 1e-12))
    post = dens / (dens.sum() + 1e-12)
    pos = dens[dens > 0]
    hi = np.percentile(pos, 99) if pos.size else 1.0
    display = np.clip(dens / (hi + 1e-12), 0, 1) ** 0.6
    return post, display, covered


def find_hotspots(poa, stack, ctx):
    """Rank search segments by Probability of Area MASS via greedy peel (SAR:
    allocate effort by contained probability). Repeatedly take the segment
    holding the most probability, then suppress its footprint so the next pick
    doesn't overlap — yields the top-N non-overlapping segments, ranked, each
    with 'prob' = P(dog within ~SEGMENT_RADIUS_M of the point)."""
    res = ctx["res"]
    R = int(config.SEGMENT_RADIUS_M / res)
    win = 2 * R + 1
    mass = ndimage.uniform_filter(poa, size=win, mode="constant") * (win * win)
    work = mass.copy()
    sep = int(config.HOTSPOT_MIN_SEP_M / res)
    H, W = mass.shape
    rows, cols = np.indices(mass.shape)
    # only pin on genuine hiding ground (not roads/water/trail beds/buildings)
    pinnable = _suppression(ctx) > 0.5
    poa_pin = poa * pinnable
    picked = []
    for _ in range(config.N_HOTSPOTS):
        idx = int(np.argmax(work))
        r, c = idx // W, idx % W
        if work[r, c] <= 0:
            break
        # snap the pin to the best actual hiding cell inside the segment
        r0, r1, c0, c1 = max(0, r - R), min(H, r + R + 1), max(0, c - R), min(W, c + R + 1)
        sub = poa_pin[r0:r1, c0:c1]
        pr, pc = (r, c) if sub.max() <= 0 else (
            r0 + int(np.argmax(sub)) // sub.shape[1], c0 + int(np.argmax(sub)) % sub.shape[1])
        picked.append({
            "row": pr, "col": pc,
            "prob": float(mass[r, c]),        # P(dog within this segment)
            "score": float(mass[r, c]),       # kept for map/label compatibility
            "breakdown": {k: float(v[pr, pc]) for k, v in stack.items()
                          if k in config.WEIGHTS or k == "prior"},
        })
        # suppress the segment footprint (mass) AND the chosen pin's footprint
        # (pinnable POA) so the next segment neither overlaps nor re-picks the
        # same hiding cell
        work[(rows - r) ** 2 + (cols - c) ** 2 <= sep * sep] = 0
        poa_pin[(rows - pr) ** 2 + (cols - pc) ** 2 <= sep * sep] = 0
    return picked
