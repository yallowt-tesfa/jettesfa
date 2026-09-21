"""
jet_intel.nlu — deterministic need-first intent understanding.

Pure stdlib. No dependencies. No network. Always available.

This module replaces `jet_app.parse_intent` only if `jet_intel.bench` proves
it beats the legacy parser on the labelled gold set. See jet_intel/install.py.

Three defects in the legacy parser are addressed directly:

  1. BUDGET FROM UNITS.  The legacy parser took `max(all numbers >= 3 digits)`
     as the budget. "אייפון 15 פרו 256 ג״ב" therefore produced a budget of 256,
     and every candidate was then filtered out as over budget. Here a number
     becomes a budget only when a budget cue or a currency marker vouches for
     it, and never when a measurement unit claims it.

  2. SUBSTRING CATEGORY MATCHING.  "cheap headphones" matched the category
     `phone` because "phone" is a substring of "headphones". Matching is now
     token-based with explicit multi-word phrase support.

  3. NO HEBREW VARIANT COVERAGE.  "טלויזיה" (ktiv haser) and "אייפון" did not
     match anything. The lexicon is indexed through jet_intel.hebrew so every
     spelling variant, prefix form and final-letter form collapses onto one key.

It also extracts signals the legacy parser had no concept of: intended use,
urgency, brand affinity, hard constraints, and the distinction between a
maximum price and a price floor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from . import hebrew

# ---------------------------------------------------------------------------
# Category lexicon
# ---------------------------------------------------------------------------
# Each entry maps a category to (single tokens, multi-word phrases).
# Written in natural Hebrew/English; hebrew.token_variants() expands each one
# into the canonical forms that user input normalizes to.

_LEXICON: dict[str, dict[str, list[str]]] = {
    "tv": {
        "tokens": ["טלוויזיה", "טלויזיה", "טלביזיה", "טלוויזיות", "מסך", "מסכים", "צג",
                   "television", "tv", "qled", "oled", "led", "smarttv"],
        "phrases": ["smart tv", "4k tv", "8k tv", "מסך גדול", "מסך חכם"],
    },
    "phone": {
        "tokens": ["טלפון", "סמארטפון", "סמרטפון", "פלאפון", "נייד", "אייפון", "איפון",
                   "גלקסי", "שיאומי", "iphone", "galaxy", "smartphone", "pixel", "xiaomi"],
        "phrases": ["טלפון נייד", "מכשיר סלולרי", "mobile phone", "cell phone"],
    },
    "laptop": {
        "tokens": ["מחשב", "לפטופ", "מחברת", "נייד", "מקבוק", "laptop", "notebook",
                   "macbook", "ultrabook", "chromebook", "pc"],
        "phrases": ["מחשב נייד", "מחשב נייח", "מחשב שולחני", "gaming laptop", "מחשב לגיימינג"],
    },
    "coffee": {
        "tokens": ["קפה", "אספרסו", "מטחנה", "coffee", "espresso", "nespresso", "grinder"],
        "phrases": ["מכונת קפה", "coffee machine", "coffee maker"],
    },
    "travel": {
        "tokens": ["מלון", "טיסה", "טיסות", "חופשה", "נופש", "מלונות", "hotel", "flight",
                   "travel", "vacation", "resort", "airbnb"],
        "phrases": ["חבילת נופש", "כרטיס טיסה", "בית מלון"],
    },
    "home": {
        "tokens": ["שואב", "מקרר", "מדיח", "תנור", "מכונת", "מיקרוגל", "מזגן",
                   "vacuum", "fridge", "refrigerator", "oven", "dishwasher", "microwave"],
        "phrases": ["שואב אבק", "מכונת כביסה", "מייבש כביסה", "washing machine", "air conditioner"],
    },
    "fashion": {
        "tokens": ["בגד", "בגדים", "נעל", "נעליים", "חולצה", "מכנסיים", "מעיל", "תיק",
                   "fashion", "shoe", "shoes", "sneakers", "jacket", "shirt", "bag"],
        "phrases": ["נעלי ספורט", "running shoes"],
    },
    "gadget": {
        "tokens": ["גאדגט", "אוזניות", "שעון", "רמקול", "טאבלט", "מטען", "אייפד",
                   "מצלמה", "מצלמת", "camera", "רחפן", "drone", "קונסולה", "console",
                   "gadget", "headphone", "headphones", "earbuds", "smartwatch",
                   "speaker", "tablet", "ipad", "airpods", "charger"],
        "phrases": ["אוזניות אלחוטיות", "שעון חכם", "smart watch", "noise cancelling"],
    },
}


def _build_index() -> tuple[dict[str, set[str]], list[tuple[str, str]]]:
    """Expand the lexicon into canonical token -> categories, and phrase list."""
    token_index: dict[str, set[str]] = {}
    phrases: list[tuple[str, str]] = []
    for cat, spec in _LEXICON.items():
        for tok in spec["tokens"]:
            for v in hebrew.token_variants(tok):
                token_index.setdefault(v, set()).add(cat)
        for ph in spec["phrases"]:
            phrases.append((" ".join(hebrew.tokenize(ph)), cat))
    return token_index, phrases


TOKEN_INDEX, PHRASE_INDEX = _build_index()

# Tokens that appear in more than one category are ambiguous on their own
# ("נייד" is both phone and laptop; "מחשב" leans laptop). Phrase matches and
# co-occurring evidence break the tie; a lone ambiguous token scores lower.
AMBIGUOUS = {t for t, cats in TOKEN_INDEX.items() if len(cats) > 1}

# ---------------------------------------------------------------------------
# Price / quantity understanding
# ---------------------------------------------------------------------------

# Words that mark the number following (or preceding) them as a spending ceiling.
_MAX_CUES = [
    "עד", "מתחת", "פחות", "מקסימום", "מקס", "תקציב", "בתקציב", "לכל היותר", "לא יותר",
    "under", "below", "max", "maximum", "budget", "upto", "up to", "less than",
    "no more than", "at most", "within",
]
# Words that mark a floor.
_MIN_CUES = ["מעל", "החל", "לפחות", "מ", "above", "over", "from", "at least", "starting"]

# Currency markers. A number adjacent to one of these is a price with high
# confidence, regardless of cue words.
_CURRENCY_TOKENS = {
    "₪": "ILS", "שח": "ILS", "שקל": "ILS", "שקלים": "ILS", "nis": "ILS", "ils": "ILS",
    "$": "USD", "דולר": "USD", "דולרים": "USD", "usd": "USD",
    "€": "EUR", "יורו": "EUR", "אירו": "EUR", "eur": "EUR",
    "£": "GBP", "לירות": "GBP", "gbp": "GBP",
}

# Measurement units. A number adjacent to one of these is a SPECIFICATION,
# never a budget. This single table is what fixes "256 ג״ב" being read as a
# budget of 256.
_UNIT_TOKENS = {
    "אינץ", "אינטש", "inch", "in", "אינצים",
    "גב", "גיגה", "גיגהבייט", "gb", "gigabyte", "tb", "טב", "טרה", "mb",
    "mp", "מגהפיקסל", "מגפיקסל", "megapixel",
    "hz", "הרץ", "ghz", "mhz", "גהרץ",
    "kg", "קג", "גרם", "gram", "ליטר", "liter", "ml", "מל",
    "וואט", "watt", "w", "mah", "ואט",
    "ram", "רם", "core", "cores", "ליבות", "ליבה",
    "שעות", "hours", "hour", "דקות", "min", "מטר", "cm", "סממ", "סמ", "mm",
}

# The optional trailing "k" is captured rather than excluded: the previous
# (?![\w]) lookahead treated "6k" as not-a-number at all, so "tv under 6k"
# yielded no budget.
_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[,،]\d{3})+|\d+(?:\.\d+)?)(k)?(?![\w])", re.I)

# Multipliers written as words: "3 אלף", "5k".
# Stored in canonical form, because the context tokens they are compared
# against have been normalized: "אלף" tokenizes to "אלפ" (final pe folded to
# medial pe), so the raw spelling would never match.
_THOUSAND = {v for w in ("אלף", "אלפים", "אלפי", "k")
             for v in hebrew.token_variants(w)}

# Currency symbols are outside the \w and Hebrew ranges the tokenizer keeps,
# so they vanish during tokenization and can only be found in the raw text.
# "שואב אבק 2500 ₪" has the shekel sign one space away from the digits, not
# flush against them, so a single-character adjacency test misses it too.
_SYMBOL_WINDOW = 3
_CURRENCY_SYMBOLS = {"₪": "ILS", "$": "USD", "€": "EUR", "£": "GBP"}


@dataclass
class Intent:
    """Structured understanding of what the customer actually needs."""
    query: str
    category: str | None = None
    category_confidence: float = 0.0
    max_price: float | None = None
    min_price: float | None = None
    currency: str | None = None
    price_confidence: float = 0.0
    preferences: list[str] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    specs: dict[str, float] = field(default_factory=dict)
    use_case: str | None = None
    urgency: str = "normal"
    keywords: list[str] = field(default_factory=list)
    source: str = "rules"

    def to_dict(self) -> dict:
        """Legacy-compatible dict. The v9 contract keys are always present."""
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Preference / use-case / urgency lexicons
# ---------------------------------------------------------------------------

_PREF_CUES = {
    "quality": ["איכות", "איכותי", "איכותית", "מעולה", "טוב", "הכי טוב", "פרימיום", "מקצועי",
                "quality", "premium", "best", "professional", "high end", "flagship"],
    "value": ["זול", "חסכוני", "משתלם", "תקציב", "כלכלי", "בזול", "סביר",
              "cheap", "value", "budget", "affordable", "bargain", "inexpensive"],
    "delivery": ["מהיר", "משלוח", "דחוף", "היום", "מחר", "עכשיו",
                 "fast", "delivery", "shipping", "urgent", "today", "tomorrow", "overnight"],
    "durability": ["עמיד", "אמין", "חזק", "לאורך זמן", "שנים", "אחריות",
                   "durable", "reliable", "warranty", "long lasting", "sturdy"],
    "compact": ["קטן", "קומפקטי", "נייד", "קל", "compact", "small", "portable", "lightweight"],
    "quiet": ["שקט", "שקטה", "רעש", "quiet", "silent", "low noise"],
}

_USE_CASES = {
    "study": ["לימודים", "סטודנט", "אוניברסיטה", "בית ספר", "study", "student", "school", "college"],
    "work": ["עבודה", "משרד", "מקצועי", "עסק", "work", "office", "business", "professional"],
    "gaming": ["גיימינג", "משחקים", "גיימר", "gaming", "games", "gamer", "esports"],
    "creative": ["עיצוב", "עריכה", "וידאו", "גרפיקה", "צילום", "design", "editing",
                 "video", "photo", "creative", "rendering"],
    "family": ["משפחה", "ילדים", "בית", "סלון", "family", "kids", "children", "living room"],
    "gift": ["מתנה", "מתנת", "יום הולדת", "gift", "present", "birthday"],
    "travel": ["טיול", "נסיעה", "חופשה", "trip", "travel", "vacation"],
}

_URGENCY = {
    "high": ["דחוף", "מיד", "עכשיו", "היום", "נשרף", "התקלקל", "נשבר", "חייב",
             "urgent", "asap", "immediately", "today", "broke", "broken", "died", "need now"],
    "low": ["מתישהו", "בעתיד", "לא דחוף", "בודק", "מתלבט", "שוקל",
            "someday", "eventually", "not urgent", "browsing", "considering", "thinking about"],
}

_BRANDS = [
    "samsung", "lg", "sony", "apple", "xiaomi", "hisense", "tcl", "philips", "toshiba",
    "dell", "hp", "lenovo", "asus", "acer", "msi", "microsoft", "google", "oneplus",
    "bosch", "siemens", "electrolux", "beko", "whirlpool", "delonghi", "breville",
    "nespresso", "dyson", "bose", "jbl", "sennheiser", "anker", "nike", "adidas",
    "סמסונג", "אלגי", "סוני", "אפל", "שיאומי", "היסנס", "פיליפס", "דל", "לנובו",
    "אסוס", "בוש", "סימנס", "דייסון", "בוס", "נייק", "אדידס", "דלונגי",
]
_BRAND_INDEX: dict[str, str] = {}
for _b in _BRANDS:
    for _v in hebrew.token_variants(_b):
        _BRAND_INDEX[_v] = _b


def _index_cues(mapping: dict[str, list[str]]) -> dict[str, str]:
    idx: dict[str, str] = {}
    for label, words in mapping.items():
        for w in words:
            key = " ".join(hebrew.tokenize(w))
            if key:
                idx[key] = label
    return idx


_PREF_INDEX = _index_cues(_PREF_CUES)
_USE_INDEX = _index_cues(_USE_CASES)
_URGENCY_INDEX = _index_cues(_URGENCY)
_MAX_CUE_SET = {" ".join(hebrew.tokenize(c)) for c in _MAX_CUES}
_MIN_CUE_SET = {" ".join(hebrew.tokenize(c)) for c in _MIN_CUES}

# Multi-word cues, longest first, matched against the joined context.
_MAX_CUE_PHRASES = sorted((c for c in _MAX_CUE_SET if " " in c),
                          key=lambda c: -len(c))
_MIN_CUE_PHRASES = sorted((c for c in _MIN_CUE_SET if " " in c),
                          key=lambda c: -len(c))
_UNIT_SET = {v for u in _UNIT_TOKENS for v in hebrew.token_variants(u)}
_CURRENCY_INDEX = {v: code for tok, code in _CURRENCY_TOKENS.items()
                   for v in (hebrew.token_variants(tok) if tok.isalpha() else {tok})}


# ---------------------------------------------------------------------------
# Number extraction
# ---------------------------------------------------------------------------

@dataclass
class _NumberHit:
    value: float
    start: int
    end: int
    before: str
    after: str
    currency: str | None = None
    is_unit: bool = False
    cue: str | None = None          # "max" | "min" | None
    confidence: float = 0.0


def _context_tokens(text: str, start: int, end: int, width: int = 22) -> tuple[list[str], list[str]]:
    """Canonical tokens immediately before and after a number span."""
    before = hebrew.tokenize(text[max(0, start - width):start])
    after = hebrew.tokenize(text[end:end + width])
    return before[-3:], after[:3]


def _scan_numbers(text: str) -> list[_NumberHit]:
    norm = hebrew.normalize(text)
    hits: list[_NumberHit] = []
    for m in _NUM_RE.finditer(norm):
        raw = m.group(1).replace(",", "").replace("،", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        before, after = _context_tokens(norm, m.start(), m.end())

        # Literal currency symbols sit flush against the digits, so check the
        # raw characters as well as the tokenized context.
        left_char = norm[m.start() - 1] if m.start() else ""
        right_char = norm[m.end()] if m.end() < len(norm) else ""

        hit = _NumberHit(value=value, start=m.start(), end=m.end(),
                         before=" ".join(before), after=" ".join(after))

        # "3 אלף" / "5k" -> 3000 / 5000
        if m.group(2):                      # trailing "k" captured by the regex
            hit.value = value * 1000
        elif after and after[0] in _THOUSAND:
            hit.value = value * 1000

        # Look for a currency symbol in a small raw-text window on either
        # side, not only flush against the digits.
        left_win = norm[max(0, m.start() - _SYMBOL_WINDOW):m.start()]
        right_win = norm[m.end():m.end() + _SYMBOL_WINDOW]
        for sym, code in _CURRENCY_SYMBOLS.items():
            if sym in left_win or sym in right_win:
                hit.currency = code
                break
        if hit.currency is None:
            for tok in (after[:1] + before[-1:]):
                if tok in _CURRENCY_INDEX:
                    hit.currency = _CURRENCY_INDEX[tok]
                    break

        if any(tok in _UNIT_SET for tok in after[:1]) or (
                after and after[0] in _UNIT_SET):
            hit.is_unit = True
        # A unit may also be glued: "256gb", "75inch"
        glued = re.match(r"[a-z֐-׿]+", norm[m.end():])
        if glued and hebrew.normalize_token(glued.group(0)) in _UNIT_SET:
            hit.is_unit = True

        # Multi-word cues are checked first and win over single tokens.
        # "לא יותר מ 7000" ends in "מ", which on its own is the Hebrew "from"
        # and reads as a price FLOOR — the exact opposite of what the phrase
        # means. Scanning the phrase before the individual tokens prevents a
        # stated ceiling from being recorded as a floor.
        before_joined = " ".join(before)
        for phrase in _MAX_CUE_PHRASES:
            if phrase in before_joined:
                hit.cue = "max"
                break
        if hit.cue is None:
            for phrase in _MIN_CUE_PHRASES:
                if phrase in before_joined:
                    hit.cue = "min"
                    break

        if hit.cue is None:
            for cue in reversed(before):
                if cue in _MAX_CUE_SET:
                    hit.cue = "max"
                    break
                if cue in _MIN_CUE_SET:
                    hit.cue = "min"
                    break
        # "עד" may sit two tokens back: "עד כ 3000"
        if hit.cue is None and len(before) >= 2 and before[-2] in _MAX_CUE_SET:
            hit.cue = "max"

        hits.append(hit)
    return hits


def _score_price_hit(hit: _NumberHit) -> float:
    """
    Confidence that this number is a spending limit rather than a spec.

    A unit marker is disqualifying, not merely negative: no amount of other
    evidence should turn "256 GB" into a budget.
    """
    if hit.is_unit:
        return 0.0
    score = 0.0
    if hit.cue == "max":
        score += 0.55
    elif hit.cue == "min":
        score += 0.15
    if hit.currency:
        score += 0.40
    # Prices in consumer retail are rarely below ~50 or above ~500,000.
    if 50 <= hit.value <= 500_000:
        score += 0.15
    elif hit.value < 50:
        score -= 0.35
    # Round-ish numbers look like budgets ("3000", "1,500").
    if hit.value >= 100 and hit.value % 50 == 0:
        score += 0.10
    # Small bare integers are almost always model numbers or specs
    # ("iPhone 15", "75 inch"), not money.
    if not hit.cue and not hit.currency and hit.value < 1000:
        score -= 0.45
    return max(0.0, min(1.0, score))


def extract_prices(text: str) -> tuple[float | None, float | None, str | None, float]:
    """
    Return (max_price, min_price, currency, confidence).

    A number becomes a budget only if its confidence clears the acceptance
    threshold. When nothing clears it, the budget is None — which is correct
    and strictly better than the legacy behaviour of inventing one from a
    storage size.
    """
    hits = _scan_numbers(text)
    for h in hits:
        h.confidence = _score_price_hit(h)

    priced = [h for h in hits if h.confidence >= 0.45]
    if not priced:
        return None, None, None, 0.0

    maxes = [h for h in priced if h.cue != "min"]
    mins = [h for h in priced if h.cue == "min"]

    max_price = max((h.value for h in maxes), default=None)
    min_price = min((h.value for h in mins), default=None)

    # "בין 2000 ל 4000" — two max-ish numbers with no min cue form a range.
    if min_price is None and len(maxes) >= 2:
        values = sorted(h.value for h in maxes)
        if values[0] * 2 <= values[-1]:
            min_price, max_price = values[0], values[-1]

    if max_price is not None and min_price is not None and min_price > max_price:
        min_price, max_price = max_price, min_price

    currency = next((h.currency for h in priced if h.currency), None)
    confidence = max(h.confidence for h in priced)
    return max_price, min_price, currency, round(confidence, 3)


# ---------------------------------------------------------------------------
# Category detection
# ---------------------------------------------------------------------------

def detect_category(text: str) -> tuple[str | None, float]:
    """
    Token- and phrase-based category detection with confidence.

    Phrases outweigh single tokens, and unambiguous tokens outweigh ambiguous
    ones, so "מחשב נייד" resolves to laptop while a bare "נייד" stays weak.
    """
    tokens = hebrew.tokenize(text)
    joined = " ".join(tokens)
    scores: dict[str, float] = {}

    for phrase, cat in PHRASE_INDEX:
        if phrase and phrase in joined:
            scores[cat] = scores.get(cat, 0.0) + 2.5

    for tok in tokens:
        cats = TOKEN_INDEX.get(tok)
        if not cats:
            continue
        weight = 1.0 / len(cats) if tok in AMBIGUOUS else 1.0
        for cat in cats:
            scores[cat] = scores.get(cat, 0.0) + weight

    if not scores:
        return None, 0.0

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    total = sum(scores.values()) or 1.0
    margin = (best_score - runner_up) / total
    confidence = min(1.0, 0.45 + 0.35 * margin + 0.20 * min(1.0, best_score / 2.5))
    return best, round(confidence, 3)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _match_labels(tokens: list[str], joined: str, index: dict[str, str]) -> list[str]:
    found: list[str] = []
    for key, label in index.items():
        if label in found:
            continue
        if " " in key:
            if key in joined:
                found.append(label)
        elif key in tokens:
            found.append(label)
    return found


def parse(text: str) -> Intent:
    """Parse free-form Hebrew or English into a structured Intent."""
    clean = " ".join(str(text or "").strip().split())
    tokens = hebrew.tokenize(clean)
    joined = " ".join(tokens)

    category, cat_conf = detect_category(clean)
    max_price, min_price, currency, price_conf = extract_prices(clean)

    prefs = _match_labels(tokens, joined, _PREF_INDEX)
    uses = _match_labels(tokens, joined, _USE_INDEX)
    urg = _match_labels(tokens, joined, _URGENCY_INDEX)

    brands = []
    for tok in tokens:
        b = _BRAND_INDEX.get(tok)
        if b and b not in brands:
            brands.append(b)

    specs: dict[str, float] = {}
    for h in _scan_numbers(clean):
        if not h.is_unit:
            continue
        after = h.after.split()
        unit = after[0] if after else ""
        if unit:
            specs.setdefault(unit, h.value)

    # Content keywords: drop cue words and pure numbers, keep the nouns that
    # describe the need. These feed the semantic matcher.
    stop = _MAX_CUE_SET | _MIN_CUE_SET | _UNIT_SET | set(_CURRENCY_INDEX)
    keywords = [t for t in tokens
                if t not in stop and not t.isdigit() and len(t) > 1][:24]

    return Intent(
        query=clean,
        category=category,
        category_confidence=cat_conf,
        max_price=max_price,
        min_price=min_price,
        currency=currency,
        price_confidence=price_conf,
        preferences=prefs,
        brands=brands,
        specs=specs,
        use_case=uses[0] if uses else None,
        urgency=urg[0] if urg else "normal",
        keywords=keywords,
        source="rules",
    )


def parse_intent(text: str) -> dict:
    """
    Drop-in replacement for `jet_app.parse_intent`.

    Returns a superset of the legacy dict, so every existing caller, test and
    stored analytics payload keeps working unchanged.
    """
    return parse(text).to_dict()
