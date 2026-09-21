"""
jet_intel.ranking — currency-correct scoring, decisions and honest explanations.

Pure stdlib. No dependencies.

Three upgrades over the v9 scoring path.

1. CURRENCY-CORRECT LANDED COST
   `jet_app.total_cost` added price + shipping + tax as bare floats and
   compared the result to a budget in a different currency. Here everything is
   converted to a single base before any comparison, and the conversion's
   confidence is carried into the confidence component so a fallback-rate
   figure is never presented as a precise number.

2. FAKE-DISCOUNT RESISTANCE
   v9's `price_truth` trusted `old_price` from the feed. Feeds are supplied by
   merchants, and an inflated "was" price is the oldest trick in retail. Here
   an advertised saving is only credited when the product's own observed price
   history (robust median, outliers removed) corroborates it, and an
   uncorroborated claim is actively penalized rather than merely ignored.

3. EXPLANATIONS FROM ACTUAL CONTRIBUTIONS
   v9's `why()` was five independent threshold tests against fixed cut-offs,
   producing text with no causal link to the ranking. If quality was 0.87 and
   the cut-off was 0.88, the customer was told nothing about quality even when
   quality was what put the item first. Here the explanation is derived from
   each component's actual weighted contribution to this item's score relative
   to the other candidates, so the stated reason is the real reason.

   The same machinery produces the honest negative: what is weak about an
   option, which is what makes WAIT and AVOID credible rather than decorative.
"""
from __future__ import annotations

from . import fx, store as pstore, dna as dna_mod

# Customer value dominates; affiliate revenue stays capped at 1%, matching the
# v9 guarantee that the regression suite and the 20k simulation enforce.
WEIGHTS = {
    "need_fit": 0.27,
    "deal_truth": 0.17,
    "seller_trust": 0.18,
    "quality": 0.14,
    "timing": 0.08,
    "confidence": 0.10,
    "satisfaction": 0.05,
    "revenue": 0.01,
}

COMPONENT_LABELS_HE = {
    "need_fit": "התאמה לצורך שתיארת",
    "deal_truth": "מחיר אמיתי מול ההיסטוריה",
    "seller_trust": "אמינות המוכר",
    "quality": "איכות המוצר",
    "timing": "עיתוי וזמינות",
    "confidence": "שלמות וודאות הנתונים",
    "satisfaction": "שביעות רצון קודמת",
    "revenue": "שיקול מסחרי",
}


def clamp(v: float) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def safe_float(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", "").replace("₪", "").strip())
    except (TypeError, ValueError, AttributeError):
        return default


# ---------------------------------------------------------------------------
# Landed cost
# ---------------------------------------------------------------------------

def landed_cost(item, base: str = fx.BASE) -> dict:
    """
    Full comparable cost in one currency, with its uncertainty.

    Shipping, tax, customs and any connector-supplied fee are included. Every
    component is converted from the item's own currency.
    """
    meta = getattr(item, "metadata", None) or {}
    currency = (getattr(item, "currency", "") or fx.BASE).upper()

    price = max(0.0, safe_float(getattr(item, "price", 0)))
    shipping = max(0.0, safe_float(getattr(item, "shipping", 0) or 0))
    tax = max(0.0, safe_float(meta.get("tax", 0)))
    customs = max(0.0, safe_float(meta.get("customs", meta.get("import_fee", 0))))
    fees = max(0.0, safe_float(meta.get("fees", 0)))

    native_total = price + shipping + tax + customs + fees
    return {
        "native_total": round(native_total, 2),
        "native_currency": currency,
        "total": fx.convert(native_total, currency, base),
        "currency": base,
        "price": fx.convert(price, currency, base),
        "shipping": fx.convert(shipping, currency, base),
        "tax": fx.convert(tax + customs + fees, currency, base),
        "fx_confidence": fx.conversion_confidence(currency),
        "converted": currency != base,
    }


def total_cost(item) -> float:
    """Base-currency landed cost. Signature-compatible with jet_app.total_cost."""
    return landed_cost(item)["total"]


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def seller_trust(item) -> float:
    meta = getattr(item, "metadata", None) or {}
    t = clamp(getattr(item, "trust", 0.6))

    rating = safe_float(meta.get("seller_rating", 0))
    if rating:
        # Accept either a 0-5 or a 0-100 scale.
        normalized = rating / 5.0 if rating <= 5 else rating / 100.0
        count = safe_float(meta.get("seller_rating_count", meta.get("reviews", 0)))
        # A 5-star rating from three reviews is not evidence; shrink toward the
        # prior until enough reviews accumulate.
        shrink = min(1.0, count / 50.0) if count else 0.35
        t = (1 - 0.35 * shrink) * t + (0.35 * shrink) * clamp(normalized)

    if meta.get("returns") is False:
        t *= 0.90
    if str(meta.get("condition", "")).lower() in {"used", "for parts", "refurbished"}:
        t *= 0.94
    if not getattr(item, "available", True):
        t *= 0.35
    return clamp(t)


def deal_truth(item, snapshot: pstore.PriceSnapshot, key: str) -> tuple[float, dict]:
    """
    How good this price really is, and the evidence behind that judgement.

    An advertised discount is only credited when the observed history agrees.
    A claimed saving the history contradicts is penalized.
    """
    price = safe_float(getattr(item, "price", 0))
    fallback = clamp(getattr(item, "deal", 0.5))
    hist = snapshot.deal_score(key, price, fallback)
    stats = snapshot.stats_for(key)

    old = getattr(item, "old_price", None)
    advertised = 0.0
    if old and safe_float(old) > price > 0:
        advertised = clamp((safe_float(old) - price) / safe_float(old))

    corroborated = None
    penalty = 0.0
    if advertised > 0.02 and stats["reference"] and stats["n"] >= 3:
        ref = stats["reference"]
        implied_ref = safe_float(old)
        # If the claimed "was" price is far above everything ever observed,
        # the discount is manufactured.
        if implied_ref > ref * 1.35:
            corroborated = False
            penalty = min(0.20, 0.20 * (implied_ref / max(ref, 1) - 1))
        else:
            corroborated = True

    if corroborated is True:
        score = clamp(0.62 * hist + 0.38 * (0.5 + advertised))
    elif corroborated is False:
        score = clamp(hist - penalty)
    else:
        # No history to check against: credit the claim only weakly.
        score = clamp(0.80 * hist + 0.20 * (0.5 + advertised * 0.5))

    evidence = {
        "observations": stats["n"],
        "reference_price": stats["reference"],
        "history_confidence": stats["confidence"],
        "volatility": stats["volatility"],
        "trend": stats["trend"],
        "advertised_discount": round(advertised, 4),
        "discount_corroborated": corroborated,
    }
    return round(score, 4), evidence


def timing(item, evidence: dict) -> float:
    meta = getattr(item, "metadata", None) or {}
    stock = str(meta.get("stock", "")).lower()
    t = clamp(getattr(item, "freshness", 0.6))
    if stock in {"out of stock", "false", "0", "unavailable", "no"} or not getattr(item, "available", True):
        return 0.05
    # A price trending down means waiting is rational; trending up means now.
    trend = evidence.get("trend", 0.0) or 0.0
    if trend < -0.05:
        t = max(0.0, t - min(0.15, abs(trend)))
    elif trend > 0.05:
        t = min(1.0, t + min(0.10, trend))
    old = getattr(item, "old_price", None)
    if old and safe_float(old) > safe_float(getattr(item, "price", 0)):
        t = min(1.0, t + 0.06)
    return clamp(t)


def confidence(item, cost: dict, evidence: dict) -> float:
    fields = [
        bool(getattr(item, "title", "")),
        safe_float(getattr(item, "price", 0)) > 0,
        bool(getattr(item, "url", "")),
        bool(getattr(item, "merchant", "")),
        bool(getattr(item, "currency", "")),
        bool(getattr(item, "image", "")),
        bool(getattr(item, "gtin", "") or getattr(item, "brand", "")),
    ]
    completeness = sum(fields) / len(fields)
    history = min(1.0, evidence.get("observations", 0) / 10.0)
    return clamp(
        0.34 * clamp(getattr(item, "price_confidence", 0.6))
        + 0.20 * seller_trust(item)
        + 0.24 * completeness
        + 0.10 * history
        + 0.12 * cost["fx_confidence"]
    )


def need_fit(item, intent: dict, profile: dict, relevance: float, cost: dict) -> tuple[float, list[str]]:
    """
    Fit combines semantic relevance, personalization and budget reality.

    The budget test uses the converted landed cost, which is the whole point
    of the fx layer: a USD item is now measured against an ILS budget correctly.
    """
    boost, reasons = dna_mod.personalization_boost(profile, item)
    f = clamp(relevance + boost)

    budget = intent.get("max_price")
    if budget:
        total = cost["total"]
        # The budget is in the currency the customer named, or base by default.
        budget_base = fx.convert(budget, intent.get("currency") or fx.BASE, fx.BASE)
        if total > budget_base:
            over = (total - budget_base) / max(budget_base, 1.0)
            # The floor depends on whether the item is even the right kind of
            # thing. An over-budget laptop is still the most useful answer to
            # "a laptop under 3,000" — the customer needs to know their budget
            # does not reach, which the ALTERNATIVE decision and the
            # "over_budget" note say explicitly. Burying it beneath an
            # in-budget phone answers a question nobody asked. A wrong-category
            # item that is also over budget keeps the harsh floor.
            same_category = bool(intent.get("category")) and \
                getattr(item, "category", "") == intent.get("category")
            floor = 0.45 if same_category else 0.08
            f *= max(floor, 1.0 - over)
            reasons.append("over_budget")
        elif total <= budget_base * 0.75:
            f = clamp(f + 0.04)
            reasons.append("comfortably_under_budget")

    floor = intent.get("min_price")
    if floor and cost["total"] < fx.convert(floor, intent.get("currency") or fx.BASE, fx.BASE) * 0.6:
        f *= 0.75
        reasons.append("below_expected_range")

    return clamp(f), reasons


def score_components(item, intent: dict, profile: dict, relevance: float,
                     snapshot: pstore.PriceSnapshot, key: str) -> dict:
    cost = landed_cost(item)
    deal, evidence = deal_truth(item, snapshot, key)
    fit, fit_reasons = need_fit(item, intent, profile, relevance, cost)
    return {
        "components": {
            "need_fit": fit,
            "deal_truth": deal,
            "seller_trust": seller_trust(item),
            "quality": clamp(getattr(item, "quality", 0.6)),
            "timing": timing(item, evidence),
            "confidence": confidence(item, cost, evidence),
            "satisfaction": clamp(getattr(item, "satisfaction", 0.6)),
            "revenue": clamp(getattr(item, "commission", 0)),
        },
        "cost": cost,
        "evidence": evidence,
        "fit_reasons": fit_reasons,
    }


def score(components: dict, weights: dict | None = None) -> float:
    w = weights or WEIGHTS
    return round(100 * clamp(sum(w[k] * components[k] for k in w)), 2)


def score_conservative(components: dict) -> float:
    """
    Challenger policy: heavier downside control.

    Kept as a genuine alternative for the bandit to explore rather than a
    decorative second entry.
    """
    c = components
    base = (0.29 * c["need_fit"] + 0.18 * c["deal_truth"] + 0.21 * c["seller_trust"]
            + 0.10 * c["quality"] + 0.07 * c["timing"] + 0.11 * c["confidence"]
            + 0.04 * c["satisfaction"])
    risk = (1 - c["seller_trust"]) * 0.08 + (1 - c["confidence"]) * 0.06
    return round(100 * clamp(base - risk), 2)


POLICIES = {
    "truth_v6": score,
    "conservative_v6": score_conservative,
    "truth_v10": score,
    "conservative_v10": score_conservative,
}


def apply_policy(components: dict, policy: str) -> float:
    fn = POLICIES.get(policy, score)
    return fn(components)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def decide(item, intent: dict, components: dict, cost: dict, evidence: dict) -> tuple[str, str]:
    """Return (decision, Hebrew rationale)."""
    c = components
    budget = intent.get("max_price")
    budget_base = fx.convert(budget, intent.get("currency") or fx.BASE, fx.BASE) if budget else None
    sc = score(c)

    if not getattr(item, "available", True):
        return "AVOID", "הפריט אינו זמין כרגע אצל המוכר."
    if c["seller_trust"] < 0.42:
        return "AVOID", "רמת האמינות של המוכר נמוכה מדי ביחס לשאר האפשרויות."
    if c["confidence"] < 0.40:
        return "AVOID", "חסרים נתונים מהותיים על ההצעה, ולכן אי אפשר להשוות אותה בהוגנות."
    if evidence.get("discount_corroborated") is False:
        return "TRACK", "ההנחה המוצגת אינה מתיישבת עם היסטוריית המחירים שנצפתה. שווה לעקוב."
    if budget_base and cost["total"] > budget_base * 1.08:
        return "ALTERNATIVE", "העלות הכוללת חורגת מהתקציב שציינת. הצעתי כיוון חלופי."
    if evidence.get("trend", 0) < -0.08 and evidence.get("history_confidence", 0) > 0.3:
        return "WAIT", "המחיר במגמת ירידה עקבית. סביר שתשיג תנאים טובים יותר בהמתנה קצרה."
    if c["deal_truth"] >= 0.72 and sc >= 72:
        return "BUY", "המחיר טוב ביחס להיסטוריה, והמוכר והנתונים אמינים."
    if c["deal_truth"] < 0.48 and c["need_fit"] >= 0.70:
        return "WAIT", "ההתאמה טובה אבל המחיר אינו אטרקטיבי כרגע ביחס להיסטוריה."
    return "TRACK", "אפשרות סבירה. שווה מעקב עד שיופיעו תנאים טובים יותר."


# ---------------------------------------------------------------------------
# Explanations grounded in actual contributions
# ---------------------------------------------------------------------------

def explain(components: dict, cost: dict, evidence: dict, fit_reasons: list[str],
            baseline: dict | None = None) -> dict:
    """
    Explain the score from the components that actually moved it.

    `baseline` is the mean component vector across the candidate set. Measuring
    each component against it answers the question the customer is really
    asking — "why this one rather than the others" — instead of "does this item
    clear an arbitrary fixed threshold".
    """
    baseline = baseline or {k: 0.5 for k in WEIGHTS}
    contributions = []
    for k, w in WEIGHTS.items():
        if k == "revenue":
            continue
        delta = (components[k] - baseline.get(k, 0.5)) * w
        contributions.append((k, delta, components[k]))
    contributions.sort(key=lambda x: x[1], reverse=True)

    strengths = [(k, v) for k, d, v in contributions if d > 0.012][:3]
    weaknesses = [(k, v) for k, d, v in reversed(contributions) if d < -0.012][:2]

    positive = [COMPONENT_LABELS_HE[k] for k, _ in strengths]
    negative = [COMPONENT_LABELS_HE[k] for k, _ in weaknesses]

    notes: list[str] = []
    if evidence.get("observations", 0) >= 3 and evidence.get("reference_price"):
        ref = evidence["reference_price"]
        if ref:
            notes.append(f"מחיר ייחוס שנצפה: {ref:,.0f}")
    if evidence.get("discount_corroborated") is True:
        notes.append("ההנחה מאומתת מול היסטוריית המחירים")
    elif evidence.get("discount_corroborated") is False:
        notes.append("ההנחה המוצגת אינה מאומתת")
    if cost["converted"]:
        notes.append(f"הומר מ-{cost['native_currency']} לצורך השוואה הוגנת")
    if "over_budget" in fit_reasons:
        notes.append("מעל התקציב שציינת")
    if "category_affinity" in fit_reasons or "brand_affinity" in fit_reasons:
        notes.append("תואם להעדפות שהתגבשו מהבחירות הקודמות שלך")

    summary = " · ".join(positive) if positive else "התאמה משוקללת לפי JET Score"
    if negative:
        summary += " · לשים לב: " + ", ".join(negative)

    return {
        "why": summary,
        "strengths": [{"component": k, "label": COMPONENT_LABELS_HE[k],
                       "value": round(v * 100, 1)} for k, v in strengths],
        "weaknesses": [{"component": k, "label": COMPONENT_LABELS_HE[k],
                        "value": round(v * 100, 1)} for k, v in weaknesses],
        "notes": notes,
    }
