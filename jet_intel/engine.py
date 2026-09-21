"""
jet_intel.engine — the upgraded search pipeline.

Pure stdlib. No dependencies.

This is the v10 replacement for `jet_app.search`. It reuses jet_app's own
connectors, database, event logging and Opportunity dataclass unchanged — the
original module is imported, never edited. What changes is the pipeline
between "connectors returned candidates" and "results go back to the browser":

    v9                                  v10
    ----------------------------------  ------------------------------------
    parse_intent (keyword + max number)  nlu + optional Claude layer
    call every connector every time      TTL cache + single-flight + breaker
    connector-assigned fit               BM25 + n-gram + concept relevance
    per-item SQLite history queries      one batched snapshot per search
    raw-float budget comparison          currency-normalized landed cost
    fixed champion model                 Thompson-sampled arena
    top-24 by score                      MMR-diversified top-24
    threshold-based "why"                contribution-based explanation

Every stage degrades safely: if the cache is cold, it computes; if a connector
is open-circuited, the others still run; if the price table is missing, the
connector's own deal value is used; if the LLM is absent, the rules run.
"""
from __future__ import annotations

import time
from dataclasses import asdict

from . import (bandit, concepts, dna as dna_mod, fx, llm, nlu, ranking,
               semantic, store as pstore)
from .resilience import BREAKERS, CONNECTOR_CACHE, SEARCH_CACHE

MODELS = ["truth_v6", "conservative_v6"]
CHAMPION = "truth_v6"
MAX_RESULTS = 24
CANDIDATE_CAP = 400


def _connector_key(connector, intent: dict) -> str:
    """Cache key for one connector's answer to one intent."""
    return CONNECTOR_CACHE.key(
        "connector", getattr(connector, "name", "?"),
        intent.get("query", ""), intent.get("category"),
        intent.get("max_price"), intent.get("min_price"),
    )


def _run_connector(connector, intent: dict) -> tuple[list, float, str | None, bool]:
    """
    Run one connector behind its circuit breaker and cache.

    Returns (items, latency_ms, error, from_cache).
    """
    name = getattr(connector, "name", "?")
    breaker = BREAKERS.get(name)

    if not breaker.allows():
        return [], 0.0, f"circuit_open ({breaker.snapshot()['retry_in_s']}s)", False

    key = _connector_key(connector, intent)
    cached = CONNECTOR_CACHE.get(key)
    if cached is not None:
        return list(cached), 0.0, None, True

    t0 = time.perf_counter()
    try:
        items = list(connector.search(intent))[:CANDIDATE_CAP]
        ms = round((time.perf_counter() - t0) * 1000, 1)
        breaker.record_success()
        CONNECTOR_CACHE.set(key, items)
        return items, ms, None, False
    except Exception as e:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        breaker.record_failure(str(e))
        return [], ms, str(e)[:180], False


def search(text: str, session: str = "guest", *, jet_app=None,
           use_llm: bool | None = None) -> dict:
    """
    Full upgraded search. Returns a superset of the v9 response shape, so the
    existing frontend renders it without modification.
    """
    if jet_app is None:
        import jet_app as jet_app   # noqa: PLW0127

    started = time.perf_counter()

    # --- 1. Understand ----------------------------------------------------
    if use_llm is None:
        use_llm = llm.enabled()
    intent = llm.understand(text) if use_llm else nlu.parse_intent(text)

    # --- 2. Profile -------------------------------------------------------
    raw_profile = jet_app.get_dna(session)
    profile = dna_mod.observe_intent(raw_profile, intent)
    try:
        jet_app.log_event(session, "search", payload=intent)
        _save_profile(jet_app, session, profile)
    except Exception:
        pass   # analytics must never break a search

    # --- 3. Gather --------------------------------------------------------
    status: list[dict] = []
    items: list = []
    enabled_connectors = []
    for c in jet_app.CONNECTORS:
        try:
            if c.enabled():
                enabled_connectors.append(c)
            else:
                status.append({"source": c.name, "enabled": False})
        except Exception as e:
            status.append({"source": getattr(c, "name", "?"), "enabled": False,
                           "error": str(e)[:120]})

    if enabled_connectors:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(8, len(enabled_connectors))) as pool:
            futures = {pool.submit(_run_connector, c, intent): c
                       for c in enabled_connectors}
            for fut in as_completed(futures):
                connector = futures[fut]
                got, ms, err, cached = fut.result()
                row = {"source": connector.name, "enabled": True,
                       "count": len(got), "latency_ms": ms, "cached": cached,
                       "breaker": BREAKERS.get(connector.name).snapshot()["state"]}
                if err:
                    row["error"] = err
                    try:
                        jet_app.record_source_health(connector.name, False, ms, 0, err)
                    except Exception:
                        pass
                else:
                    items.extend(got)
                    try:
                        jet_app.record_source_health(connector.name, True, ms, len(got))
                    except Exception:
                        pass
                status.append(row)

    # --- 4. Identify and deduplicate --------------------------------------
    keys = [jet_app.canonical_key(o) for o in items]
    snapshot = pstore.PriceSnapshot.load(jet_app.DB, keys)

    best: dict[str, tuple] = {}
    for o, key in zip(items, keys):
        prior = best.get(key)
        if prior is None:
            best[key] = (o, key)
        else:
            # Prefer the cheaper landed cost; tie-break on data completeness.
            if ranking.total_cost(o) < ranking.total_cost(prior[0]):
                best[key] = (o, key)
    deduped = [v[0] for v in best.values()]
    dedup_keys = [v[1] for v in best.values()]

    try:
        jet_app.persist(deduped, raw_profile)
    except Exception:
        pass

    # --- 5. Score ---------------------------------------------------------
    relevance = semantic.relevance_scores(intent, deduped)
    model = bandit.select(_model_stats(jet_app), MODELS, CHAMPION)

    scored = []
    for o, key, rel in zip(deduped, dedup_keys, relevance):
        bundle = ranking.score_components(o, intent, profile, rel, snapshot, key)
        value = ranking.apply_policy(bundle["components"], model)
        scored.append((o, key, bundle, value))

    scored.sort(key=lambda t: t[3], reverse=True)

    # --- 6. Diversify -----------------------------------------------------
    pool_size = min(len(scored), MAX_RESULTS * 3)
    head = scored[:pool_size]
    if head:
        picked = semantic.mmr_diversify([t[0] for t in head],
                                        [t[3] / 100.0 for t in head],
                                        k=MAX_RESULTS)
        head = [head[i] for i in picked]
    selected = head[:MAX_RESULTS]

    # MMR decides WHICH items are shown; score decides the order they are shown
    # in. Presenting them in MMR order would break the v9 contract that the
    # response is sorted by jet_score descending (test_20_rank_order), and it
    # would also be confusing: a customer reading top-to-bottom expects the
    # first result to be the best-scoring one. The diversity benefit — not
    # returning twenty listings of the same product — comes from the selection,
    # which is preserved.
    selected.sort(key=lambda t: t[3], reverse=True)

    # --- 7. Explain -------------------------------------------------------
    if selected:
        baseline = {
            k: sum(t[2]["components"][k] for t in selected) / len(selected)
            for k in ranking.WEIGHTS
        }
    else:
        baseline = None

    results = []
    for o, key, bundle, value in selected:
        comps = bundle["components"]
        cost = bundle["cost"]
        evidence = bundle["evidence"]
        decision, rationale = ranking.decide(o, intent, comps, cost, evidence)
        explanation = ranking.explain(comps, cost, evidence,
                                      bundle["fit_reasons"], baseline)

        row = asdict(o)
        row.update({
            "jet_score": value,
            "score_components": {k: round(v * 100, 1) for k, v in comps.items()},
            "total_cost": cost["total"],
            "total_cost_currency": cost["currency"],
            "native_total": cost["native_total"],
            "converted": cost["converted"],
            "savings": round(float(o.old_price) - float(o.price), 2)
                       if getattr(o, "old_price", None) and float(o.old_price) > float(o.price) else 0,
            "why": explanation["why"],
            "strengths": explanation["strengths"],
            "weaknesses": explanation["weaknesses"],
            "notes": explanation["notes"],
            "deal_confidence": round(100 * comps["deal_truth"]),
            "price_evidence": evidence,
            "decision": decision,
            "decision_reason": rationale,
        })
        results.append(row)

    elapsed = round((time.perf_counter() - started) * 1000, 1)

    return {
        "intent": intent,
        "results": results,
        "sources": status,
        "dna": dna_mod.summary(profile),
        "model": model,
        "need_alternatives": alternatives(intent),
        "engine": getattr(jet_app, "ENGINE_NAME", "Jet Tesfa"),
        # `version` reports the core engine's own version, unchanged, because
        # it is part of the v9 response contract that existing clients and the
        # regression suite assert against. The upgrade layer reports itself
        # separately rather than redefining a field someone already depends on.
        "version": getattr(jet_app, "VERSION", "9.0.0"),
        "upgrade_version": "10.0.0",
        "diagnostics": {
            "candidates": len(items),
            "after_dedupe": len(deduped),
            "returned": len(results),
            "price_history": snapshot.summary(),
            "latency_ms": elapsed,
            "intent_source": intent.get("source", "rules"),
            "base_currency": fx.BASE,
            "fx_source": fx.rate_source(),
        },
    }


def _model_stats(jet_app) -> dict:
    try:
        return jet_app.model_stats()
    except Exception:
        return {}


def _save_profile(jet_app, session: str, profile: dict) -> None:
    import json
    with jet_app.db_conn() as c:
        c.execute(
            "INSERT INTO dna VALUES(?,?,?) ON CONFLICT(session_id) "
            "DO UPDATE SET profile=excluded.profile,updated=excluded.updated",
            (session, json.dumps(profile, ensure_ascii=False), time.time()),
        )


# ---------------------------------------------------------------------------
# Need-first alternatives
# ---------------------------------------------------------------------------

_HINTS = {
    "tv": ["דגם מהשנה הקודמת באותו גודל — לרוב אותה חוויה במחיר נמוך משמעותית",
           "מסך קטן יותר במפרט גבוה יותר, אם מרחק הצפייה מאפשר"],
    "laptop": ["אחסון קטן יותר עם כונן חיצוני או ענן",
               "דגם עסקי מחודש ממוכר עם אחריות — מפרט גבוה בהרבה לאותו תקציב"],
    "phone": ["דגם מהדור הקודם — הפער בשימוש יומיומי קטן מהפער במחיר",
              "נפח אחסון נמוך יותר בתוספת גיבוי בענן"],
    "coffee": ["מכונה פשוטה יותר בתוספת מטחנה איכותית — משפיע יותר על הטעם",
               "פתרון ידני אם השימוש הוא כמה כוסות בשבוע"],
    "home": ["דגם בסיסי יותר מיצרן אמין עדיף על דגם עמוס ממותג לא מוכר",
             "בדיקת עלות התחזוקה והחלפים לאורך זמן, לא רק מחיר הקנייה"],
    "gadget": ["דור קודם של אותו מותג",
               "מותג פחות מוכר עם ביקורות עקביות לאורך זמן"],
    "fashion": ["קנייה מחוץ לעונה",
                "פריט בסיסי איכותי במקום פריט אופנתי שיוחלף מהר"],
    "travel": ["תאריכים גמישים ביומיים",
               "מיקום מעט מחוץ למרכז עם תחבורה נוחה"],
}


def alternatives(intent: dict) -> list[str]:
    """Solution classes that meet the same need, never invented products."""
    hints = list(_HINTS.get(intent.get("category") or "", [
        "חלופה זולה יותר שממלאת את אותו צורך",
        "פתרון שונה לאותה מטרה",
    ]))
    if "value" in (intent.get("preferences") or []):
        hints.append("המתנה לתקופת מבצעים ידועה, אם הצורך אינו דחוף")
    if intent.get("urgency") == "high":
        hints = ["פתרון זמין מיידית, גם אם אינו האופטימלי"] + hints
    return hints[:3]
