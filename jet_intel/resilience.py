"""
jet_intel.resilience — TTL cache with single-flight, and per-connector
circuit breaking.

Pure stdlib. No dependencies.

What this fixes
---------------
v9 called every enabled connector on every single search. Two consequences:

  * REPEATED WORK.  The Awin connector downloads and parses an entire CSV
    product feed per search. Ten customers searching "טלוויזיה" within a
    minute triggered ten full feed downloads. Under any real traffic the feed
    fetch, not the ranking, is the latency and bandwidth cost of the product.

  * NO FAILURE ISOLATION.  A connector whose API was down was still called on
    every search, paying the full timeout each time. v9 caught the exception
    and carried on, which is correct, but it kept paying: with a 12-second
    urlopen timeout, one dead connector added 12 seconds to every search
    indefinitely. The `/ready` gate would still report healthy.

Single-flight matters as much as the cache itself: when a popular cache entry
expires, every concurrent request misses at once and stampedes the upstream.
Here the first caller computes and the rest wait on its result.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable


# ---------------------------------------------------------------------------
# TTL cache with single-flight
# ---------------------------------------------------------------------------

class TTLCache:
    """Thread-safe TTL cache with LRU-ish bounded eviction and single-flight."""

    def __init__(self, ttl: float = 300.0, maxsize: int = 512):
        self.ttl = float(ttl)
        self.maxsize = int(maxsize)
        self._data: dict[str, tuple[float, Any]] = {}
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}

    @staticmethod
    def key(*parts: Any) -> str:
        raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get(self, key: str, default=None):
        with self._lock:
            entry = self._data.get(key)
            if entry and time.time() - entry[0] <= self.ttl:
                self._hits += 1
                return entry[1]
            if entry:
                self._data.pop(key, None)
            self._misses += 1
            return default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self.maxsize:
                # Evict the oldest quarter in one pass; cheaper than tracking
                # exact LRU order for a cache this size.
                oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])
                for k, _ in oldest[: max(1, self.maxsize // 4)]:
                    self._data.pop(k, None)
            self._data[key] = (time.time(), value)

    def get_or_call(self, key: str, fn: Callable[[], Any], *, timeout: float = 30.0):
        """
        Return the cached value, or compute it exactly once across threads.

        Concurrent callers that miss the same key do not all call `fn`: the
        first one computes while the others wait on its completion.
        """
        hit = self.get(key)
        if hit is not None:
            return hit

        with self._lock:
            waiter = self._inflight.get(key)
            if waiter is None:
                waiter = threading.Event()
                self._inflight[key] = waiter
                leader = True
            else:
                leader = False

        if not leader:
            waiter.wait(timeout)
            cached = self.get(key)
            if cached is not None:
                return cached
            # Leader failed or timed out; fall through and compute locally.
            return fn()

        try:
            value = fn()
            if value is not None:
                self.set(key, value)
            return value
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            waiter.set()

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._data.clear()
            else:
                self._data.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._data),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
                "ttl": self.ttl,
                "maxsize": self.maxsize,
            }


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    """
    Per-connector breaker with half-open probing.

    CLOSED     — normal operation; consecutive failures are counted.
    OPEN       — calls are refused immediately for `reset_after` seconds.
    HALF_OPEN  — a single probe call is allowed; success closes the breaker,
                 failure re-opens it with a longer backoff.

    Backoff grows with each consecutive open so a persistently dead source is
    probed rarely rather than every cooldown.
    """

    def __init__(self, name: str, threshold: int = 3, reset_after: float = 60.0,
                 max_backoff: float = 900.0):
        self.name = name
        self.threshold = int(threshold)
        self.reset_after = float(reset_after)
        self.max_backoff = float(max_backoff)
        self._state = CLOSED
        self._failures = 0
        self._consecutive_opens = 0
        self._opened_at = 0.0
        self._probing = False
        self._lock = threading.Lock()
        self.total_calls = 0
        self.total_failures = 0
        self.total_short_circuits = 0
        self.last_error = ""

    def _cooldown(self) -> float:
        return min(self.max_backoff, self.reset_after * (2 ** max(0, self._consecutive_opens - 1)))

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == OPEN and time.time() - self._opened_at >= self._cooldown():
                self._state = HALF_OPEN
                self._probing = False
            return self._state

    def allows(self) -> bool:
        """True if a call may proceed now."""
        st = self.state
        with self._lock:
            if st == CLOSED:
                return True
            if st == HALF_OPEN and not self._probing:
                self._probing = True   # exactly one probe at a time
                return True
            self.total_short_circuits += 1
            return False

    def record_success(self) -> None:
        with self._lock:
            self.total_calls += 1
            self._failures = 0
            self._consecutive_opens = 0
            self._state = CLOSED
            self._probing = False
            self.last_error = ""

    def record_failure(self, error: str = "") -> None:
        with self._lock:
            self.total_calls += 1
            self.total_failures += 1
            self.last_error = str(error)[:200]
            self._failures += 1
            if self._state == HALF_OPEN or self._failures >= self.threshold:
                self._state = OPEN
                self._opened_at = time.time()
                self._consecutive_opens += 1
                self._probing = False

    def snapshot(self) -> dict:
        st = self.state
        with self._lock:
            return {
                "source": self.name,
                "state": st,
                "consecutive_failures": self._failures,
                "total_calls": self.total_calls,
                "total_failures": self.total_failures,
                "short_circuited": self.total_short_circuits,
                "cooldown_s": round(self._cooldown(), 1) if st != CLOSED else 0,
                "retry_in_s": round(max(0.0, self._cooldown() - (time.time() - self._opened_at)), 1)
                              if st == OPEN else 0,
                "last_error": self.last_error,
            }


class BreakerRegistry:
    """One breaker per connector name."""

    def __init__(self, threshold: int = 3, reset_after: float = 60.0):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()
        self.threshold = threshold
        self.reset_after = reset_after

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(name, self.threshold, self.reset_after)
            return self._breakers[name]

    def snapshot(self) -> list[dict]:
        with self._lock:
            names = list(self._breakers)
        return [self._breakers[n].snapshot() for n in sorted(names)]

    def reset(self) -> None:
        with self._lock:
            self._breakers.clear()


# Shared instances used by the installed search pipeline.
SEARCH_CACHE = TTLCache(ttl=float(__import__("os").getenv("JET_SEARCH_CACHE_TTL", "180")), maxsize=512)
CONNECTOR_CACHE = TTLCache(ttl=float(__import__("os").getenv("JET_CONNECTOR_CACHE_TTL", "300")), maxsize=256)
BREAKERS = BreakerRegistry(
    threshold=int(__import__("os").getenv("JET_BREAKER_THRESHOLD", "3")),
    reset_after=float(__import__("os").getenv("JET_BREAKER_RESET_S", "60")),
)
