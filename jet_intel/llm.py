"""
jet_intel.llm — optional Claude-backed intent understanding.

Pure stdlib (urllib). No SDK, no dependency added to requirements.txt.

Position in the system
----------------------
This layer is strictly optional and strictly additive. The deterministic
engine in jet_intel.nlu is the system of record: it runs first, always, and
its result is what gets used if anything here is unavailable, slow, malformed
or disabled. Removing the API key returns the product to a fully working
state with no code change.

That ordering is deliberate. An engine that cannot answer a customer when a
third-party API has an incident is not a production engine. The LLM adds
nuance where nuance exists; it is never load-bearing.

What it adds that rules cannot
------------------------------
Rules handle "טלוויזיה 75 אינץ עד 5000". They do not handle:

    "המקרר שלי מרעיש נורא בלילה והשכנים מתלוננים"
      -> category home, use_case family, attribute preference "quiet",
         urgency high, and the understanding that the need is a replacement,
         not an upgrade.

    "אני מתחיל תואר בהנדסה ואין לי הרבה כסף"
      -> category laptop, use_case study, preference value, an inferred
         budget band, and a required spec profile for engineering software.

Safety properties
-----------------
  * Hard timeout, single attempt, no retry storm.
  * The response is parsed against a strict allow-list; any field that is not
    recognized, out of range or of the wrong type is discarded, not trusted.
  * Merge is conservative: the LLM may fill gaps the rules left empty and may
    raise confidence, but it cannot overwrite a high-confidence rule result.
  * Results are cached by normalized query, so repeated searches cost nothing.
  * Every failure mode degrades to the rules result and is counted for the
    control centre.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import hebrew, nlu
from .resilience import TTLCache, CircuitBreaker

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

CACHE = TTLCache(ttl=float(os.getenv("JET_LLM_CACHE_TTL", "1800")), maxsize=1024)
BREAKER = CircuitBreaker("llm", threshold=3, reset_after=120.0)

_STATS = {"calls": 0, "hits": 0, "failures": 0, "timeouts": 0,
          "rejected_payloads": 0, "disabled": 0}
_STATS_LOCK = threading.Lock()

VALID_CATEGORIES = set(nlu._LEXICON.keys())
VALID_PREFERENCES = set(nlu._PREF_CUES.keys())
VALID_USE_CASES = set(nlu._USE_CASES.keys())
VALID_URGENCY = {"low", "normal", "high"}

SYSTEM_PROMPT = """You extract structured shopping intent from a customer's own words, in Hebrew or English.

Return ONLY a JSON object. No prose, no markdown fence.

Schema:
{
  "category": one of ["tv","phone","laptop","coffee","travel","home","fashion","gadget"] or null,
  "max_price": number or null,          // spending ceiling ONLY. Never a screen size, storage size, model number or quantity.
  "min_price": number or null,
  "currency": "ILS"|"USD"|"EUR"|"GBP" or null,
  "preferences": subset of ["quality","value","delivery","durability","compact","quiet"],
  "use_case": one of ["study","work","gaming","creative","family","gift","travel"] or null,
  "urgency": "low"|"normal"|"high",
  "brands": [lowercase brand names the customer named],
  "keywords": [up to 8 content words describing the actual need],
  "underlying_need": one short sentence, in the customer's own language, naming the problem to solve rather than the product,
  "confidence": 0.0 to 1.0
}

Rules:
- Infer the need, not just the words. "המקרר מרעיש בלילה" is a home appliance need with a "quiet" preference and high urgency.
- Set max_price to null unless the customer actually expressed a spending limit. Numbers attached to units (inch, GB, TB, GHz, mAh) are specifications, never prices.
- Never invent a budget. A missing budget is correct and useful information.
- Set confidence below 0.5 when the request is too vague to act on."""


def enabled() -> bool:
    """True when an API key is configured and the layer is not switched off."""
    if os.getenv("JET_LLM_ENABLED", "1").strip() == "0":
        return False
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def model_name() -> str:
    return os.getenv("JET_LLM_MODEL", "claude-sonnet-4-5").strip()


def _bump(counter: str, n: int = 1) -> None:
    with _STATS_LOCK:
        _STATS[counter] = _STATS.get(counter, 0) + n


def stats() -> dict:
    with _STATS_LOCK:
        out = dict(_STATS)
    out["enabled"] = enabled()
    out["model"] = model_name() if enabled() else None
    out["breaker"] = BREAKER.snapshot()
    out["cache"] = CACHE.stats()
    return out


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _call_api(query: str, timeout: float) -> dict | None:
    """One request to the Messages API. Returns parsed JSON or None."""
    payload = json.dumps({
        "model": model_name(),
        "max_tokens": 600,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": query[:1000]}],
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "content-type": "application/json",
            "x-api-key": os.getenv("ANTHROPIC_API_KEY", "").strip(),
            "anthropic-version": API_VERSION,
            "user-agent": "JetTesfa/10.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    for block in body.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
            return json.loads(text)
    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _sanitize(raw: dict) -> dict:
    """
    Keep only fields that match the schema exactly.

    Anything unexpected is dropped rather than coerced. A model that returns a
    budget of "about 3000" or a category of "electronics" contributes nothing
    instead of contributing a wrong value the ranking would then act on.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}

    cat = raw.get("category")
    if isinstance(cat, str) and cat in VALID_CATEGORIES:
        out["category"] = cat

    for field in ("max_price", "min_price"):
        val = raw.get(field)
        if isinstance(val, (int, float)) and 0 < float(val) <= 10_000_000:
            out[field] = float(val)

    cur = raw.get("currency")
    if isinstance(cur, str) and cur.upper() in {"ILS", "USD", "EUR", "GBP"}:
        out["currency"] = cur.upper()

    prefs = raw.get("preferences")
    if isinstance(prefs, list):
        clean = [p for p in prefs if isinstance(p, str) and p in VALID_PREFERENCES]
        if clean:
            out["preferences"] = clean[:6]

    uc = raw.get("use_case")
    if isinstance(uc, str) and uc in VALID_USE_CASES:
        out["use_case"] = uc

    urg = raw.get("urgency")
    if isinstance(urg, str) and urg in VALID_URGENCY:
        out["urgency"] = urg

    brands = raw.get("brands")
    if isinstance(brands, list):
        clean = [str(b).lower()[:40] for b in brands if isinstance(b, str) and b.strip()]
        if clean:
            out["brands"] = clean[:5]

    kws = raw.get("keywords")
    if isinstance(kws, list):
        clean = [str(k)[:40] for k in kws if isinstance(k, str) and k.strip()]
        if clean:
            out["keywords"] = clean[:8]

    need = raw.get("underlying_need")
    if isinstance(need, str) and 0 < len(need) <= 300:
        out["underlying_need"] = need.strip()

    conf = raw.get("confidence")
    out["confidence"] = float(conf) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else 0.5

    return out


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge(rules: dict, llm: dict) -> dict:
    """
    Combine rules and LLM output, with the rules holding the deciding vote
    wherever they were confident.

    The asymmetry is intentional. The deterministic parser is auditable,
    testable and cannot hallucinate; it should not be overridden by a model
    that is merely fluent. The LLM's value is in the gaps.
    """
    if not llm:
        return rules

    merged = dict(rules)
    llm_conf = llm.get("confidence", 0.5)

    # Category: fill a gap, or override only a clearly weak rule result with a
    # clearly confident model result.
    if llm.get("category"):
        if not merged.get("category"):
            merged["category"] = llm["category"]
            merged["category_confidence"] = min(0.85, llm_conf)
        elif merged.get("category_confidence", 0) < 0.55 and llm_conf >= 0.75:
            merged["category"] = llm["category"]
            merged["category_confidence"] = llm_conf

    # Budget: the rules parser never invents one, so an LLM budget only fills
    # a genuine gap. It never overrides a budget the customer stated outright.
    if llm.get("max_price") and not merged.get("max_price"):
        if llm_conf >= 0.6:
            merged["max_price"] = llm["max_price"]
            merged["price_confidence"] = min(0.7, llm_conf)
    if llm.get("min_price") and not merged.get("min_price"):
        merged["min_price"] = llm["min_price"]
    if llm.get("currency") and not merged.get("currency"):
        merged["currency"] = llm["currency"]

    # Additive fields: union, rules first.
    for field in ("preferences", "brands"):
        combined = list(merged.get(field) or [])
        for v in llm.get(field) or []:
            if v not in combined:
                combined.append(v)
        merged[field] = combined[:8]

    if llm.get("use_case") and not merged.get("use_case"):
        merged["use_case"] = llm["use_case"]
    if llm.get("urgency") and merged.get("urgency") == "normal":
        merged["urgency"] = llm["urgency"]

    # Keywords broaden retrieval, so union them.
    kws = list(merged.get("keywords") or [])
    for k in llm.get("keywords") or []:
        norm = hebrew.normalize_token(k)
        if norm and norm not in kws:
            kws.append(norm)
    merged["keywords"] = kws[:24]

    if llm.get("underlying_need"):
        merged["underlying_need"] = llm["underlying_need"]

    merged["source"] = "rules+llm"
    merged["llm_confidence"] = llm_conf
    return merged


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def understand(text: str, timeout: float | None = None) -> dict:
    """
    Full intent understanding: rules always, LLM when it can help.

    Never raises. Never blocks longer than the configured timeout. Always
    returns a usable intent dict.
    """
    rules = nlu.parse_intent(text)

    if not enabled():
        _bump("disabled")
        return rules

    if not BREAKER.allows():
        return rules

    timeout = timeout if timeout is not None else float(os.getenv("JET_LLM_TIMEOUT", "6"))
    cache_key = CACHE.key("llm-intent", model_name(), hebrew.normalize(text))

    cached = CACHE.get(cache_key)
    if cached is not None:
        _bump("hits")
        return merge(rules, cached)

    started = time.perf_counter()
    try:
        _bump("calls")
        raw = _call_api(text, timeout)
        clean = _sanitize(raw or {})
        if not clean:
            _bump("rejected_payloads")
            BREAKER.record_success()   # the API answered; the payload was poor
            return rules
        BREAKER.record_success()
        CACHE.set(cache_key, clean)
        clean["_latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return merge(rules, clean)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if "timed out" in str(reason).lower():
            _bump("timeouts")
        _bump("failures")
        BREAKER.record_failure(str(reason))
        return rules
    except (TimeoutError, OSError) as e:
        _bump("timeouts")
        BREAKER.record_failure(str(e))
        return rules
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        _bump("rejected_payloads")
        BREAKER.record_failure(f"bad payload: {e}")
        return rules
    except Exception as e:                      # never take a search down
        _bump("failures")
        BREAKER.record_failure(str(e))
        return rules
