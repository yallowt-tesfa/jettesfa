"""
jet_intel.bandit — Thompson sampling model arena.

Pure stdlib (`random.betavariate`). No dependencies.

What v9 did
-----------
    if min(impressions_a, impressions_b) >= 100 and utility(b) > utility(a) * 1.03:
        return challenger
    return champion

Three problems with that rule:

  1. IT NEVER EXPLORES.  The challenger only accumulates impressions if it is
     already being served, and it is only served after it has proved itself.
     `arena_score` calls `arena_model()` which returns the champion, so the
     challenger's impression count stays at zero and the promotion condition
     is unreachable. The arena could not actually learn.

  2. NO UNCERTAINTY.  A model with 100 impressions and 4 purchases is treated
     as exactly as trustworthy as one with 10,000 impressions and 400. The
     3% margin is an arbitrary stand-in for a confidence interval.

  3. ALL-OR-NOTHING SWITCHING.  Promotion flips 100% of traffic at once. A
     challenger that looked good on a noisy sample takes the whole product
     down with it, and there is no gradual rollback.

Thompson sampling fixes all three with one mechanism. Each model keeps a
Beta posterior over its success rate. To pick a model, draw one sample from
each posterior and serve the argmax. Models that are genuinely better get
served more as evidence accumulates; models that are merely untested get
explored because their posteriors are wide. Traffic share moves continuously,
and reverts on its own if performance degrades — no promotion event, no
rollback procedure.

A minimum-observation floor is retained from v9: below it, the champion is
served with only a small exploration probability, so the arena cannot swing
the whole product on ten data points.
"""
from __future__ import annotations

import math
import os
import random
import threading

# Reward weights. A purchase is the outcome that matters; a refund is a strong
# negative because it means the recommendation actively wasted the customer's
# time and money.
W_CLICK = 0.20
W_PURCHASE = 1.00
W_REFUND = -1.20
W_SATISFACTION = 0.40

MIN_OBS = int(os.getenv("JET_ARENA_MIN_OBS", "100"))
EXPLORE_FLOOR = float(os.getenv("JET_ARENA_EXPLORE", "0.10"))

_LOCK = threading.Lock()


def reward_stats(stat: dict) -> tuple[float, float]:
    """
    Convert raw counters into Beta(alpha, beta) pseudo-counts.

    `successes` is the reward-weighted positive outcome mass and `failures` is
    the remaining impression mass. Both are floored at a weak uniform prior
    Beta(1,1), which is what makes an untested model explore rather than be
    assumed dead.
    """
    impressions = max(0, int(stat.get("impressions", 0) or 0))
    clicks = max(0, int(stat.get("clicks", 0) or 0))
    purchases = max(0, int(stat.get("purchases", 0) or 0))
    refunds = max(0, int(stat.get("refunds", 0) or 0))
    sat_sum = float(stat.get("satisfaction_sum", 0) or 0)
    sat_n = max(0, int(stat.get("satisfaction_n", 0) or 0))

    reward = W_CLICK * clicks + W_PURCHASE * purchases + W_REFUND * refunds
    if sat_n:
        # Satisfaction is 0..5; centre it so 2.5 is neutral.
        reward += W_SATISFACTION * sat_n * ((sat_sum / sat_n) - 2.5) / 2.5

    denom = max(impressions, clicks, 1)
    successes = max(0.0, reward)
    failures = max(0.0, denom - successes)

    return 1.0 + successes, 1.0 + failures


def posterior_mean(stat: dict) -> float:
    a, b = reward_stats(stat)
    return a / (a + b)


def posterior_interval(stat: dict, z: float = 1.96) -> tuple[float, float]:
    """Normal approximation to the Beta credible interval, for reporting."""
    a, b = reward_stats(stat)
    n = a + b
    mean = a / n
    var = (a * b) / (n * n * (n + 1))
    half = z * math.sqrt(max(var, 0.0))
    return round(max(0.0, mean - half), 4), round(min(1.0, mean + half), 4)


def select(stats: dict, models: list[str], champion: str, rng: random.Random | None = None) -> str:
    """
    Choose which model serves this request.

    Below the observation floor the champion serves, except for an
    EXPLORE_FLOOR share of traffic used to gather the evidence the floor
    requires — this is the piece v9 was missing, and without it the arena can
    never accumulate the data its own promotion rule demands.
    """
    rng = rng or random
    models = [m for m in models if m] or [champion]
    if champion not in models:
        models = [champion] + models

    observed = [int((stats.get(m) or {}).get("impressions", 0) or 0) for m in models]
    if min(observed) < MIN_OBS:
        if rng.random() < EXPLORE_FLOOR:
            underexplored = [m for m, n in zip(models, observed) if n < MIN_OBS]
            return rng.choice(underexplored or models)
        return champion

    with _LOCK:
        draws = {m: rng.betavariate(*reward_stats(stats.get(m) or {})) for m in models}
    return max(draws, key=draws.get)


def traffic_share(stats: dict, models: list[str], champion: str, samples: int = 2000) -> dict:
    """
    Estimated share of traffic each model would receive right now.

    Reported in the admin console so an operator can see the arena shifting
    gradually instead of only seeing a promotion event after the fact.
    """
    rng = random.Random(12345)
    counts = {m: 0 for m in models}
    for _ in range(samples):
        chosen = select(stats, models, champion, rng)
        counts[chosen] = counts.get(chosen, 0) + 1
    total = sum(counts.values()) or 1
    return {m: round(c / total, 4) for m, c in counts.items()}


def report(stats: dict, models: list[str], champion: str) -> dict:
    """Full arena state for the control centre."""
    rows = {}
    for m in models:
        st = stats.get(m) or {}
        lo, hi = posterior_interval(st)
        imp = int(st.get("impressions", 0) or 0)
        rows[m] = {
            "impressions": imp,
            "clicks": int(st.get("clicks", 0) or 0),
            "purchases": int(st.get("purchases", 0) or 0),
            "refunds": int(st.get("refunds", 0) or 0),
            "posterior_mean": round(posterior_mean(st), 4),
            "credible_interval": [lo, hi],
            "observations_to_floor": max(0, MIN_OBS - imp),
            "status": "exploring" if imp < MIN_OBS else "evaluated",
        }
    return {
        "policy": "thompson_sampling",
        "champion": champion,
        "min_observations": MIN_OBS,
        "explore_floor": EXPLORE_FLOOR,
        "models": rows,
        "estimated_traffic_share": traffic_share(stats, models, champion),
    }
