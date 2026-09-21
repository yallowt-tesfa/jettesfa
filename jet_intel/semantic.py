"""
jet_intel.semantic — zero-dependency semantic relevance.

Pure stdlib. No model downloads, no vector database, no network.

The legacy `fit` score was set by the connector, usually from a single
substring test: an item either contained a query term (fit .82) or did not
(fit .42). That is a two-valued signal driving a weight of 0.24–0.27 in the
final ranking, which means most of the ranking was decided by fields the
connectors hard-coded rather than by how well the item answers the request.

This module computes a real relevance score from three complementary views:

  1. BM25 over the candidate set.  Classic, well-understood lexical ranking
     with term saturation and length normalization. Rare, discriminating words
     ("אלחוטי", "oled") count far more than common ones ("מכונה", "the").

  2. Character n-gram cosine.  A cheap stand-in for embeddings that survives
     morphology, typos and transliteration: "מקבוק" vs "macbook" share no
     token but do share trigram structure once transliterated, and
     "אוזניה"/"אוזניות" overlap heavily. This catches matches BM25 misses.

  3. Structured agreement.  Category, brand and numeric specs extracted by
     jet_intel.nlu are matched against the candidate's own fields, so a
     request for 75 inches prefers a 75-inch television over an 85-inch one.

The three are blended into one 0..1 score. It is deterministic, costs
microseconds per item, and needs nothing installed.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from . import hebrew, concepts

# ---------------------------------------------------------------------------
# Hebrew <-> Latin transliteration for brand and loanword matching
# ---------------------------------------------------------------------------
# Deliberately coarse. The goal is to give the n-gram comparator a shared
# alphabet, not to produce correct romanization.

_TRANSLIT = {
    "א": "a", "ב": "b", "ג": "g", "ד": "d", "ה": "h", "ו": "o", "ז": "z",
    "ח": "h", "ט": "t", "י": "i", "כ": "k", "ל": "l", "מ": "m", "נ": "n",
    "ס": "s", "ע": "a", "פ": "p", "צ": "ts", "ק": "k", "ר": "r", "ש": "sh",
    "ת": "t",
}


def translit(text: str) -> str:
    """Map Hebrew letters onto a rough Latin skeleton."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in hebrew.normalize(text))


def char_ngrams(text: str, n: int = 3) -> Counter:
    """Padded character n-grams of the transliterated form."""
    s = re.sub(r"\s+", " ", translit(text)).strip()
    if not s:
        return Counter()
    s = f"  {s}  "
    return Counter(s[i:i + n] for i in range(len(s) - n + 1))


def ngram_cosine(a: str, b: str, n: int = 3) -> float:
    """Cosine similarity between two strings' character n-gram profiles."""
    ca, cb = char_ngrams(a, n), char_ngrams(b, n)
    if not ca or not cb:
        return 0.0
    common = set(ca) & set(cb)
    if not common:
        return 0.0
    dot = sum(ca[g] * cb[g] for g in common)
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

class BM25:
    """
    Okapi BM25 over a small in-memory candidate set.

    Built fresh per search. With the tens-to-hundreds of candidates a single
    search returns, construction costs well under a millisecond.
    """

    __slots__ = ("docs", "df", "idf", "avgdl", "k1", "b", "n")

    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs = [Counter(d) for d in documents]
        self.n = len(documents)
        self.k1, self.b = k1, b
        self.avgdl = (sum(len(d) for d in documents) / self.n) if self.n else 0.0
        self.df: Counter = Counter()
        for d in documents:
            for term in set(d):
                self.df[term] += 1
        self.idf = {
            t: math.log(1 + (self.n - c + 0.5) / (c + 0.5))
            for t, c in self.df.items()
        }

    def score(self, index: int, query_terms: list[str]) -> float:
        if not self.n or index >= len(self.docs):
            return 0.0
        doc = self.docs[index]
        dl = sum(doc.values()) or 1
        total = 0.0
        for term in query_terms:
            tf = doc.get(term, 0)
            if not tf:
                continue
            idf = self.idf.get(term, 0.0)
            denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            total += idf * (tf * (self.k1 + 1)) / (denom or 1)
        return total

    def scores(self, query_terms: list[str]) -> list[float]:
        return [self.score(i, query_terms) for i in range(self.n)]


# ---------------------------------------------------------------------------
# Structured agreement
# ---------------------------------------------------------------------------

_SPEC_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z֐-׿\"']{1,12})")


def spec_agreement(intent_specs: dict, text: str) -> float:
    """
    How well the candidate's stated numbers match the ones the customer asked
    for. Returns 0.5 (neutral) when the customer asked for no specs.
    """
    if not intent_specs:
        return 0.5
    found: dict[str, float] = {}
    for m in _SPEC_IN_TEXT.finditer(hebrew.normalize(text)):
        unit = hebrew.normalize_token(m.group(2))
        if unit:
            found.setdefault(unit, float(m.group(1)))
    if not found:
        return 0.45
    hits = []
    for unit, want in intent_specs.items():
        got = found.get(unit)
        if got is None:
            continue
        if want <= 0:
            continue
        ratio = min(got, want) / max(got, want)
        hits.append(ratio)
    if not hits:
        return 0.45
    return max(0.0, min(1.0, sum(hits) / len(hits)))


def brand_agreement(intent_brands: list[str], text: str) -> float:
    """1.0 when a requested brand appears, 0.35 when brands were requested and
    none appear, 0.5 when no brand was requested."""
    if not intent_brands:
        return 0.5
    hay = " ".join(hebrew.tokenize(text))
    lat = translit(text)
    for b in intent_brands:
        if b in hay or b in lat or ngram_cosine(b, text) > 0.45:
            return 1.0
    return 0.35


# ---------------------------------------------------------------------------
# Combined relevance
# ---------------------------------------------------------------------------

def candidate_text(item) -> str:
    """Flatten the searchable surface of an Opportunity-like object."""
    parts = [
        getattr(item, "title", "") or "",
        getattr(item, "brand", "") or "",
        getattr(item, "merchant", "") or "",
        getattr(item, "category", "") or "",
        (getattr(item, "description", "") or "")[:400],
    ]
    return " ".join(p for p in parts if p)


def match_text(item) -> str:
    """
    The text used for concept, accessory and conflict analysis.

    Deliberately excludes the category slug and merchant name that
    `candidate_text` includes for lexical matching. The slug is an English
    keyword ("laptop", "phone") that is itself a concept term, so including it
    stamped `c_laptop` onto every candidate in a laptop search — which made
    concept overlap look identical for a genuine laptop and for a desktop
    tower, and silently disabled the attribute-conflict detector, since the
    conflict rule requires the opposing concept to be absent.

    Category agreement is already scored separately and as a constraint, so
    nothing is lost by keeping it out of the free-text signals.
    """
    parts = [
        getattr(item, "title", "") or "",
        getattr(item, "brand", "") or "",
        (getattr(item, "description", "") or "")[:400],
    ]
    return " ".join(p for p in parts if p)


def relevance_scores(intent: dict, items: list) -> list[float]:
    """
    Score every candidate against the intent, in one batched pass.

    Returns a list of 0..1 relevance scores aligned with `items`. BM25 raw
    scores are min-max normalized within the candidate set, because BM25 is a
    ranking function whose absolute magnitude is not comparable across queries.
    """
    if not items:
        return []

    texts = [candidate_text(o) for o in items]
    match_texts = [match_text(o) for o in items]
    docs = [hebrew.tokenize(t) for t in texts]

    query = intent.get("query", "") or ""
    q_terms = intent.get("keywords") or hebrew.tokenize(query)
    q_terms = [t for t in q_terms if len(t) > 1]

    bm = BM25(docs)
    raw = bm.scores(q_terms) if q_terms else [0.0] * len(items)
    hi = max(raw) if raw else 0.0
    lex = [(r / hi) if hi > 0 else 0.0 for r in raw]

    want_cat = intent.get("category")
    cat_conf = float(intent.get("category_confidence") or 0.0)
    brands = intent.get("brands") or []
    specs = intent.get("specs") or {}

    out: list[float] = []
    for i, o in enumerate(items):
        ng = ngram_cosine(query, texts[i])
        cat = getattr(o, "category", "") or ""
        if want_cat:
            cat_score = 1.0 if cat == want_cat else 0.30
        else:
            cat_score = 0.6
        br = brand_agreement(brands, texts[i])
        sp = spec_agreement(specs, texts[i])

        # Concept agreement bridges synonym pairs that share no characters
        # ("לפטופ"/"מחשב נייד", "סטודנטים"/"לימודים"), which is precisely
        # where the lexical and n-gram signals both score zero.
        con = concepts.overlap(query, match_texts[i])

        score = (
            0.24 * lex[i]
            + 0.13 * ng
            + 0.26 * con
            + 0.17 * cat_score
            + 0.10 * br
            + 0.10 * sp
        )

        # An accessory for the product is not the product. Lexically it is an
        # excellent match ("תיק למחשב נייד" contains the whole query), so this
        # correction cannot come from similarity at all.
        score *= (1.0 - 0.75 * concepts.accessory_penalty(query, match_texts[i]))
        score *= (1.0 - 0.45 * concepts.conflict_penalty(query, match_texts[i]))

        # Category behaves as a constraint, not as one feature among many.
        # A confidently detected category that the candidate does not belong
        # to means the candidate does not answer the need at all, and no
        # amount of textual similarity should compensate. Applied
        # multiplicatively so a wrong-category item cannot outrank a
        # right-category one that merely costs more than the stated budget —
        # which is what a purely additive category term allowed.
        if want_cat and cat_conf >= 0.55 and cat != want_cat:
            score *= 0.40

        out.append(max(0.0, min(1.0, score)))

    # Rescale across the candidate set.
    #
    # The blended score is a weighted sum of terms that are neutral (0.5) or
    # zero for a non-matching item, so raw values cluster in a narrow band
    # near the bottom of the range — in one measured case the best match
    # scored 0.27 and everything else 0.06. Feeding that into a 0.27-weighted
    # `need_fit` meant relevance contributed about five points out of a
    # hundred, and quality/trust — which connectors report at 0.9 for almost
    # everything — decided the ranking instead.
    #
    # Relevance is inherently relative to the candidate set, so it is
    # normalized within it: the best match approaches 1.0 and poor matches
    # stay near the floor. The spread is what makes `need_fit` actually
    # dominate the score, which is the whole premise of a need-first engine.
    hi, lo = max(out), min(out)
    if hi - lo > 1e-6:
        out = [0.08 + 0.90 * (v - lo) / (hi - lo) for v in out]
    elif hi > 0:
        out = [0.60 for _ in out]

    return [round(v, 4) for v in out]


def mmr_diversify(items: list, scores: list[float], k: int = 24,
                  lambda_: float = 0.78) -> list[int]:
    """
    Maximal Marginal Relevance selection.

    Without this, a feed-driven search returns twenty near-identical listings
    of the same product from the same merchant, which looks thorough and is
    useless. MMR trades a little relevance for coverage, so the customer sees
    genuinely different options.

    Returns the selected indices, best first.
    """
    n = len(items)
    if n == 0:
        return []
    k = min(k, n)
    texts = [candidate_text(o) for o in items]
    grams = [char_ngrams(t) for t in texts]

    def sim(i: int, j: int) -> float:
        a, b = grams[i], grams[j]
        common = set(a) & set(b)
        if not common:
            return 0.0
        dot = sum(a[g] * b[g] for g in common)
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        return dot / (na * nb) if na and nb else 0.0

    order = sorted(range(n), key=lambda i: scores[i], reverse=True)
    selected = [order[0]]
    pool = order[1:]

    while len(selected) < k and pool:
        best_i, best_val = None, -1e9
        for i in pool:
            redundancy = max(sim(i, s) for s in selected)
            val = lambda_ * scores[i] - (1 - lambda_) * redundancy
            if val > best_val:
                best_val, best_i = val, i
        selected.append(best_i)
        pool.remove(best_i)
    return selected
