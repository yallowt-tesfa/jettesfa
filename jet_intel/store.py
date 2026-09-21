"""
jet_intel.store — batched price-history access and robust deal statistics.

Pure stdlib. No dependencies.

The defect this fixes
---------------------
`jet_app.historical_deal(o)` opens a fresh SQLite connection and runs a query
for a single product, and it is called from inside `score_v1`, `score_value`,
`price_truth` and `score_components`. Those in turn are called from `dedupe`
(twice per collision), `persist`, the ranking sort, and again per result when
building the response.

Measured on the shipped demo path:

    5 results  ->  45 historical_deal calls  (9 SQLite connections per result)

The demo catalogue is 5 items. A live eBay + Awin search returns 80–110
candidates, which extrapolates to roughly 700–1,000 separate SQLite
connections for one customer search, each with its own connect/query/close
cycle, all fetching from the same small table.

The fix is to load every relevant product's price history once per search into
a request-scoped snapshot, then answer all subsequent questions from memory.

Second improvement: robust statistics
-------------------------------------
v9 computed the "deal" from a plain median. A merchant that lists a product at
an inflated price for one day and then "discounts" it moves the median. Here
the reference price uses a median-absolute-deviation filter that discards
outliers before computing the reference, so a single planted high price cannot
manufacture a discount. Trend and volatility are reported too, so the engine
can tell "genuinely cheap right now" apart from "this price bounces weekly".
"""
from __future__ import annotations

import sqlite3
import statistics
import threading
import time
from contextlib import contextmanager


# ---------------------------------------------------------------------------
# Connection reuse
# ---------------------------------------------------------------------------

_LOCAL = threading.local()


@contextmanager
def reader(db_path, timeout: float = 15.0):
    """
    A per-thread read connection, reused across calls.

    SQLite connections are cheap but not free, and v9 created one per scoring
    call. Reuse keeps the WAL reader open for the life of the worker thread.
    """
    key = str(db_path)
    conns = getattr(_LOCAL, "conns", None)
    if conns is None:
        conns = _LOCAL.conns = {}
    conn = conns.get(key)
    if conn is None:
        conn = sqlite3.connect(key, timeout=timeout, check_same_thread=False)
        conn.execute("PRAGMA query_only=ON")
        conns[key] = conn
    try:
        yield conn
    except sqlite3.Error:
        try:
            conn.close()
        finally:
            conns.pop(key, None)
        raise


def close_thread_connections() -> None:
    for conn in getattr(_LOCAL, "conns", {}).values():
        try:
            conn.close()
        except Exception:
            pass
    _LOCAL.conns = {}


# ---------------------------------------------------------------------------
# Robust price statistics
# ---------------------------------------------------------------------------

def robust_reference(prices: list[float]) -> dict:
    """
    Reference price and dispersion, resistant to planted outliers.

    Uses the median absolute deviation: points more than 3 MAD from the median
    are dropped before the reference median is recomputed. With fewer than
    four observations there is not enough data to filter, so the plain median
    is returned with low confidence.
    """
    clean = [float(p) for p in prices if p and float(p) > 0]
    if not clean:
        return {"reference": None, "n": 0, "confidence": 0.0,
                "volatility": 0.0, "trend": 0.0, "low": None, "high": None}

    med = statistics.median(clean)
    if len(clean) >= 4:
        deviations = [abs(p - med) for p in clean]
        mad = statistics.median(deviations) or 0.0
        if mad > 0:
            kept = [p for p, d in zip(clean, deviations) if d <= 3 * mad]
            if len(kept) >= 3:
                clean = kept
                med = statistics.median(clean)

    spread = (statistics.pstdev(clean) / med) if med > 0 and len(clean) > 1 else 0.0

    # Trend: recent half against older half. `prices` arrives newest-first.
    trend = 0.0
    if len(clean) >= 6:
        half = len(clean) // 2
        recent = statistics.median(clean[:half])
        older = statistics.median(clean[half:])
        if older > 0:
            trend = (recent - older) / older

    # Confidence saturates around 20 observations.
    confidence = min(1.0, len(clean) / 20.0)

    return {
        "reference": round(med, 2),
        "n": len(clean),
        "confidence": round(confidence, 3),
        "volatility": round(min(1.0, spread), 4),
        "trend": round(max(-1.0, min(1.0, trend)), 4),
        "low": round(min(clean), 2),
        "high": round(max(clean), 2),
    }


# ---------------------------------------------------------------------------
# Request-scoped snapshot
# ---------------------------------------------------------------------------

class PriceSnapshot:
    """
    All price history needed for one search, loaded in one query.

    Built once per search and passed down through scoring. Every
    `historical_deal`-equivalent lookup afterwards is a dict access.
    """

    __slots__ = ("_stats", "loaded_keys", "queries", "built_at")

    def __init__(self, stats: dict[str, dict] | None = None, queries: int = 0):
        self._stats: dict[str, dict] = stats or {}
        self.loaded_keys = len(self._stats)
        self.queries = queries
        self.built_at = time.time()

    @classmethod
    def load(cls, db_path, keys: list[str], limit_per_key: int = 90) -> "PriceSnapshot":
        """Load history for `keys` using a single SELECT."""
        keys = [k for k in dict.fromkeys(keys) if k]
        if not keys:
            return cls({}, 0)

        stats: dict[str, dict] = {}
        queries = 0
        try:
            # SQLite's default variable limit is 999; chunk to stay well inside.
            for i in range(0, len(keys), 400):
                chunk = keys[i:i + 400]
                placeholders = ",".join("?" * len(chunk))
                with reader(db_path) as conn:
                    rows = conn.execute(
                        f"SELECT product_key, price FROM price_history "
                        f"WHERE product_key IN ({placeholders}) ORDER BY ts DESC",
                        chunk,
                    ).fetchall()
                queries += 1
                buckets: dict[str, list[float]] = {}
                for key, price in rows:
                    bucket = buckets.setdefault(key, [])
                    if len(bucket) < limit_per_key:
                        bucket.append(price)
                for key, prices in buckets.items():
                    stats[key] = robust_reference(prices)
        except sqlite3.Error:
            # A missing or locked table must never take a search down; an
            # empty snapshot degrades to the connector-supplied deal value.
            return cls({}, queries)
        return cls(stats, queries)

    def stats_for(self, key: str) -> dict:
        return self._stats.get(key) or {
            "reference": None, "n": 0, "confidence": 0.0,
            "volatility": 0.0, "trend": 0.0, "low": None, "high": None,
        }

    def deal_score(self, key: str, price: float, fallback: float = 0.5) -> float:
        """
        0..1 score for how good this price is against its own history.

        Blends toward `fallback` when history is thin, so a product seen twice
        does not get a confident verdict. Volatile prices are pulled toward
        neutral because "cheap today" means little when the price swings.
        """
        st = self.stats_for(key)
        ref = st["reference"]
        if not ref or ref <= 0 or price <= 0:
            return max(0.0, min(1.0, fallback))

        raw = 0.5 + (ref - price) / ref
        raw = max(0.0, min(1.0, raw))

        conf = st["confidence"]
        score = conf * raw + (1 - conf) * fallback

        volatility = st["volatility"]
        if volatility > 0.25:
            damp = min(0.5, (volatility - 0.25))
            score = score * (1 - damp) + 0.5 * damp

        return round(max(0.0, min(1.0, score)), 4)

    def summary(self) -> dict:
        return {"keys": self.loaded_keys, "queries": self.queries}


EMPTY = PriceSnapshot()
