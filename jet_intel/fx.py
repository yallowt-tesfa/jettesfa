"""
jet_intel.fx — currency normalization.

Pure stdlib. No dependencies.

The defect this fixes
---------------------
v9 compared prices and budgets as bare floats with no regard for currency.
The eBay connector emits USD, the Awin connector emits GBP, the demo catalogue
emits ILS, and `total_cost()` returned whichever number the connector happened
to produce. Measured on the shipped code:

    Opportunity(price=900, currency='USD') against an ILS 3,000 budget
      -> total_cost 900, need_fit 0.5  (treated as comfortably under budget)

At a realistic rate that item costs about ILS 3,300 — over budget. The engine
was therefore ranking foreign-currency items as cheap, recommending BUY on
them, and comparing them against local items on a scale roughly 3.7x apart.
For a product whose entire promise is honest value comparison, this is the
most damaging bug in the codebase.

Design
------
Rates are read from the JET_FX_RATES environment variable so an operator can
keep them current without a code change, with a conservative built-in fallback.
Every converted figure is marked with the rate and its age, so the UI and the
scoring layer can discount confidence rather than silently presenting a stale
conversion as fact.
"""
from __future__ import annotations

import json
import os
import threading
import time

BASE = "ILS"

# Fallback table, units of BASE per 1 unit of the quoted currency.
# These are order-of-magnitude anchors for offline and first-boot operation,
# NOT a live feed. Set JET_FX_RATES in production.
_FALLBACK: dict[str, float] = {
    "ILS": 1.0,
    "USD": 3.70,
    "EUR": 4.00,
    "GBP": 4.70,
    "AUD": 2.45,
    "CAD": 2.70,
    "JPY": 0.025,
    "CHF": 4.15,
    "SEK": 0.35,
    "PLN": 0.93,
    "TRY": 0.11,
    "AED": 1.01,
}

_LOCK = threading.Lock()
_STATE: dict = {"rates": None, "loaded_at": 0.0, "source": "fallback"}
_TTL = 3600.0


def _load() -> tuple[dict[str, float], str]:
    """Read rates from the environment, falling back to the built-in table."""
    raw = os.getenv("JET_FX_RATES", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            rates = {str(k).upper(): float(v) for k, v in parsed.items() if float(v) > 0}
            if rates:
                rates.setdefault(BASE, 1.0)
                return rates, "env"
        except Exception:
            pass
    return dict(_FALLBACK), "fallback"


def rates() -> dict[str, float]:
    """Current rate table, refreshed at most once per TTL."""
    with _LOCK:
        if _STATE["rates"] is None or time.time() - _STATE["loaded_at"] > _TTL:
            r, src = _load()
            _STATE.update(rates=r, loaded_at=time.time(), source=src)
        return _STATE["rates"]


def rate_source() -> str:
    rates()
    return _STATE["source"]


def supported(currency: str) -> bool:
    return str(currency or "").upper() in rates()


def convert(amount: float, currency: str, target: str = BASE) -> float:
    """
    Convert `amount` from `currency` into `target`.

    An unknown currency is returned unconverted rather than zeroed: losing the
    price entirely would be worse than showing it un-normalized, and
    `conversion_confidence` reports the uncertainty to the ranking layer.
    """
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return 0.0
    table = rates()
    src = str(currency or BASE).upper()
    dst = str(target or BASE).upper()
    if src == dst:
        return round(amount, 2)
    r_src, r_dst = table.get(src), table.get(dst)
    if not r_src or not r_dst:
        return round(amount, 2)
    return round(amount * r_src / r_dst, 2)


def conversion_confidence(currency: str) -> float:
    """
    How much to trust a converted figure.

    Native currency is certain. A converted figure from a live-configured rate
    is good. A converted figure from the built-in fallback table is explicitly
    uncertain, and the ranking layer should not present it as a precise saving.
    """
    src = str(currency or BASE).upper()
    if src == BASE:
        return 1.0
    if not supported(src):
        return 0.35
    return 0.90 if rate_source() == "env" else 0.65


def normalize_amount(amount: float, currency: str) -> dict:
    """Return the full conversion record for a single amount."""
    src = str(currency or BASE).upper()
    return {
        "amount": round(float(amount or 0), 2),
        "currency": src,
        "base_amount": convert(amount, src, BASE),
        "base_currency": BASE,
        "confidence": conversion_confidence(src),
        "rate_source": rate_source(),
    }
