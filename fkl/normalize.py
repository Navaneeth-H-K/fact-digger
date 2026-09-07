"""Deterministic normalisation of values as they are printed in financial and statistical PDFs.

Everything here is a pure function of its inputs. The LLM copies values verbatim; this module
turns them into comparable numbers, units, scales, periods and keys.
"""

from __future__ import annotations

import re

# A printed number: Western grouping (1,234,567.89), Indian grouping (3,25,540), or plain.
_NUMBER = r"(?:\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"

# Things legitimately printed before a number: currency symbols and words, or a single stray
# letter glued to the digits (the rupee glyph is often rendered as I, J or K by PDF text layers).
_PREFIX = r"(?:[₹$€£]|rs\.?|inr|usd|us\$|eur|gbp|[A-Za-z](?=\d))?"

_NUMBER_RE = re.compile(
    rf"^\s*(?P<open>\(\s*-\s*\)|\()?\s*(?P<sign>[-+])?\s*{_PREFIX}\s*(?P<sign2>[-+])?\s*"
    rf"(?P<num>{_NUMBER})\s*(?P<suffix>[^\d]*)$",
    re.IGNORECASE,
)

_RANGE_RE = re.compile(
    rf"^\s*(?:between\s+)?(?P<low>{_NUMBER})\s*(?:-|–|—|to|and)\s*(?P<high>{_NUMBER})\s*[^\d]*$",
    re.IGNORECASE,
)


def _to_float(printed: str) -> float:
    return float(printed.replace(",", ""))


def parse_number(raw: str | None) -> float | None:
    """Parse one printed number; None for placeholders ("n.a.", "...", "-") and non-numbers.

    Handles "(2.5)" and "(-) 2.5" negatives, Unicode minus, Indian digit grouping, currency
    prefixes, and any digit-free suffix such as "%", "per cent", "x" or "bn".
    """
    if raw is None:
        return None
    text = raw.replace("−", "-").strip()
    match = _NUMBER_RE.match(text)
    if match is None:
        return None
    opener, suffix = match.group("open"), match.group("suffix")
    if opener == "(" and not suffix.startswith(")"):
        return None
    value = _to_float(match.group("num"))
    negative = opener is not None or "-" in (match.group("sign"), match.group("sign2"))
    return -value if negative else value


def parse_range(raw: str | None) -> tuple[float, float] | None:
    """Parse a printed range like "6.3-6.8" or "between 6.3 and 6.8 per cent" as (low, high)."""
    if raw is None:
        return None
    match = _RANGE_RE.match(raw.replace("−", "-").strip())
    if match is None:
        return None
    low, high = _to_float(match.group("low")), _to_float(match.group("high"))
    if low >= high:
        return None
    return low, high
