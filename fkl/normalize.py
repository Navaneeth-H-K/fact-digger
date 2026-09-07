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


_SCALE_FACTORS: dict[str, float] = {
    "one": 1.0,
    "thousand": 1e3,
    "'000": 1e3,
    "k": 1e3,
    "lakh": 1e5,
    "lac": 1e5,
    "million": 1e6,
    "mn": 1e6,
    "crore": 1e7,
    "cr": 1e7,
    "lakh crore": 1e12,
    "billion": 1e9,
    "bn": 1e9,
    "trillion": 1e12,
    "tn": 1e12,
}

_EMBEDDED_SCALE_RE = re.compile(
    r"(?<![\w'])(lakh crore|thousand|'000|lakh|lac|million|mn|crore|cr|billion|bn|trillion|tn)(?![\w])",
    re.IGNORECASE,
)

# Units whose magnitude is never scaled by lakh/crore/million words.
_UNSCALED_UNITS = frozenset({"percent", "pp", "months", "years", "days", "ratio"})


def normalize_unit(unit_raw: str | None) -> str:
    """Map a printed unit ("per cent", "₹ Cr", "US$ billion", "x") to a canonical unit name.

    Numbers with no unit are counts. Scale words inside the unit are ignored here; see
    to_base_value for magnitude handling.
    """
    text = (unit_raw or "").strip().lower()
    if not text:
        return "count"
    if "percentage point" in text or text in {"pp", "ppt"}:
        return "pp"
    if "bps" in text or "basis point" in text:
        return "pp"
    if "%" in text or "per cent" in text or "percent" in text:
        return "percent"
    if "₹" in text or text.startswith("rs") or "inr" in text or "rupee" in text:
        return "INR"
    if "$" in text or "usd" in text or "dollar" in text:
        return "USD"
    if "sq" in text and ("ft" in text or "feet" in text or "foot" in text):
        return "sq_ft"
    if "month" in text:
        return "months"
    if "year" in text:
        return "years"
    if "day" in text:
        return "days"
    if text in {"x", "times"}:
        return "ratio"
    return "count"


def normalize_scale(scale_raw: str | None) -> float | None:
    """Map a scale word ("lakh", "Cr", "mn", "billion") to a multiplier; None if unknown."""
    if scale_raw is None:
        return 1.0
    return _SCALE_FACTORS.get(scale_raw.strip().lower().strip("."))


def _embedded_scale(*texts: str | None) -> float:
    for text in texts:
        if not text:
            continue
        match = _EMBEDDED_SCALE_RE.search(text)
        if match:
            return _SCALE_FACTORS[match.group(1).lower()]
    return 1.0


def to_base_value(value_raw: str, unit_raw: str | None, scale_raw: str | None) -> float | None:
    """Convert a printed value to its base magnitude (rupees, dollars, square feet, counts).

    The scale comes from the explicit scale field first, else from a scale word printed in the
    unit or the value itself ("₹ Cr", "US$ 45.6 bn"). Percentages, ratios and durations are never
    scaled; basis points become percentage points.
    """
    number = parse_number(value_raw)
    if number is None:
        return None
    unit = normalize_unit(unit_raw)
    if unit == "pp" and unit_raw and ("bps" in unit_raw.lower() or "basis" in unit_raw.lower()):
        return number / 100
    if unit in _UNSCALED_UNITS:
        return number
    explicit = normalize_scale(scale_raw) if scale_raw else None
    factor = explicit if explicit is not None else _embedded_scale(unit_raw, value_raw)
    return number * factor
