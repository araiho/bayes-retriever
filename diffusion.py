"""Time-diffusion of the dog's probable location on the movement resistance
surface. Solves the variable-coefficient diffusion equation

        dP/dt = div( D(x) grad P ),     D(x) = MOBILITY / resistance(x)

with a delta of probability at the last-seen point at t=0. Low-resistance
corridors (rail/trail) have high diffusivity, so probability mass flows fast &
far along them; busy-road barriers have near-zero diffusivity and hold mass back.
This is the diffusion/heat-equation view of animal movement (a continuous cousin
of the least-cost `prior`): it shows the probable zone GROWING over time.

Run on a coarsened grid (~15 m) for speed — the probable-zone envelope doesn't
need 1 m precision; the 1 m hiding-spot detail comes from the habitat layers.
Snapshots are returned at each requested horizon (hours), upsampled back to 1 m.
"""
import numpy as np
from scipy import ndimage

import config
import layers


def _block_reduce(a, f, op):
    """Downsample by integer factor f using op ('mean'|'min'|'max')."""
    h, w = a.shape
    a = a[:h // f * f, :w // f * f].reshape(h // f, f, w // f, f)
    return getattr(a, op)(axis=(1, 3))


def diffuse(ctx, hours):
    """Return {h: prob_1m_array} for each horizon h in `hours` (each normalized
    so its own max = 1). Also returns the coarse cell size used, for reporting."""
    res = ctx["res"]
    f = max(1, int(round(config.DIFFUSION_COARSEN_M / res)))
    dx = f * res

    cost = layers.movement_cost(ctx)
    # Coarsen so corridors survive (min keeps thin low-cost ribbons) but barriers
    # are never lost (re-stamp any block touching a barrier as impassable).
    cost_c = _block_reduce(cost, f, "min")
    barrier = layers.dilate(ctx["masks"]["barriers"], config.BARRIER_BUFFER_M, res)
    barrier_c = _block_reduce(barrier.astype(np.float32), f, "max") > 0
    cost_c[barrier_c] = config.BARRIER_COST
    # rail corridor punches through barriers (grade-separated underpasses) — the
    # track stays passable under the roads, matching movement_cost's ordering.
    rails = layers.dilate(ctx["masks"]["rails"], config.RAIL_BUFFER_M, res)
    rail_c = _block_reduce(rails.astype(np.float32), f, "max") > 0
    cost_c[rail_c] = config.RAIL_TRAVEL_COST

    r0, c0 = ctx["center_rc"]
    # The last-seen point is reachable by definition — don't let its coarse cell
    # read as a barrier just because a road clips the block (the dog was beside,
    # not on, the road). Seed it as passable ground.
    src_cost = 1.0 if barrier[r0, c0] else min(cost[r0, c0], 1.0)
    cost_c[r0 // f, c0 // f] = min(cost_c[r0 // f, c0 // f], src_cost)

    D = config.DIFFUSION_MOBILITY / cost_c          # diffusivity, m^2/h
    # Face diffusivities via harmonic mean (correct across sharp contrasts).
    def hmean(a, b):
        return 2 * a * b / (a + b + 1e-12)
    Dr = hmean(D[:, :-1], D[:, 1:])                 # between (j, j+1)
    Dd = hmean(D[:-1, :], D[1:, :])                 # between (i, i+1)

    # Explicit time step under the CFL stability limit, with a safety factor.
    dt = config.DIFFUSION_CFL * dx * dx / (4.0 * D.max())
    coef = dt / (dx * dx)

    P = np.zeros_like(cost_c)
    P[r0 // f, c0 // f] = 1.0

    horizons = sorted(hours)
    snaps = {}
    t = 0.0
    hi = 0
    max_t = horizons[-1]
    while t < max_t + dt and hi < len(horizons):
        # snapshot any horizons reached
        while hi < len(horizons) and t >= horizons[hi] - 1e-9:
            snaps[horizons[hi]] = P.copy()
            hi += 1
        if hi >= len(horizons):
            break
        # one explicit diffusion step (mass-conserving, no-flux boundaries)
        fx = coef * Dr * (P[:, 1:] - P[:, :-1])
        P[:, :-1] += fx
        P[:, 1:] -= fx
        fy = coef * Dd * (P[1:, :] - P[:-1, :])
        P[:-1, :] += fy
        P[1:, :] -= fy
        t += dt

    # upsample each snapshot back to the 1 m grid, normalize to max = 1
    out = {}
    zoom = (ctx["dem"].shape[0] / P.shape[0], ctx["dem"].shape[1] / P.shape[1])
    for h, Pc in snaps.items():
        big = ndimage.zoom(Pc, zoom, order=1)[:ctx["dem"].shape[0], :ctx["dem"].shape[1]]
        big = np.clip(big, 0, None)
        out[h] = big / (big.max() + 1e-12)
    return out, dx


def reach_stats(prob, ctx, frac=0.05):
    """Farthest distance (m) from last-seen where prob exceeds frac*max, plus how
    much of that reach is to the north — a quick 'how big is the zone' summary."""
    r0, c0 = ctx["center_rc"]
    rr, cc = np.nonzero(prob > frac)
    if rr.size == 0:
        return {"max_m": 0.0, "north_m": 0.0}
    d = np.hypot(rr - r0, cc - c0) * ctx["res"]
    north = (r0 - rr) * ctx["res"]                  # rows increase southward
    return {"max_m": float(d.max()), "north_m": float(north.max())}
