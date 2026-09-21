"""
jet_intel.hebrew — Hebrew text normalization for retrieval and intent parsing.

Pure stdlib. No dependencies.

Why this exists
---------------
The v9 engine matched categories with raw substring tests against a short
keyword list. Measured consequences on real phrasings:

    "טלויזיה גדולה לסלון"  -> category None   (ktiv-haser spelling variant)
    "אייפון 15 פרו"        -> category None   (Hebrew brand transliteration)
    "cheap headphones"      -> category phone  ("phone" inside "headphones")

Hebrew makes substring matching unusually unreliable:

  * Ktiv male / ktiv haser: the same word is spelled with or without
    mater lectionis (טלוויזיה / טלויזיה / טלביזיה).
  * Inseparable prefixes: ו ה ב ל מ כ ש ד attach directly to the noun,
    so "בטלוויזיה" and "טלוויזיה" are the same word with no space.
  * Final-form letters: ך ם ן ף ץ are the same letters as כ מ נ פ צ.
  * Geresh / gershayim: ג'ב, ג"ב, ג׳יגה all denote gigabyte.
  * Niqqud and cantillation marks may appear in pasted text.

Normalizing all of this to one canonical surface form lets a single lexicon
entry match every spelling a real user types.
"""
from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Character classes
# ---------------------------------------------------------------------------

# Niqqud (vowel points), cantillation marks and the Hebrew punctuation marks
# that carry no lexical meaning for matching purposes.
_NIQQUD = re.compile(r"[֑-ֽֿ-ׇ]")

# Geresh / gershayim in all their Unicode and ASCII spellings.
_GERESH = re.compile(r"[׳״‘’“”'\"`]")

HEBREW_FINALS = {"ך": "כ", "ם": "מ", "ן": "נ", "ף": "פ", "ץ": "צ"}

# Single-letter inseparable prefixes. "ה" (the) is included; "ו" (and) too.
# These are stripped only when the remainder is still a plausible word, to
# avoid mangling words that legitimately begin with these letters
# (e.g. "מחשב" must not become "חשב", "בית" must not become "ית").
_PREFIXES = ("ומה", "שה", "כש", "לכש", "מה", "וה", "ול", "וב", "ומ", "ו", "ה", "ב", "ל", "מ", "כ", "ש", "ד")

HEBREW_RE = re.compile(r"[֐-׿]")

# Tokens whose remainder after stripping would be shorter than this are left
# alone. Set to 4 rather than 3 after observing "מחשב" (computer) being
# reduced to "חשב" (accountant/calculated): a 4-letter noun beginning with a
# prefix letter is far more often a whole word than a prefixed 3-letter stem.
_MIN_STEM = 4

# Common words that begin with a prefix letter and must never be stripped,
# whatever their length. Cheaper and more reliable than a morphological
# analyzer for the handful of words that actually matter in this domain.
_PROTECTED_WORDS = (
    "מחשב", "מחשבים", "מקרר", "מזגן", "מסך", "מטען", "מכונה", "מכונת",
    "בית", "בגד", "בגדים", "ביתי", "ברזל", "המלצה", "לימוד", "לימודים",
    "כיריים", "כלים", "שעון", "שולחן", "דלת", "וילון", "מעיל", "מטבח",
    "משחק", "משחקים", "מצלמה", "מדיח", "מייבש", "משרד", "מתנה", "לפטופ",
    "שואב", "שקט", "דגם", "כבל", "מגן", "מדף", "מנורה", "מיטה",
)
# Populated below, AFTER normalize_token is defined. The list above is written
# in natural spelling, but strip_prefix() receives tokens that have already
# been normalized (final letters folded, matres collapsed), so the raw
# spellings would never match: "לימודים" arrives as "לימודימ". Storing the raw
# forms silently disabled protection for every word ending in a final letter.
_PROTECTED: set[str] = set()


def has_hebrew(text: str) -> bool:
    """True if the string contains at least one Hebrew letter."""
    return bool(HEBREW_RE.search(text or ""))


def strip_niqqud(text: str) -> str:
    """Remove vowel points and cantillation marks."""
    return _NIQQUD.sub("", unicodedata.normalize("NFC", text or ""))


def normalize_finals(text: str) -> str:
    """Map final-form letters to their medial forms."""
    return "".join(HEBREW_FINALS.get(ch, ch) for ch in text)


def collapse_matres(token: str) -> str:
    """
    Collapse doubled vav/yod to a single letter.

    This is the key to ktiv-male / ktiv-haser equivalence:

        טלוויזיה -> טלויזיה
        טלויזיה  -> טלויזיה   (already canonical)
        אייפון   -> איפון

    Both spellings converge, so one lexicon entry matches both.
    """
    token = re.sub(r"ו{2,}", "ו", token)
    token = re.sub(r"י{2,}", "י", token)
    return token


def strip_prefix(token: str) -> str:
    """
    Remove one inseparable prefix when doing so leaves a plausible stem.

    Conservative by design: a token is only stripped if it is not protected
    and the remainder is at least `_MIN_STEM` characters. This keeps "מחשב"
    (computer) intact rather than reducing it to "חשב".
    """
    if token in _PROTECTED:
        return token
    if len(token) < _MIN_STEM + 1:
        return token
    for p in _PREFIXES:
        if token.startswith(p) and len(token) - len(p) >= _MIN_STEM:
            return token[len(p):]
    return token


def normalize_token(token: str) -> str:
    """Full canonical form of a single token."""
    token = strip_niqqud(token).lower()
    token = _GERESH.sub("", token)
    token = normalize_finals(token)
    token = collapse_matres(token)
    return token


_PROTECTED.update(normalize_token(w) for w in _PROTECTED_WORDS)


def normalize(text: str) -> str:
    """
    Canonicalize a whole string: niqqud, gershayim, final letters, matres,
    and whitespace. Prefixes are NOT stripped here — that happens per token
    during tokenization, so the original word order and spacing survive.
    """
    text = strip_niqqud(text or "").lower()
    text = _GERESH.sub("", text)
    text = normalize_finals(text)
    text = re.sub(r"[׳״]", "", text)
    text = re.sub(r"\s+", " ", text)
    return collapse_matres(text).strip()


_TOKEN_RE = re.compile(r"[\w֐-׿]+", re.UNICODE)


def tokenize(text: str, *, stem: bool = True) -> list[str]:
    """
    Tokenize into canonical tokens.

    With stem=True each Hebrew token also has one inseparable prefix removed,
    so "בטלוויזיה" and "טלוויזיה" produce the same token.
    """
    norm = normalize(text)
    out: list[str] = []
    for raw in _TOKEN_RE.findall(norm):
        tok = normalize_token(raw)
        if not tok:
            continue
        if stem and has_hebrew(tok):
            tok = strip_prefix(tok)
        out.append(tok)
    return out


def token_variants(token: str) -> set[str]:
    """
    Every canonical form a token might be indexed under: itself, and its
    prefix-stripped stem. Used when building lexicons so that a lexicon
    written in plain Hebrew still matches prefixed user input.
    """
    t = normalize_token(token)
    variants = {t}
    if has_hebrew(t):
        variants.add(strip_prefix(t))
    return {v for v in variants if v}
