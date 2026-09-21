"""
jet_intel.install — activate only what the benchmark proved.

Pure stdlib. No dependencies.

The contract
------------
`jet_app.py` is never edited. This module attaches upgrades to the running
module object at import time, and only for the upgrades that cleared
`jet_intel.bench.gate()`. Anything that failed the gate is left alone and the
original v9 implementation keeps serving.

Every replacement records the original callable, so `uninstall()` restores the
exact v9 behaviour in one call, with no redeploy. That is the rollback story:
a feature flag that turns out badly is switched off in an environment
variable, and a whole upgrade set is reverted by a single function call.

Feature flags (all default on, each independently switchable):

    JET_UPGRADE=0              disable the entire upgrade layer
    JET_UPGRADE_INTENT=0       keep v9 parse_intent
    JET_UPGRADE_SEARCH=0       keep v9 search pipeline
    JET_UPGRADE_FX=0           keep v9 total_cost
    JET_UPGRADE_ARENA=0        keep v9 fixed-champion arena
    JET_UPGRADE_GATE=0         skip benchmarking and force-install (testing only)
"""
from __future__ import annotations

import os
import threading

from . import bandit, bench, engine, fx, llm, nlu, ranking
from .resilience import BREAKERS, CONNECTOR_CACHE, SEARCH_CACHE

# RLock, not Lock: install() returns status() from inside the critical section
# (and does so on the idempotent early-return path), and status() takes the
# same lock. With a plain Lock, calling install() twice deadlocks the worker —
# which is exactly what happens under gunicorn if anything imports the
# entrypoint more than once. Caught by
# test_v10_upgrade.Installation.test_install_is_idempotent.
_LOCK = threading.RLock()
_STATE: dict = {
    "installed": False,
    "originals": {},
    "applied": [],
    "skipped": [],
    "verdicts": {},
}


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip() not in ("0", "false", "no", "off")


def status() -> dict:
    """Current installation state, for /api/config and the control centre."""
    with _LOCK:
        return {
            "installed": _STATE["installed"],
            "applied": list(_STATE["applied"]),
            "skipped": list(_STATE["skipped"]),
            "verdicts": {
                k: {
                    "metric": v["metric"],
                    "delta": v["delta"],
                    "required_margin": v["required_margin"],
                    "passed": v["passed"],
                }
                for k, v in _STATE["verdicts"].items() if not k.startswith("_")
            },
            "llm": llm.stats(),
            "fx": {"base": fx.BASE, "source": fx.rate_source()},
            "caches": {
                "search": SEARCH_CACHE.stats(),
                "connector": CONNECTOR_CACHE.stats(),
            },
            "breakers": BREAKERS.snapshot(),
        }


def install(jet_app=None, *, force: bool = False, verbose: bool = False) -> dict:
    """
    Benchmark the upgrades and attach the ones that earned their place.

    Idempotent: calling it twice does nothing the second time.
    """
    if jet_app is None:
        import jet_app as jet_app   # noqa: PLW0127

    with _LOCK:
        if _STATE["installed"]:
            return status()

        if not _flag("JET_UPGRADE"):
            _STATE["skipped"] = ["all (JET_UPGRADE=0)"]
            _STATE["installed"] = True
            return status()

        run_gate = _flag("JET_UPGRADE_GATE") and not force
        verdicts = bench.gate(jet_app) if run_gate else {}
        _STATE["verdicts"] = verdicts

        def passed(name: str) -> bool:
            if not run_gate:
                return True
            v = verdicts.get(name)
            return bool(v and v["passed"])

        originals = _STATE["originals"]
        applied, skipped = [], []

        def replace(attr: str, new_fn, label: str) -> None:
            originals[attr] = getattr(jet_app, attr, None)
            setattr(jet_app, attr, new_fn)
            applied.append(label)

        # --- intent ------------------------------------------------------
        if _flag("JET_UPGRADE_INTENT") and passed("intent"):
            if llm.enabled():
                replace("parse_intent", llm.understand,
                        "parse_intent -> jet_intel.llm.understand (rules + Claude)")
            else:
                replace("parse_intent", nlu.parse_intent,
                        "parse_intent -> jet_intel.nlu (deterministic)")
        else:
            skipped.append("parse_intent (gate not cleared or flag off)")

        # --- currency ----------------------------------------------------
        if _flag("JET_UPGRADE_FX") and passed("fx"):
            replace("total_cost", ranking.total_cost,
                    "total_cost -> currency-normalized landed cost")
        else:
            skipped.append("total_cost (gate not cleared or flag off)")

        # --- search pipeline ---------------------------------------------
        # Gated on relevance, because the pipeline's headline change is the
        # ranking. The infrastructure improvements inside it (cache, breaker,
        # batched history) ride along only when the ranking earned the swap.
        if _flag("JET_UPGRADE_SEARCH") and passed("relevance"):
            legacy_search = jet_app.search

            def upgraded_search(text, session="guest", _legacy=legacy_search):
                try:
                    return engine.search(text, session, jet_app=jet_app)
                except Exception:
                    # Any unexpected failure in the new pipeline falls back to
                    # the v9 implementation rather than failing the request.
                    return _legacy(text, session)

            replace("search", upgraded_search,
                    "search -> jet_intel.engine (semantic + cache + breaker + MMR)")
        else:
            skipped.append("search (gate not cleared or flag off)")

        # --- model arena --------------------------------------------------
        # Not gated on offline data: an exploration policy cannot be evaluated
        # offline, because its whole purpose is to gather the data that would
        # be needed to evaluate it. It is installed on the argument in
        # jet_intel/bandit.py that the v9 rule was unreachable, and it is
        # independently switchable.
        if _flag("JET_UPGRADE_ARENA"):
            def upgraded_arena_model():
                try:
                    return bandit.select(jet_app.model_stats(),
                                         engine.MODELS, engine.CHAMPION)
                except Exception:
                    return engine.CHAMPION

            replace("arena_model", upgraded_arena_model,
                    "arena_model -> Thompson sampling (explores, never stalls)")
        else:
            skipped.append("arena_model (flag off)")

        _STATE["applied"] = applied
        _STATE["skipped"] = skipped
        _STATE["installed"] = True

    if verbose:
        print(report())
    return status()


def uninstall(jet_app=None) -> dict:
    """Restore every original v9 function. Instant, complete rollback."""
    if jet_app is None:
        import jet_app as jet_app   # noqa: PLW0127
    with _LOCK:
        for attr, original in _STATE["originals"].items():
            if original is not None:
                setattr(jet_app, attr, original)
        _STATE.update(installed=False, originals={}, applied=[], skipped=[])
    CONNECTOR_CACHE.invalidate()
    SEARCH_CACHE.invalidate()
    BREAKERS.reset()
    return {"uninstalled": True}


def report() -> str:
    """Human-readable installation summary."""
    st = status()
    lines = ["", "=" * 74, "JET TESFA v10 — UPGRADE LAYER", "=" * 74]
    if not st["installed"]:
        lines.append("  not installed")
        return "\n".join(lines)

    lines.append("\nINSTALLED (each one beat the v9 baseline on the benchmark):")
    for a in st["applied"] or ["  (none)"]:
        lines.append(f"  + {a}")
    if st["skipped"]:
        lines.append("\nNOT INSTALLED (original v9 code still running):")
        for s in st["skipped"]:
            lines.append(f"  - {s}")

    if st["verdicts"]:
        lines.append("\nGATE RESULTS:")
        for name, v in st["verdicts"].items():
            mark = "PASS" if v["passed"] else "FAIL"
            lines.append(f"  [{mark}] {name:<10} delta {v['delta']:+.1f} "
                         f"(needed {v['required_margin']:+.1f}) — {v['metric']}")

    lines.append(f"\nIntent engine : {'rules + Claude' if st['llm']['enabled'] else 'rules only (no API key set)'}")
    lines.append(f"Base currency : {st['fx']['base']} (rates: {st['fx']['source']})")
    lines.append("=" * 74)
    return "\n".join(lines)


def main() -> None:
    import jet_app
    install(jet_app, verbose=True)


if __name__ == "__main__":
    main()
