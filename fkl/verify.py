"""Ground a model-provided quote in the page text it claims to come from.

The LLM copies quotes from a text layer that is often corrupted (missing currency glyphs, odd
spacing, split lines). Verification is tiered: exact substring, then whitespace/glyph-normalised
substring, then a fuzzy partial match with a score. Quotes read off the page image that never
appear in the text layer are reported as image_only so the UI can badge them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz

VerifyMethod = Literal["exact", "normalized", "fuzzy", "image_only", "not_found"]

FUZZY_THRESHOLD = 0.85
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class VerifyResult:
    verified: bool
    score: float
    method: VerifyMethod


def _normalized(text: str) -> str:
    return _NON_ALNUM_RE.sub("", text.lower())


def _normalized_spaced(text: str) -> str:
    return " ".join(_NON_ALNUM_RE.sub(" ", text.lower()).split())


def verify_quote(
    quote: str, page_text: str, quote_source: Literal["text", "image"] = "text"
) -> VerifyResult:
    """Check that `quote` occurs on the page; return how it was matched and how well."""
    quote = quote.strip()
    if not quote:
        return VerifyResult(False, 0.0, "not_found")
    if quote in page_text:
        return VerifyResult(True, 1.0, "exact")
    if _normalized(quote) in _normalized(page_text):
        return VerifyResult(True, 1.0, "normalized")
    score = fuzz.partial_ratio(_normalized_spaced(quote), _normalized_spaced(page_text)) / 100
    if score >= FUZZY_THRESHOLD:
        return VerifyResult(True, round(score, 4), "fuzzy")
    if quote_source == "image":
        return VerifyResult(False, 0.0, "image_only")
    return VerifyResult(False, round(score, 4), "not_found")
