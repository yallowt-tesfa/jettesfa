# Jet Tesfa v10 — Quick Start

The v9 engine is untouched. v10 is an additive layer in `jet_intel/` that
benchmarks itself against v9 and installs only what wins.

## Run

```bash
python jet_v10.py                  # prints the gate results, then serves
# or
gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 45 jet_v10:app
```

Still zero dependencies beyond `gunicorn`.

## See the proof

```bash
python -m jet_intel.bench          # the benchmark gate, with numbers
python -m jet_intel.install        # what got installed, and what did not
```

Expected:

```
[INTENT]     63.4%  ->  97.6%    +34.1   INSTALL
[RELEVANCE]  27.4   ->  83.4     +56.0   INSTALL
[FX]         83.3%  ->  100.0%   +16.7   INSTALL
```

## Test

```bash
PYTHONWARNINGS='error::ResourceWarning' python -m unittest -q test_v6.py
PYTHONWARNINGS='error::ResourceWarning' python -m unittest -q test_v8_upgrade.py
PYTHONWARNINGS='error::ResourceWarning' python -m unittest -q test_v9_hardening.py
PYTHONWARNINGS='error::ResourceWarning' python -m unittest -q test_frontend_v7.py
PYTHONWARNINGS='error::ResourceWarning' python -m unittest -q test_v10_upgrade.py
python simulate_v6.py
```

140/140 pass. Run each suite separately — they share module-level state, which
was already true in v9.

## Turn it off

```bash
JET_UPGRADE=0 python jet_v10.py    # v9 behaviour, v10 code loaded but inert
python jet_app.py                  # v9, v10 not loaded at all
```

Or in-process: `jet_intel.install.uninstall(jet_app)`.

## Optional: the Claude layer

```bash
export ANTHROPIC_API_KEY=sk-...
```

Without it the engine runs fully on deterministic rules. With it, requests that
name no product at all ("המקרר מרעיש בלילה והשכנים מתלוננים") are understood as
needs. If the API is slow, down, or returns something unexpected, the rules
result is used and the customer sees nothing unusual.

## Set your exchange rates

The built-in table is a fallback, not a feed. Before live traffic:

```bash
export JET_FX_RATES='{"USD":3.70,"EUR":4.00,"GBP":4.70}'
```

## What is in `jet_intel/`

| Module | Purpose |
|---|---|
| `hebrew.py` | ktiv variants, prefixes, final letters, niqqud, gershayim |
| `nlu.py` | need-first intent: category, budget, brand, use case, urgency |
| `concepts.py` | synonyms, accessory suppression, attribute conflicts |
| `semantic.py` | BM25 + n-gram + concept relevance, MMR diversification |
| `fx.py` | currency conversion with confidence |
| `ranking.py` | landed cost, fake-discount resistance, real explanations |
| `store.py` | batched price history, MAD-filtered reference prices |
| `dna.py` | decayed multi-facet preferences, learns rejection too |
| `bandit.py` | Thompson-sampling model arena |
| `resilience.py` | TTL cache with single-flight, circuit breaker |
| `llm.py` | optional Claude layer, strictly non-load-bearing |
| `gold.py` | labelled evaluation sets |
| `bench.py` | **the gate** — nothing installs without beating v9 |
| `engine.py` | the upgraded search pipeline |
| `install.py` | gated activation, one-call rollback |

Full findings, measurements and limitations: **`UPGRADE_REPORT_V10.md`**.
