"""
jet_intel.dna — time-decayed, multi-facet customer preference model.

Pure stdlib. No dependencies. Fully backward compatible with the v9 profile
shape, so existing rows in the `dna` table keep working and are upgraded in
place on first write.

What v9 stored
--------------
    {'categories': {'tv': 3}, 'queries': 7, 'clicks': {...},
     'budget_sum': 12000, 'budget_n': 3}

and applied it as:

    boost = min(.12, .12 * cat_hits / total)
    boost += .05 if price <= mean_budget else -.04

Four limitations:

  1. NO DECAY.  A television bought two years ago counts exactly as much as a
     search from this morning. The profile describes who the customer used to
     be. For a product whose pitch is "לומד מה המשתמש אוהב", learning that
     never forgets is the wrong model — preferences move.

  2. NO NEGATIVE SIGNAL.  Only clicks are recorded. An option shown twenty
     times and never clicked is the strongest preference signal available, and
     v9 discarded it. The product literally promises to learn what the user
     "דוחה" (rejects); the v9 code had no field for it.

  3. MEAN BUDGET.  `budget_sum / budget_n` means one search for a 40,000
     kitchen renovation permanently distorts the profile of someone who
     otherwise shops at 500. A trimmed, decayed quantile is far more stable.

  4. CATEGORY ONLY.  Brand affinity, merchant affinity, price-band preference
     and attribute preference are all invisible, so personalization cannot
     express "this person buys quiet mid-range Bosch appliances".

Design
------
Every facet stores (weight, last_updated) and decays exponentially with a
configurable half-life. Positive and negative evidence are tracked separately
so that "shown often, never chosen" is representable. Nothing is stored that
identifies a person: the key is the anonymous session id the v9 schema
already used.
"""
from __future__ import annotations

import math
import os
import time

HALF_LIFE_DAYS = float(os.getenv("JET_DNA_HALF_LIFE_DAYS", "45"))
MAX_FACET_ENTRIES = 40
MAX_BOOST = 0.18

# How much each observed event moves a facet.
EVENT_WEIGHTS = {
    "search": 0.6,
    "view": 0.3,
    "impression": 0.05,
    "outbound_click": 1.0,
    "save": 1.4,
    "share": 1.2,
    "purchase": 3.0,
    "refund": -2.5,
    "dismiss": -1.0,
    "reject": -1.2,
}


def _decay_factor(age_seconds: float) -> float:
    if HALF_LIFE_DAYS <= 0:
        return 1.0
    half_life = HALF_LIFE_DAYS * 86400.0
    return 0.5 ** (max(0.0, age_seconds) / half_life)


def empty_profile() -> dict:
    """A fresh profile, carrying the v9 keys for compatibility."""
    return {
        "version": 2,
        "queries": 0,
        "categories": {},      # v9 key, kept as plain counts for compatibility
        "clicks": {},          # v9 key
        "budget_sum": 0.0,     # v9 key
        "budget_n": 0,         # v9 key
        "facets": {            # v2: decayed (weight, timestamp) pairs
            "category": {},
            "brand": {},
            "merchant": {},
            "price_band": {},
            "attribute": {},
            "use_case": {},
        },
        "negative": {"category": {}, "brand": {}, "merchant": {}},
        "budget_samples": [],  # [(value, ts)], trimmed
        "updated": 0.0,
    }


def upgrade(profile: dict | None) -> dict:
    """Bring a v9 profile up to v2 without losing anything it recorded."""
    base = empty_profile()
    if not profile:
        return base
    if profile.get("version") == 2:
        for k, v in base.items():
            profile.setdefault(k, v)
        for facet in base["facets"]:
            profile["facets"].setdefault(facet, {})
        return profile

    now = time.time()
    base["queries"] = int(profile.get("queries", 0) or 0)
    base["categories"] = dict(profile.get("categories", {}) or {})
    base["clicks"] = dict(profile.get("clicks", {}) or {})
    base["budget_sum"] = float(profile.get("budget_sum", 0) or 0)
    base["budget_n"] = int(profile.get("budget_n", 0) or 0)

    # Seed the decayed facets from the legacy counters, timestamped now.
    # Their influence then decays going forward like any other evidence.
    for cat, count in base["categories"].items():
        base["facets"]["category"][cat] = [float(count) * EVENT_WEIGHTS["search"], now]
    if base["budget_n"]:
        mean = base["budget_sum"] / base["budget_n"]
        base["budget_samples"] = [[mean, now]] * min(base["budget_n"], 5)
    base["updated"] = now
    return base


def _bump(store: dict, key: str, delta: float, now: float) -> None:
    if not key:
        return
    key = str(key)[:64]
    weight, ts = store.get(key, [0.0, now])
    weight = weight * _decay_factor(now - ts) + delta
    store[key] = [round(weight, 5), now]
    if len(store) > MAX_FACET_ENTRIES:
        # Drop the weakest decayed entries.
        ranked = sorted(store.items(), key=lambda kv: abs(kv[1][0] * _decay_factor(now - kv[1][1])))
        for k, _ in ranked[: len(store) - MAX_FACET_ENTRIES]:
            store.pop(k, None)


def current_weight(store: dict, key: str, now: float | None = None) -> float:
    now = now or time.time()
    entry = store.get(str(key))
    if not entry:
        return 0.0
    return entry[0] * _decay_factor(now - entry[1])


def price_band(price: float) -> str:
    """Log-spaced price bands, so preference generalizes across nearby prices."""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return "unknown"
    if price <= 0:
        return "unknown"
    return f"b{int(math.log10(price) * 3)}"


def observe_intent(profile: dict, intent: dict) -> dict:
    """Record a search."""
    profile = upgrade(profile)
    now = time.time()
    profile["queries"] = profile.get("queries", 0) + 1
    profile["updated"] = now

    cat = intent.get("category")
    if cat:
        profile["categories"][cat] = profile["categories"].get(cat, 0) + 1
        _bump(profile["facets"]["category"], cat, EVENT_WEIGHTS["search"], now)
    for brand in (intent.get("brands") or [])[:3]:
        _bump(profile["facets"]["brand"], brand, EVENT_WEIGHTS["search"], now)
    if intent.get("use_case"):
        _bump(profile["facets"]["use_case"], intent["use_case"], EVENT_WEIGHTS["search"], now)
    for pref in (intent.get("preferences") or [])[:4]:
        _bump(profile["facets"]["attribute"], pref, EVENT_WEIGHTS["search"], now)

    budget = intent.get("max_price")
    if budget:
        profile["budget_sum"] = profile.get("budget_sum", 0) + float(budget)
        profile["budget_n"] = profile.get("budget_n", 0) + 1
        samples = profile.setdefault("budget_samples", [])
        samples.append([float(budget), now])
        if len(samples) > 50:
            del samples[: len(samples) - 50]
        _bump(profile["facets"]["price_band"], price_band(budget), EVENT_WEIGHTS["search"], now)
    return profile


def observe_event(profile: dict, event: str, product: dict | None = None) -> dict:
    """
    Record an outcome event against the facets of the product involved.

    `product` is optional; when the caller only knows a product id the legacy
    click counter is still updated, matching v9 behaviour.
    """
    profile = upgrade(profile)
    now = time.time()
    delta = EVENT_WEIGHTS.get(event, 0.0)
    profile["updated"] = now

    if event == "outbound_click" and product and product.get("product_id"):
        pid = str(product["product_id"])[:128]
        profile["clicks"][pid] = profile["clicks"].get(pid, 0) + 1
        if len(profile["clicks"]) > 200:
            for k in list(profile["clicks"])[:50]:
                profile["clicks"].pop(k, None)

    if not product or delta == 0.0:
        return profile

    facets = profile["facets"]
    _bump(facets["category"], product.get("category", ""), delta, now)
    _bump(facets["brand"], product.get("brand", ""), delta, now)
    _bump(facets["merchant"], product.get("merchant", ""), delta, now)
    if product.get("price"):
        _bump(facets["price_band"], price_band(product["price"]), delta, now)

    if delta < 0:
        neg = profile["negative"]
        _bump(neg["category"], product.get("category", ""), -delta, now)
        _bump(neg["brand"], product.get("brand", ""), -delta, now)
        _bump(neg["merchant"], product.get("merchant", ""), -delta, now)
    return profile


def typical_budget(profile: dict) -> float | None:
    """
    Decay-weighted trimmed median of declared budgets.

    The trim is what stops one outlier search from redefining the customer.
    """
    profile = upgrade(profile)
    samples = profile.get("budget_samples") or []
    now = time.time()
    weighted = [(v, _decay_factor(now - ts)) for v, ts in samples if v and v > 0]
    weighted = [(v, w) for v, w in weighted if w > 0.05]
    if not weighted:
        if profile.get("budget_n"):
            return profile["budget_sum"] / profile["budget_n"]
        return None
    weighted.sort(key=lambda x: x[0])
    if len(weighted) >= 5:
        cut = max(1, len(weighted) // 10)
        weighted = weighted[cut:-cut] or weighted
    total_w = sum(w for _, w in weighted)
    if total_w <= 0:
        return None
    acc = 0.0
    for value, w in weighted:
        acc += w
        if acc >= total_w / 2:
            return round(value, 2)
    return round(weighted[-1][0], 2)


def personalization_boost(profile: dict, item) -> tuple[float, list[str]]:
    """
    Signed adjustment in roughly [-MAX_BOOST, +MAX_BOOST], plus the reasons.

    Returning the reasons alongside the number is deliberate: personalization
    that cannot explain itself is indistinguishable from manipulation, and the
    reasons feed the customer-facing explanation.
    """
    profile = upgrade(profile)
    now = time.time()
    facets = profile["facets"]
    negative = profile.get("negative", {})
    reasons: list[str] = []
    score = 0.0

    def norm(store: dict, key: str) -> float:
        total = sum(abs(w * _decay_factor(now - ts)) for w, ts in store.values()) or 1.0
        return current_weight(store, key, now) / total

    cat = getattr(item, "category", "") or ""
    brand = getattr(item, "brand", "") or ""
    merchant = getattr(item, "merchant", "") or ""
    price = getattr(item, "price", 0) or 0

    if cat:
        v = norm(facets["category"], cat)
        if v > 0.12:
            score += min(0.07, 0.07 * v * 2)
            reasons.append("category_affinity")
        neg = current_weight(negative.get("category", {}), cat, now)
        if neg > 1.5:
            score -= 0.05
            reasons.append("category_previously_rejected")

    if brand:
        v = norm(facets["brand"], brand)
        if v > 0.10:
            score += min(0.05, 0.05 * v * 2)
            reasons.append("brand_affinity")
        if current_weight(negative.get("brand", {}), brand, now) > 1.5:
            score -= 0.04
            reasons.append("brand_previously_rejected")

    if merchant:
        if current_weight(negative.get("merchant", {}), merchant, now) > 2.0:
            score -= 0.05
            reasons.append("merchant_previously_rejected")
        elif norm(facets["merchant"], merchant) > 0.20:
            score += 0.03
            reasons.append("merchant_affinity")

    budget = typical_budget(profile)
    if budget and price:
        if price <= budget:
            score += 0.04
            reasons.append("within_typical_budget")
        elif price > budget * 1.5:
            score -= 0.05
            reasons.append("well_above_typical_budget")

    band_v = norm(facets["price_band"], price_band(price)) if price else 0.0
    if band_v > 0.25:
        score += 0.03
        reasons.append("familiar_price_range")

    return round(max(-MAX_BOOST, min(MAX_BOOST, score)), 4), reasons


def summary(profile: dict) -> dict:
    """Non-identifying snapshot for the control centre."""
    profile = upgrade(profile)
    now = time.time()

    def top(store: dict, n: int = 5) -> dict:
        live = {k: round(w * _decay_factor(now - ts), 3) for k, (w, ts) in store.items()}
        live = {k: v for k, v in live.items() if abs(v) > 0.01}
        return dict(sorted(live.items(), key=lambda kv: kv[1], reverse=True)[:n])

    facets = profile["facets"]
    return {
        "queries": profile.get("queries", 0),
        "known_categories": profile.get("categories", {}),
        "category_affinity": top(facets["category"]),
        "brand_affinity": top(facets["brand"]),
        "attribute_affinity": top(facets["attribute"]),
        "use_case_affinity": top(facets["use_case"]),
        "rejected": {k: top(v, 3) for k, v in profile.get("negative", {}).items()},
        "typical_budget": typical_budget(profile),
        "half_life_days": HALF_LIFE_DAYS,
    }
