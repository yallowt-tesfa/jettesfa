"""
jet_intel — the Jet Tesfa v10 intelligence layer.

An additive upgrade package for the validated v9 engine. `jet_app.py` is not
modified: this package imports it, measures candidate replacements against it,
and attaches only the ones that demonstrably win.

Usage
-----
    import jet_app
    from jet_intel import install
    install.install(jet_app)          # benchmark, then attach what passed

or, without touching the entrypoint at all, run the v10 WSGI wrapper:

    gunicorn jet_v10:app

Rollback
--------
    install.uninstall(jet_app)        # every v9 function restored, in-process

or set JET_UPGRADE=0 and restart.

Modules
-------
  hebrew      Hebrew normalization: ktiv variants, prefixes, finals, gershayim
  nlu         deterministic need-first intent engine
  concepts    synonym groups, accessory and attribute-conflict detection
  semantic    BM25 + character n-gram + concept relevance, MMR diversification
  fx          currency normalization and conversion confidence
  ranking     landed cost, fake-discount resistance, contribution explanations
  store       batched price history, robust (MAD-filtered) reference prices
  dna         time-decayed multi-facet preference model with rejection learning
  bandit      Thompson-sampling model arena
  resilience  TTL cache with single-flight, per-connector circuit breaker
  llm         optional Claude intent layer, strictly non-load-bearing
  gold        labelled evaluation sets
  bench       the gate: every replacement must beat the v9 baseline
  engine      the upgraded search pipeline
  install     benchmark-gated activation and one-call rollback
"""

__version__ = "10.0.0"
__all__ = [
    "hebrew", "nlu", "concepts", "semantic", "fx", "ranking", "store",
    "dna", "bandit", "resilience", "llm", "gold", "bench", "engine", "install",
]
