"""Hiding-spot signal layers. Each function takes the shared ctx dict and returns
a 0..1 float array on the DEM grid. Add a new signal here + register it in
score.SIGNALS with a weight in config.WEIGHTS — nothing else to touch.

ctx keys: dem, masks {buildings, roads, water, veg} (bool arrays),
          center_rc (row, col of last-seen point), res (m/px)
"""
import numpy as np
from scipy import ndimage
from skimage.graph import MCP_Geometric

import config


def dilate(mask, meters, res):
    """Grow a boolean mask outward by `meters`."""
    if not mask.any():
        return mask
    return ndimage.distance_transform_edt(~mask) * res <= meters


def _norm_p95(a):
    """Clip negatives, scale by the 95th percentile of positive values."""
    a = np.clip(a, 0, None)
    pos = a[a > 0]
    if pos.size == 0:
        return np.zeros_like(a)
    return np.clip(a / np.percentile(pos, 95), 0, 1)


def concavity(ctx):
    """Multi-scale negative TPI: local lows, gullies, drainage bottoms."""
    dem = ctx["dem"]
    score = np.zeros_like(dem)
    for s in config.TPI_SCALES:
        tpi = dem - ndimage.uniform_filter(dem, size=int(s / ctx["res"]))
        score += _norm_p95(-tpi)
    return score / len(config.TPI_SCALES)


def depression(ctx):
    """Closed depressions / culvert-like pockets via morphological closing."""
    dem = ctx["dem"]
    size = int(config.DEPRESSION_SIZE / ctx["res"])
    return _norm_p95(ndimage.grey_closing(dem, size=(size, size)) - dem)


def flat_bench(ctx):
    """Low-slope spots — dogs bed down on benches/flats, not cliff walls."""
    gy, gx = np.gradient(ctx["dem"], ctx["res"])
    slope_deg = np.degrees(np.arctan(np.hypot(gx, gy)))
    return np.exp(-slope_deg / config.SLOPE_EFOLD_DEG)


def vegetation(ctx):
    """Dense cover from OSM wood/scrub/forest polygons, softened at edges."""
    m = ctx["masks"]["veg"].astype("float64")
    return np.clip(ndimage.gaussian_filter(m, sigma=config.VEG_BLUR_M / ctx["res"]), 0, 1)


def water(ctx):
    """Proximity to streams/ponds (exponential decay)."""
    m = ctx["masks"]["water"]
    if not m.any():
        return np.zeros_like(ctx["dem"])
    dist = ndimage.distance_transform_edt(~m) * ctx["res"]
    return np.exp(-dist / config.WATER_EFOLD_M)


def road_quiet(ctx):
    """Mild preference for areas away from motor roads."""
    m = ctx["masks"]["roads"]
    if not m.any():
        return np.ones_like(ctx["dem"])
    dist = ndimage.distance_transform_edt(~m) * ctx["res"]
    return np.clip(dist / config.ROAD_SATURATE_M, 0, 1)


def movement_cost(ctx):
    """Resistance surface for the animal-movement model — per-meter 'effort' to
    travel through each cell. This is the connectivity/least-cost-path framework
    (cf. Circuitscape). A scared dog:
      * follows CORRIDORS (rail beds, trails) — low resistance, so probability
        propagates FAR along them (rail lines are documented travel routes);
      * avoids climbing — resistance rises with slope (valley bottoms are cheap);
      * will not cross BUSY ROADS — near-impassable barrier resistance;
      * BUT the rail line is grade-separated (bridges/underpasses, e.g. the
        Allens Lane underpass) so the TRACK stays a continuous corridor UNDER
        the roads — rail is applied LAST so it punches through barriers.
    Add a movement factor here to extend the model (fences, streams as barriers,
    open-ground aversion, etc.)."""
    res = ctx["res"]
    gy, gx = np.gradient(ctx["dem"], res)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    cost = 1.0 + config.SLOPE_COST_PER_DEG * slope        # terrain effort
    # trails: easy going, but a trail crossing a road is at grade -> yields to
    # the barrier below
    trails = dilate(ctx["masks"]["trails"], config.TRAIL_BUFFER_M, res)
    cost[trails] = np.minimum(cost[trails], config.TRAIL_TRAVEL_COST)
    # busy trails (people pressure): NOT a cheap corridor for a scared dog —
    # crossable, mildly averted, overriding the quiet-trail discount above
    busy = ctx["masks"].get("busy_trails")
    if busy is not None and busy.any():
        busy = dilate(busy, config.TRAIL_BUFFER_M, res)
        cost[busy] = np.maximum(cost[busy], config.BUSY_TRAIL_TRAVEL_COST)
    # busy roads: near-impassable to a scared dog (blocks open ground & trails)
    barrier = dilate(ctx["masks"]["barriers"], config.BARRIER_BUFFER_M, res)
    cost[barrier] = config.BARRIER_COST
    # rail corridor LAST: grade-separated, so the dog follows the track straight
    # under the road via the underpass — a continuous low-cost slot through the
    # barrier (only the narrow rail ribbon, not the whole road, becomes passable)
    rails = dilate(ctx["masks"]["rails"], config.RAIL_BUFFER_M, res)
    cost[rails] = config.RAIL_TRAVEL_COST
    return cost


def _cell_bearings(ctx):
    """Compass bearing (deg, 0=N) of every cell as seen from the last-seen
    point, plus straight-line distance (m). Row 0 is the north edge."""
    r0, c0 = ctx["center_rc"]
    rows, cols = np.indices(ctx["dem"].shape)
    theta = np.degrees(np.arctan2(cols - c0, r0 - rows)) % 360
    dist = np.hypot(rows - r0, cols - c0) * ctx["res"]
    return theta, dist


def direction_weight(ctx):
    """Flight-bearing cone: de-prioritize (never mask) directions away from the
    observed heading. Fades to neutral near the origin — a bearing says nothing
    about the immediate vicinity, and dogs loop back."""
    b = ctx.get("bearing_deg")
    if b is None:
        return np.ones_like(ctx["dem"])
    theta, dist = _cell_bearings(ctx)
    kappa = config.FLIGHT_KAPPA_CHASED if ctx.get("chased") else config.FLIGHT_KAPPA
    cosd = np.cos(np.radians(theta - b))
    w = config.FLIGHT_FLOOR + (1 - config.FLIGHT_FLOOR) * np.exp(kappa * (cosd - 1))
    ramp = np.clip(dist / config.FLIGHT_NEAR_FADE_M, 0, 1)
    return 1 + ramp * (w - 1)


def _bearing_stretch(ctx):
    """A chased/panicked bolt overshoots the median find-distance ALONG the
    flight bearing: stretch the dispersion e-fold in that half-plane only."""
    b = ctx.get("bearing_deg")
    if b is None or not ctx.get("chased"):
        return 1.0
    theta, _ = _cell_bearings(ctx)
    ahead = np.clip(np.cos(np.radians(theta - b)), 0, 1)
    return 1 + (config.CHASED_RING_STRETCH - 1) * ahead


def prior(ctx):
    """Reachability from the last-seen point under the movement model: accumulate
    least-cost 'travel effort' outward, then decay EXPONENTIALLY. Exponential (not
    Gaussian) decay is deliberate: each barrier crossing adds a fixed effort and so
    dims reachability by a constant multiplicative factor (a smooth step), instead
    of the Gaussian-of-cum² cliff that snapped reach to 0 and drew hard-edged
    wedges along every road. Corridors stretch the envelope; barriers dim it.
    The flight-bearing cone and (for a chased bolt) the along-bearing stretch of
    the dispersion ring are folded in here so every consumer of the prior —
    static POA and diffusion snapshots alike — sees the same directional field."""
    mcp = MCP_Geometric(movement_cost(ctx), sampling=(ctx["res"], ctx["res"]))
    cum, _ = mcp.find_costs([ctx["center_rc"]])
    finite = np.isfinite(cum)
    cum = np.where(finite, cum, cum[finite].max())
    return np.exp(-cum / (config.PRIOR_DECAY_M * _bearing_stretch(ctx))) \
        * direction_weight(ctx)


def downhill(ctx):
    """Scared dogs take the path of least resistance — mild preference for
    terrain below the last-seen elevation (sigmoid, 0.5 at same elevation)."""
    r0, c0 = ctx["center_rc"]
    rel = ctx["dem"] - ctx["dem"][r0, c0]  # positive = above last-seen
    return 1.0 / (1.0 + np.exp(rel / config.DOWNHILL_EFOLD_M))
