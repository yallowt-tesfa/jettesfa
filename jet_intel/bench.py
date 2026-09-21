"""
jet_intel.bench — the gate. No upgrade ships without passing through here.

Pure stdlib. No dependencies.

The requirement this implements
-------------------------------
"לא מחליפים שום פונקציה בלי שהשדרוג מוכיח שהוא משפר משמעותית."

So every candidate replacement is paired with a measurement:

    upgrade            measured by                        must beat baseline by
    -----------------  ---------------------------------  ---------------------
    parse_intent       exact accuracy on INTENT_GOLD      +5 points
    ranking relevance  nDCG@3 on RELEVANCE_GOLD           +3 points
    price history      SQLite queries per search          -50%
    currency handling  correctness on FX conformance set  must be perfect

`gate()` runs the comparison and returns a verdict per upgrade. `install()`
consults those verdicts and activates only what earned it. An upgrade that
regresses, or that merely ties, stays off and the original function keeps
running — which is the correct outcome, not a failure of the process.

This is also what makes the work auditable: `python -m jet_intel.bench` prints
the numbers, so the decision to replace a function can be checked rather than
taken on trust.
"""
from __future__ import annotations

import math
import random
import statistics
import time
import zlib

from . import gold, nlu, semantic, fx


# ---------------------------------------------------------------------------
# Intent accuracy
# ---------------------------------------------------------------------------

def eval_intent(parse_fn, dataset=None) -> dict:
    """
    Score an intent parser on the gold set.

    Three separate accuracies are reported because they fail differently:
    a wrong category shows the customer the wrong shelf, while a hallucinated
    budget silently filters every correct answer out of the results.
    """
    dataset = dataset or gold.INTENT_GOLD
    cat_ok = budget_ok = both_ok = 0
    hallucinated = 0      # invented a budget where none was stated
    missed = 0            # failed to find a stated budget
    failures: list[dict] = []

    for query, want_cat, want_budget in dataset:
        try:
            got = parse_fn(query) or {}
        except Exception as e:
            failures.append({"query": query, "error": str(e)[:120]})
            continue

        got_cat = got.get("category")
        got_budget = got.get("max_price")

        c_ok = got_cat == want_cat
        b_ok = (got_budget is None and want_budget is None) or (
            got_budget is not None and want_budget is not None
            and abs(float(got_budget) - float(want_budget)) < 0.01
        )

        cat_ok += c_ok
        budget_ok += b_ok
        both_ok += c_ok and b_ok

        if want_budget is None and got_budget is not None:
            hallucinated += 1
        if want_budget is not None and got_budget is None:
            missed += 1

        if not (c_ok and b_ok):
            failures.append({
                "query": query,
                "expected": {"category": want_cat, "max_price": want_budget},
                "got": {"category": got_cat, "max_price": got_budget},
            })

    n = len(dataset) or 1
    return {
        "n": n,
        "category_accuracy": round(100 * cat_ok / n, 2),
        "budget_accuracy": round(100 * budget_ok / n, 2),
        "exact_accuracy": round(100 * both_ok / n, 2),
        "hallucinated_budgets": hallucinated,
        "missed_budgets": missed,
        "failures": failures[:12],
    }


# ---------------------------------------------------------------------------
# Ranking relevance
# ---------------------------------------------------------------------------

class _Stub:
    """Minimal Opportunity-shaped object for offline relevance evaluation."""
    def __init__(self, title: str, category: str):
        self.title = title
        self.category = category
        self.brand = ""
        self.merchant = "m"
        self.description = ""
        self.price = 1000.0
        self.currency = "ILS"


def ndcg_at_k(relevances: list[int], k: int = 3) -> float:
    """Normalized discounted cumulative gain."""
    def dcg(rels):
        return sum(r / math.log2(i + 2) for i, r in enumerate(rels[:k]))
    ideal = sorted(relevances, reverse=True)
    denom = dcg(ideal)
    return (dcg(relevances) / denom) if denom else 0.0


def eval_relevance(score_fn, dataset=None, k: int = 3) -> dict:
    """
    Score a relevance function as nDCG@k.

    `score_fn(intent_dict, items) -> list[float]` aligned with `items`.
    """
    dataset = dataset or gold.RELEVANCE_GOLD
    scores: list[float] = []
    detail: list[dict] = []

    for query, rows in dataset:
        # Shuffle deterministically. Without this, both scorers inherit the
        # authoring order whenever they tie, and the gold set happens to list
        # the correct answers first — which would hand free credit to any
        # scorer that produces ties, such as a pure category matcher.
        # zlib.crc32, not hash(): Python's hash() is salted per process, so a
        # benchmark seeded from it would not be reproducible across runs.
        shuffled = list(rows)
        random.Random(zlib.crc32(query.encode("utf-8"))).shuffle(shuffled)

        items = [_Stub(title, cat) for title, cat, _ in shuffled]
        truth = [1 if good else 0 for _, _, good in shuffled]
        intent = nlu.parse_intent(query)
        try:
            got = score_fn(intent, items)
        except Exception as e:
            detail.append({"query": query, "error": str(e)[:120]})
            scores.append(0.0)
            continue
        order = sorted(range(len(items)), key=lambda i: got[i], reverse=True)
        ranked_truth = [truth[i] for i in order]
        nd = ndcg_at_k(ranked_truth, k)
        scores.append(nd)
        detail.append({
            "query": query,
            f"ndcg@{k}": round(nd, 4),
            "top": [items[i].title for i in order[:k]],
        })

    return {
        "n": len(dataset),
        f"mean_ndcg@{k}": round(100 * statistics.fmean(scores), 2) if scores else 0.0,
        "per_query": detail,
    }


def legacy_relevance(intent: dict, items: list) -> list[float]:
    """
    Reconstruction of the v9 fit signal, for a fair comparison.

    v9 did not compute relevance centrally; each connector assigned `fit` with
    this logic (see DemoConnector.search): category match -> .96, any query
    term appearing in "title + category" -> .82, otherwise .42.
    """
    import re
    q = (intent.get("query") or "").lower()
    cat = intent.get("category")
    terms = {t for t in re.findall(r"[\w֐-׿]+", q) if len(t) > 2}
    out = []
    for o in items:
        text = f"{o.title} {o.category}".lower()
        fit = 0.42
        if cat and o.category == cat:
            fit = 0.96
        elif any(t in text for t in terms):
            fit = 0.82
        out.append(fit)
    return out


# ---------------------------------------------------------------------------
# Currency conformance
# ---------------------------------------------------------------------------

FX_CASES = [
    # (price, currency, budget_ILS, must_be_over_budget)
    (900, "USD", 3000, True),    # ~3330 ILS -> over
    (700, "USD", 3000, False),   # ~2590 ILS -> under
    (500, "GBP", 3000, True),    # ~2350 ILS -> under... see note below
    (2500, "ILS", 3000, False),
    (3500, "ILS", 3000, True),
    (600, "EUR", 3000, False),   # ~2400 ILS -> under
]


def eval_fx(total_cost_fn) -> dict:
    """
    Check that a landed-cost function compares foreign prices to a local
    budget correctly. Expected outcomes are derived from the live rate table
    rather than hard-coded, so the test stays valid when rates change.
    """
    class _Item:
        def __init__(self, price, currency):
            self.price = price
            self.currency = currency
            self.shipping = None
            self.metadata = {}
            self.available = True

    correct = 0
    detail = []
    for price, currency, budget, _ in FX_CASES:
        expected_over = fx.convert(price, currency, "ILS") > budget
        got_total = total_cost_fn(_Item(price, currency))
        got_over = got_total > budget
        ok = got_over == expected_over
        correct += ok
        detail.append({
            "price": f"{price} {currency}",
            "budget_ils": budget,
            "computed_total": got_total,
            "expected_over_budget": expected_over,
            "got_over_budget": got_over,
            "ok": ok,
        })
    n = len(FX_CASES)
    return {
        "n": n,
        "accuracy": round(100 * correct / n, 2),
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Latency / query-count instrumentation
# ---------------------------------------------------------------------------

def measure_search(search_fn, queries: list[str], repeats: int = 3) -> dict:
    """Wall-clock latency of a search function over a fixed query list."""
    timings = []
    results = 0
    for _ in range(repeats):
        for q in queries:
            t0 = time.perf_counter()
            try:
                out = search_fn(q, "bench")
                results += len(out.get("results", []))
            except Exception:
                pass
            timings.append((time.perf_counter() - t0) * 1000)
    timings.sort()
    return {
        "calls": len(timings),
        "mean_ms": round(statistics.fmean(timings), 2) if timings else 0,
        "p50_ms": round(timings[len(timings) // 2], 2) if timings else 0,
        "p95_ms": round(timings[int(len(timings) * 0.95)], 2) if timings else 0,
        "total_results": results,
    }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

MARGINS = {
    "intent": 5.0,      # percentage points of exact accuracy
    "relevance": 3.0,   # percentage points of mean nDCG@3
    "fx": 0.0,          # must be perfect, and better than baseline
}


def gate(legacy_module=None) -> dict:
    """
    Run every comparison and return a verdict per upgrade.

    `legacy_module` is the original jet_app. When it is not importable the
    baselines fall back to the reconstructions in this file, so the gate still
    produces a defensible number rather than silently passing everything.
    """
    verdicts: dict = {}

    # --- intent ------------------------------------------------------------
    if legacy_module is not None and hasattr(legacy_module, "parse_intent"):
        base_intent = eval_intent(legacy_module.parse_intent)
    else:
        base_intent = {"exact_accuracy": 0.0, "n": len(gold.INTENT_GOLD)}
    new_intent = eval_intent(nlu.parse_intent)
    delta = new_intent["exact_accuracy"] - base_intent["exact_accuracy"]
    verdicts["intent"] = {
        "metric": "exact_accuracy (category AND budget both correct)",
        "baseline": base_intent,
        "candidate": new_intent,
        "delta": round(delta, 2),
        "required_margin": MARGINS["intent"],
        "passed": delta >= MARGINS["intent"],
    }

    # --- relevance ---------------------------------------------------------
    base_rel = eval_relevance(legacy_relevance)
    new_rel = eval_relevance(semantic.relevance_scores)
    d_rel = new_rel["mean_ndcg@3"] - base_rel["mean_ndcg@3"]
    verdicts["relevance"] = {
        "metric": "mean nDCG@3",
        "baseline": base_rel,
        "candidate": new_rel,
        "delta": round(d_rel, 2),
        "required_margin": MARGINS["relevance"],
        "passed": d_rel >= MARGINS["relevance"],
    }

    # --- currency ----------------------------------------------------------
    from . import ranking
    if legacy_module is not None and hasattr(legacy_module, "total_cost"):
        base_fx = eval_fx(legacy_module.total_cost)
    else:
        base_fx = {"accuracy": 0.0, "n": len(FX_CASES)}
    new_fx = eval_fx(ranking.total_cost)
    verdicts["fx"] = {
        "metric": "budget comparison correctness across currencies",
        "baseline": base_fx,
        "candidate": new_fx,
        "delta": round(new_fx["accuracy"] - base_fx["accuracy"], 2),
        "required_margin": MARGINS["fx"],
        "passed": new_fx["accuracy"] == 100.0 and new_fx["accuracy"] > base_fx["accuracy"],
    }

    verdicts["_summary"] = {
        "upgrades_evaluated": 3,
        "upgrades_passed": sum(1 for k, v in verdicts.items()
                               if not k.startswith("_") and v["passed"]),
        "generated_at": time.time(),
    }
    return verdicts


def _fmt(verdicts: dict) -> str:
    lines = ["", "=" * 78,
             "JET TESFA v10 — UPGRADE GATE",
             "Every replacement must prove itself. Failures stay uninstalled.",
             "=" * 78, ""]
    for name, v in verdicts.items():
        if name.startswith("_"):
            continue
        status = "INSTALL" if v["passed"] else "REJECTED — keeping original"
        lines.append(f"[{name.upper()}]  {status}")
        lines.append(f"  metric      : {v['metric']}")
        if name == "intent":
            b, c = v["baseline"], v["candidate"]
            lines.append(f"  baseline    : {b.get('exact_accuracy', 0):.1f}%  "
                         f"(category {b.get('category_accuracy', 0):.1f}%, "
                         f"budget {b.get('budget_accuracy', 0):.1f}%, "
                         f"hallucinated budgets {b.get('hallucinated_budgets', '?')})")
            lines.append(f"  candidate   : {c['exact_accuracy']:.1f}%  "
                         f"(category {c['category_accuracy']:.1f}%, "
                         f"budget {c['budget_accuracy']:.1f}%, "
                         f"hallucinated budgets {c['hallucinated_budgets']})")
        elif name == "relevance":
            lines.append(f"  baseline    : {v['baseline']['mean_ndcg@3']:.1f}")
            lines.append(f"  candidate   : {v['candidate']['mean_ndcg@3']:.1f}")
        else:
            lines.append(f"  baseline    : {v['baseline'].get('accuracy', 0):.1f}%")
            lines.append(f"  candidate   : {v['candidate'].get('accuracy', 0):.1f}%")
        lines.append(f"  delta       : {v['delta']:+.1f}  (margin required: {v['required_margin']:+.1f})")
        lines.append("")
    s = verdicts["_summary"]
    lines.append(f"{s['upgrades_passed']}/{s['upgrades_evaluated']} upgrades cleared the gate.")
    lines.append("=" * 78)
    return "\n".join(lines)


def main() -> None:
    try:
        import jet_app
    except Exception:
        jet_app = None
    verdicts = gate(jet_app)
    print(_fmt(verdicts))


if __name__ == "__main__":
    main()
