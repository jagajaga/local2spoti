from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

_FEAT_RE = re.compile(
    r"\s*[\(\[]?\s*\b(?:feat\.?|ft\.?|featuring)\s+[^\)\]]+[\)\]]?",
    re.IGNORECASE,
)


def _nfc_lower(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip().lower()


def normalize_artist(s: str) -> str:
    if not s:
        return ""
    return _nfc_lower(_FEAT_RE.sub("", s))


def normalize_title(s: str) -> str:
    if not s:
        return ""
    return _nfc_lower(_FEAT_RE.sub("", s))


def similarity(a: str, b: str) -> float:
    return fuzz.WRatio(_nfc_lower(a), _nfc_lower(b)) / 100.0


def alpha_bucket(s: str) -> str:
    s = _nfc_lower(s).lstrip()
    if not s:
        return "#"
    first = s[0]
    return first.upper() if first.isalpha() else "#"
