"""
jet_intel.concepts — synonym and accessory awareness.

Pure stdlib. No dependencies.

Why this module exists
----------------------
The benchmark caught a failure the lexical scorer could not fix on its own.
For the query "מחשב נייד ללימודים", the candidate "לפטופ 15.6 אינץ לסטודנטים"
shares *zero* tokens with the query:

    query tokens : מחשב · ניד · לימודים
    candidate    : לפטופ · אינץ · סטודנטים

Every one of those is a synonym pair, and BM25 scored the match at zero while
ranking "מחשב נייח לגיימינג" (a desktop gaming PC — the wrong product) above
it, purely because it repeats the literal word "מחשב".

This is the exact failure mode an embedding model is usually brought in to
solve. Embeddings would mean a model download, a vector index and a large
dependency for a service that currently installs in seconds. A curated
concept lexicon buys most of the benefit for this domain at zero cost, and
unlike an embedding it is inspectable and correctable: when a synonym is
missing, someone adds one line.

Second function: accessory suppression.
"תיק למחשב נייד" (a laptop bag) is lexically an excellent match for "מחשב
נייד" — it contains the whole phrase. It is also the wrong product, and
returning it wastes the customer's attention. Accessory markers are detected
and penalized, which no amount of lexical similarity would have achieved.
"""
from __future__ import annotations

from . import hebrew

# ---------------------------------------------------------------------------
# Concept groups
# ---------------------------------------------------------------------------
# Every term in a group is treated as the same concept. Groups deliberately
# mix Hebrew, English and Hebrew transliterations of English, because Israeli
# product listings mix all three freely within a single feed.

_GROUPS: dict[str, list[str]] = {
    # --- devices ----------------------------------------------------------
    "c_laptop": ["מחשב נייד", "לפטופ", "laptop", "notebook", "מקבוק", "macbook",
                 "אולטרabook", "ultrabook", "chromebook", "מחברת"],
    "c_desktop": ["מחשב נייח", "מחשב שולחני", "desktop", "tower", "נייח"],
    "c_phone": ["טלפון", "סמארטפון", "סמרטפון", "פלאפון", "smartphone", "phone",
                "mobile", "אייפון", "iphone", "גלקסי", "galaxy", "סלולרי"],
    "c_tv": ["טלוויזיה", "טלויזיה", "טלביזיה", "television", "tv", "מסך", "צג", "screen"],
    "c_tablet": ["טאבלט", "tablet", "אייפד", "ipad"],
    "c_watch": ["שעון", "watch", "smartwatch", "שעון חכם"],
    "c_headphones": ["אוזניות", "אוזנייה", "headphone", "headphones", "earbuds",
                     "earphones", "airpods", "אירפודס", "אוזניות אלחוטיות"],
    "c_speaker": ["רמקול", "speaker", "סאונדבר", "soundbar"],
    "c_camera": ["מצלמה", "camera", "dslr", "מצלמת"],

    # --- home appliances --------------------------------------------------
    "c_vacuum": ["שואב אבק", "שואב", "vacuum", "hoover", "שואב רובוטי", "robot vacuum"],
    "c_fridge": ["מקרר", "fridge", "refrigerator", "פריזר", "freezer"],
    "c_washer": ["מכונת כביסה", "washing machine", "washer", "מייבש", "dryer"],
    "c_oven": ["תנור", "oven", "מיקרוגל", "microwave", "כיריים"],
    "c_ac": ["מזגן", "air conditioner", "ac", "מיזוג"],
    "c_kettle": ["קומקום", "kettle"],
    "c_dishwasher": ["מדיח", "dishwasher", "מדיח כלים"],

    # --- coffee -----------------------------------------------------------
    "c_coffee_machine": ["מכונת קפה", "מכונת אספרסו", "coffee machine", "coffee maker",
                         "espresso machine", "אספרסו", "espresso", "נספרסו", "nespresso"],
    "c_grinder": ["מטחנה", "מטחנת קפה", "grinder", "coffee grinder"],

    # --- fashion ----------------------------------------------------------
    "c_shoes": ["נעל", "נעליים", "נעלי", "shoe", "shoes", "sneakers", "סניקרס",
                "נעלי ספורט", "running shoes", "trainers"],
    "c_jacket": ["מעיל", "ג'קט", "jacket", "coat", "חליפה"],
    "c_bag": ["תיק", "bag", "backpack", "תרמיל"],

    # --- use / audience ---------------------------------------------------
    "c_study": ["לימודים", "לימוד", "סטודנט", "סטודנטים", "סטודנטית", "אוניברסיטה",
                "מכללה", "בית ספר", "תלמיד", "study", "student", "students",
                "school", "college", "university", "academic"],
    "c_work": ["עבודה", "משרד", "עסקי", "מקצועי", "work", "office", "business",
               "professional", "enterprise"],
    "c_gaming": ["גיימינג", "גיימר", "משחקים", "gaming", "gamer", "games", "esports"],
    "c_creative": ["עיצוב", "עריכה", "גרפיקה", "וידאו", "צילום", "רנדור",
                   "design", "editing", "video", "photo", "graphics", "rendering",
                   "creative", "3d"],
    "c_family": ["משפחה", "משפחתי", "ילדים", "סלון", "בית",
                 "family", "kids", "children", "living room", "household"],

    # --- attributes -------------------------------------------------------
    "c_wireless": ["אלחוטי", "אלחוטית", "אלחוטיות", "בלוטות", "wireless",
                   "bluetooth", "cordless", "נטען"],
    "c_wired": ["חוטי", "כבל", "wired", "cable", "corded"],
    "c_noise_cancel": ["ביטול רעשים", "ביטול רעש", "מבטל רעשים", "noise cancelling",
                       "noise canceling", "anc", "noise cancellation"],
    "c_smart": ["חכם", "חכמה", "smart", "אנדרואיד", "android", "webos", "tizen"],
    "c_compact": ["קטן", "קטנה", "קומפקטי", "קל", "דק", "נייד",
                  "compact", "small", "light", "lightweight", "slim", "portable"],
    "c_large": ["גדול", "גדולה", "ענק", "large", "big", "huge", "oversized"],
    "c_quiet": ["שקט", "שקטה", "מרעיש", "רעש", "quiet", "silent", "noisy", "noise"],
    "c_premium": ["איכותי", "איכותית", "פרימיום", "מקצועי", "יוקרתי",
                  "premium", "pro", "professional", "flagship", "high end"],
    "c_budget": ["זול", "זולה", "חסכוני", "משתלם", "בסיסי", "בסיסית",
                 "cheap", "budget", "basic", "entry", "affordable", "value"],
    "c_4k": ["4k", "uhd", "אולטרה", "ultra hd", "8k"],
    "c_oled": ["oled", "qled", "אולד", "קיולד", "mini led", "neo qled"],
}


def _build() -> dict[str, set[str]]:
    """Map every canonical token/phrase form to the concepts it belongs to."""
    index: dict[str, set[str]] = {}
    for concept, terms in _GROUPS.items():
        for term in terms:
            key = " ".join(hebrew.tokenize(term))
            if not key:
                continue
            index.setdefault(key, set()).add(concept)
            # Single words are also indexed under their prefix-stripped stem so
            # "ללימודים" and "לימודים" both resolve.
            if " " not in key:
                for v in hebrew.token_variants(term):
                    index.setdefault(v, set()).add(concept)
    return index


CONCEPT_INDEX = _build()

# Longest phrases first so "מחשב נייד" wins over the bare "נייד".
_PHRASE_KEYS = sorted((k for k in CONCEPT_INDEX if " " in k),
                      key=lambda k: -len(k.split()))


def concepts_of(text: str) -> set[str]:
    """Every concept a piece of text expresses."""
    tokens = hebrew.tokenize(text)
    joined = " ".join(tokens)
    found: set[str] = set()
    for phrase in _PHRASE_KEYS:
        if phrase in joined:
            found |= CONCEPT_INDEX[phrase]
    for tok in tokens:
        if tok in CONCEPT_INDEX:
            found |= CONCEPT_INDEX[tok]
    return found


def overlap(query_text: str, candidate_text: str) -> float:
    """
    Jaccard-style concept agreement in 0..1.

    Weighted toward the query: a candidate that expresses every concept the
    customer asked for scores highly even if it also mentions others, which is
    the normal shape of a real product title.
    """
    q = concepts_of(query_text)
    if not q:
        return 0.5
    c = concepts_of(candidate_text)
    if not c:
        return 0.0
    shared = q & c
    coverage = len(shared) / len(q)
    precision = len(shared) / len(c) if c else 0.0
    return round(0.75 * coverage + 0.25 * precision, 4)


# ---------------------------------------------------------------------------
# Accessory and incompatibility detection
# ---------------------------------------------------------------------------

_ACCESSORY_MARKERS = [
    # Hebrew: these words mark the item as something FOR the product.
    "תיק ל", "כיסוי ל", "מגן ל", "מעמד ל", "מתקן ל", "כבל ל", "מטען ל",
    "סוללה ל", "שקיות ל", "פילטר ל", "מסנן ל", "אביזר", "אביזרים", "חלפים",
    "מדבקה ל", "סטנד ל", "זרוע ל", "קפסולות", "שקיות", "מארז ל", "נרתיק",
    # English
    "case for", "cover for", "stand for", "mount for", "cable for",
    "charger for", "bag for", "screen protector", "adapter for",
    "replacement", "accessory", "accessories", "filter for", "refill",
    "compatible with", "spare",
]
_ACCESSORY_KEYS = [" ".join(hebrew.tokenize(m)) for m in _ACCESSORY_MARKERS]
_ACCESSORY_KEYS = [k for k in _ACCESSORY_KEYS if k]

# Standalone accessory nouns: only an accessory when the customer did NOT ask
# for that thing itself. Someone searching "תיק למחשב" wants the bag.
_ACCESSORY_CONCEPTS = {"c_bag"}


def accessory_penalty(query_text: str, candidate_text: str) -> float:
    """
    0.0 when the candidate is the product asked for, up to 1.0 when it is
    clearly an accessory FOR that product rather than the product.
    """
    cand = " ".join(hebrew.tokenize(candidate_text))
    q_concepts = concepts_of(query_text)

    for marker in _ACCESSORY_KEYS:
        if marker in cand:
            # If the customer asked for the accessory itself, no penalty.
            marker_concepts = concepts_of(marker)
            if marker_concepts and marker_concepts & q_concepts:
                return 0.0
            return 1.0

    c_concepts = concepts_of(candidate_text)
    stray = (c_concepts & _ACCESSORY_CONCEPTS) - q_concepts
    if stray:
        return 0.6
    return 0.0


def conflict_penalty(query_text: str, candidate_text: str) -> float:
    """
    Penalize candidates that contradict an explicitly requested attribute.

    "wireless headphones" should not surface wired earbuds; "מחשב נייד"
    should not surface a desktop tower.
    """
    q = concepts_of(query_text)
    c = concepts_of(candidate_text)
    conflicts = [
        ("c_wireless", "c_wired"),
        ("c_laptop", "c_desktop"),
        ("c_compact", "c_large"),
        ("c_premium", "c_budget"),
    ]
    penalty = 0.0
    for a, b in conflicts:
        if a in q and b in c and a not in c:
            penalty += 0.5
        if b in q and a in c and b not in c:
            penalty += 0.5
    return min(1.0, penalty)
