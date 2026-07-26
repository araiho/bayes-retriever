"""Central config for the lost-dog search prioritization pipeline.

Tune weights here; add new signals in layers.py + score.SIGNALS.
"""

# --- Area of interest ---
# Half-width of the square analysis window, meters (1000 -> 2km x 2km).
HALF_WIDTH_M = 1000
RESOLUTION_M = 1.0          # DEM grid resolution (3DEP 1m LiDAR in Philly)
UTM_CRS = "EPSG:26918"      # UTM 18N (meters) — Philadelphia

# --- Data endpoints (all free, no auth) ---
DEM_URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
           "3DEPElevation/ImageServer/exportImage")
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# --- Signal weights (relative; normalized at combine time) ---
# Base weights; the temperament preset (below) scales them per dog personality.
BASE_WEIGHTS = {
    "concavity":  3.0,   # TPI local lows / gullies — core terrain signal
    "depression": 2.0,   # closed depressions / culvert-like pockets
    "flat_bench": 1.0,   # low-slope spots (dogs bed down on benches, not cliffs)
    "vegetation": 3.0,   # dense cover — Wissahickon is high-density veg
    "water":      2.5,   # proximity to streams/ponds
    "road_quiet": 1.0,   # preference away from roads/people
    "downhill":   1.0,   # scared dogs take the path of least resistance
}

# --- Temperament (Missing Animal Response behavioral categories) ---
# Drives (a) dispersion tightness via the 50%-mass ring radius calibrated to
# lost-dog find-distance statistics, (b) how much the dog moves (diffusion), and
# (c) signal weight emphasis. Set TEMPERAMENT or pass it as run.py's 3rd arg.
TEMPERAMENTS = {
    # extremely shy/scared: dens down CLOSE in the heaviest cover, flees all
    # people — tightest rings, least travel, cover & people-avoidance boosted.
    "xenophobic": {
        "ring50_m": 250, "mobility_mult": 0.45,
        "weight_mult": {"vegetation": 1.7, "concavity": 1.4, "depression": 1.3,
                        "road_quiet": 2.0, "downhill": 1.2},
    },
    # wary but will travel; intermediate dispersion.
    "aloof": {
        "ring50_m": 550, "mobility_mult": 1.0,
        "weight_mult": {"vegetation": 1.2, "road_quiet": 1.2},
    },
    # friendly: approaches people, found near activity/roads, ranges wider.
    "gregarious": {
        "ring50_m": 900, "mobility_mult": 1.4,
        "weight_mult": {"road_quiet": 0.3, "vegetation": 0.8},
    },
}
TEMPERAMENT = "xenophobic"   # this dog is really scared and shy

# Buildings are DE-prioritized, not removed (dogs hide under decks / in yards).
BUILDING_MULTIPLIER = 0.2
# Dog isn't IN the creek: water surface (+ small buffer) is knocked down hard;
# banks stay hot via the water-proximity signal.
WATER_MULTIPLIER = 0.08
WATER_EXCLUDE_BUFFER_M = 4
# Trail beds are travel corridors, not hiding spots (and their graded cuts fake
# concavity/flatness in the LiDAR) — suppress a narrow ribbon along them.
TRAIL_MULTIPLIER = 0.3
TRAIL_BUFFER_M = 5
# Road surfaces themselves aren't hiding spots either (embankment cuts fake
# concavity); verges beyond the buffer stay searchable.
ROAD_MULTIPLIER = 0.15
ROAD_BUFFER_M = 3
# Rail cuts read as perfect gullies in LiDAR but are travel corridors, not
# hiding spots (and active track is unsafe to search) — suppress the bed,
# keep the brushy cut banks.
RAIL_MULTIPLIER = 0.25
RAIL_BUFFER_M = 6

# --- Signal parameters (meters unless noted) ---
TPI_SCALES = [15, 50]        # neighborhood sizes for multi-scale TPI
DEPRESSION_SIZE = 25         # structuring element for depression fill
SLOPE_EFOLD_DEG = 12.0       # slope score = exp(-slope/this)
VEG_BLUR_M = 10              # gaussian blur on vegetation mask
WATER_EFOLD_M = 100.0        # water score = exp(-dist/this)
ROAD_SATURATE_M = 200.0      # road score = clip(dist/this, 0, 1)
DOWNHILL_EFOLD_M = 15.0      # elevation scale for the downhill-preference sigmoid
# PRIOR_DECAY_M (e-fold of travel effort) is set from temperament ring50 below.

# --- Animal-movement resistance surface (layers.movement_cost) ---
# Per-meter travel effort. Base ground = 1.0.
SLOPE_COST_PER_DEG = 0.08    # each degree of slope adds this much effort
RAIL_TRAVEL_COST = 0.30      # rail corridors: documented dog travel routes (cheap)
TRAIL_TRAVEL_COST = 0.55     # trails/footpaths: easy going, dogs follow them
# Busy road: crossing the ~8m barrier ribbon adds BARRIER_COST*8 effort, which
# under exp(-cum/600) dims reach by exp(-8*120/600) ≈ 0.20 per road crossed —
# strongly deprioritized but a smooth step, not a hard black cliff.
BARRIER_COST = 120.0
BARRIER_BUFFER_M = 4         # half-width of the barrier ribbon around road centerline

# --- Time-diffusion model (diffusion.py) ---
# Snapshots of the probable zone, hours since the dog went missing.
DIFFUSION_HOURS = [6, 24, 72]
DIFFUSION_COARSEN_M = 15     # grid size for the diffusion solve (envelope-scale)
DIFFUSION_CFL = 0.2          # explicit-scheme safety factor (<0.25 for stability)
# Diffusivity of open ground (m^2/h). Calibrated so open-ground spread std is
# ~175 m @6h, ~350 m @24h, ~600 m @72h (sqrt-time growth) for a typical dog;
# scaled by temperament mobility_mult below. Corridors reach 1/sqrt(cost)
# further; barriers hold mass back.
DIFFUSION_MOBILITY_BASE = 1250.0

# --- Direction-of-travel prior (flight bearing from a sighting) ---
# Multiplicative cone toward the observed bearing:
#   w = FLOOR + (1-FLOOR) * exp(kappa * (cos(theta - bearing) - 1))
# FLOOR de-prioritizes the opposite direction WITHOUT masking it (dogs loop
# back); kappa sets cone tightness. A chased/panicked bolt (case "chased") is a
# more reliable direction signal, so kappa tightens and the dispersion ring
# stretches ALONG the bearing (adrenalized dogs overshoot the median).
FLIGHT_KAPPA = 1.5
FLIGHT_KAPPA_CHASED = 2.5
FLIGHT_FLOOR = 0.35
FLIGHT_NEAR_FADE_M = 75      # bearing says nothing about the immediate vicinity
CHASED_RING_STRETCH = 1.35   # along-bearing dispersion stretch for a bolt

# --- Home anchor (case "home": {lat, lon}) ---
# Dogs lost away from home sometimes work back along familiar ground (homing
# is documented within a few miles), so cells nearer home get a mild boost:
#   w = FLOOR + (1-FLOOR) * exp(-(d_home - d_home_min) / DECAY)
# Deliberately WEAK — a settled shy dog anchors on food/water/cover first —
# and floored so the far side stays searchable (de-prioritize, never mask).
HOME_PULL_FLOOR = 0.6
HOME_PULL_DECAY_M = 1500

# --- Search-effort discounting (Bayesian update for effort already spent) ---
# Koopman search theory: after a search with detection probability POD over an
# area, the posterior there is prior * (1 - POD). Each logged visit removes a
# Gaussian patch of probability; the NEXT-search ranking runs on what remains.
SEARCH_POD_DEFAULT = 0.40    # one walk-through pass, scared hiding dog (low!)
SEARCH_RADIUS_M_DEFAULT = 60.0   # effective sweep radius of one logged point
SEARCH_DECAY_H = 96.0        # old effort counts less (the dog moves back in):
                             # effective POD = pod * exp(-age_h / this)

# --- Busy trails (people pressure; case lists them by OSM name) ---
# A heavily-used path (walkers/dogs/bikes) is the opposite of a quiet trail for
# a scared dog: NOT a cheap travel corridor, and nearby cover is de-prioritized
# (never masked — she can still cross it, or use it in the dead of night).
BUSY_TRAIL_TRAVEL_COST = 2.0     # mild aversion vs 0.55 for quiet trails
PEOPLE_PRESSURE_M = 80           # habitat knock-down ramps back to 1 by here
PEOPLE_PRESSURE_FLOOR = 0.35     # de-prioritize-not-mask floor at the trail

# --- Settled-dog phase (case gives lost_since -> hours missing) ---
# After the early wide-ranging days a shy lost dog settles into a small circuit
# anchored on a reliable water/food source (drinking-site fidelity; Lost Dogs
# of America / MAR settling behavior). Past this threshold the water-proximity
# weight is boosted, and hours-missing is appended to DIFFUSION_HOURS so the
# map shows the zone at the ACTUAL time out.
SETTLED_AFTER_H = 72
SETTLED_WATER_MULT = 1.6

# --- Search-segment ranking (SAR: rank by Probability of Area mass) ---
# Candidate spots are ranked by the probability MASS integrated over a
# searchable segment of this radius, not by the single peak cell — so isolated
# far peaks with little surrounding probability drop out.
SEGMENT_RADIUS_M = 90
N_HOTSPOTS = 10
HOTSPOT_MIN_SEP_M = 160      # minimum separation between reported segments


# --- Temperament application (sets PRIOR_DECAY_M, DIFFUSION_MOBILITY, WEIGHTS) ---
# For a 2-D exponential dispersal exp(-r/L), 50% of the probability mass lies
# within r = 1.6783*L, so L = ring50 / 1.6783 reproduces the target 50% ring.
_RING50_TO_EFOLD = 1.6783


def apply_temperament(name=None, ring50_m=None):
    """Resolve the temperament preset into effective globals used across the
    pipeline. Call with a name to override TEMPERAMENT (e.g. from the CLI).
    ring50_m overrides the preset's guess with case EVIDENCE (e.g. the measured
    median find-distance for the region/breed)."""
    global TEMPERAMENT, PRIOR_DECAY_M, DIFFUSION_MOBILITY, WEIGHTS
    if name:
        TEMPERAMENT = name
    t = TEMPERAMENTS[TEMPERAMENT]
    PRIOR_DECAY_M = (ring50_m or t["ring50_m"]) / _RING50_TO_EFOLD
    DIFFUSION_MOBILITY = DIFFUSION_MOBILITY_BASE * t.get("mobility_mult", 1.0)
    WEIGHTS = dict(BASE_WEIGHTS)
    for k, m in t.get("weight_mult", {}).items():
        if k in WEIGHTS:
            WEIGHTS[k] *= m
    return TEMPERAMENT


apply_temperament()   # initialize WEIGHTS / PRIOR_DECAY_M / DIFFUSION_MOBILITY
