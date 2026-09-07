"""Deterministic normalisation of values as they are printed in financial and statistical PDFs.

Everything here is a pure function of its inputs. The LLM copies values verbatim; this module
turns them into comparable numbers, units, scales, periods and keys.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

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


# --------------------------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------------------------


_MONTHS = {
    name.lower(): number
    for number, names in enumerate(zip(calendar.month_name, calendar.month_abbr, strict=True))
    if number
    for name in names
}
_MONTHS["sept"] = 9
_MONTH_WORD = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_YEAR2 = r"(\d{2}|\d{4})"
_FY_TAIL = rf"{_YEAR2}(?:\s*[-/]\s*{_YEAR2})?"
_NUMBER_WORDS = {"three": 3, "six": 6, "nine": 9, "twelve": 12}


@dataclass(frozen=True)
class Period:
    """A closed date interval with the kind of period the document expressed."""

    start: date
    end: date
    kind: str  # fy | quarter | half | month | calendar | asof | range


def period_key(period: Period | None) -> str:
    return f"{period.start.isoformat()}:{period.end.isoformat()}" if period else "unknown"


def _end_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _full_year(printed: str) -> int:
    year = int(printed)
    return year + 2000 if year < 100 else year


def _fy_end_year(first: str, second: str | None) -> int:
    return _full_year(second) if second else _full_year(first)


def _fiscal_year(end_year: int, fy_start_month: int) -> Period:
    if fy_start_month == 1:
        return Period(date(end_year, 1, 1), date(end_year, 12, 31), "fy")
    start = date(end_year - 1, fy_start_month, 1)
    return Period(start, _add_months(start, 12) - timedelta(days=1), "fy")


def _fiscal_span(
    end_year: int, fy_start_month: int, first_month: int, months: int, kind: str
) -> Period:
    start = _add_months(_fiscal_year(end_year, fy_start_month).start, first_month)
    return Period(start, _add_months(start, months) - timedelta(days=1), kind)


def _parse_date(text: str) -> date | None:
    text = text.strip().rstrip(".")
    patterns = (
        (rf"^(\d{{1,2}})\s+({_MONTH_WORD})[,\s]+(\d{{4}})$", ("d", "m", "y")),
        (rf"^({_MONTH_WORD})\s+(\d{{1,2}})[,\s]+(\d{{4}})$", ("m", "d", "y")),
        (r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", ("d", "n", "y")),
        (r"^(\d{4})-(\d{2})-(\d{2})$", ("y", "n", "d")),
    )
    for pattern, order in patterns:
        match = re.match(pattern, text, re.IGNORECASE)
        if not match:
            continue
        parts = dict(zip(order, match.groups(), strict=True))
        month = _MONTHS[parts["m"].lower()] if "m" in parts else int(parts["n"])
        return date(int(parts["y"]), month, int(parts["d"]))
    month_year = re.match(rf"^({_MONTH_WORD})\s+(\d{{4}})$", text, re.IGNORECASE)
    if month_year:
        return _end_of_month(int(month_year.group(2)), _MONTHS[month_year.group(1).lower()])
    return None


def parse_period(text: str | None, fy_start_month: int = 4) -> Period | None:
    """Parse a printed period ("FY24", "Q3:2024-25", "April-December 2024", "as on 31 March 2025").

    Two-digit years are read as 20xx. Fiscal labels resolve with the document's fiscal-year start
    month, so "FY24" is April 2023–March 2024 for Indian issuers and calendar 2024 when the year
    starts in January.
    """
    if not text:
        return None
    t = (
        re.sub(r"\s+", " ", text.replace("–", "-").replace("—", "-").replace(":", " "))
        .strip()
        .lower()
    )

    if m := re.match(r"^(?:as (?:on|at|of)|at|end[- ]?(?:of )?)\s*(.+)$", t):
        day = _parse_date(m.group(1))
        return Period(day, day, "asof") if day else None
    if m := re.match(r"^(\w+) months? (?:period )?(?:ended|ending|to) (.+)$", t):
        count = _NUMBER_WORDS.get(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else None)
        end = _parse_date(m.group(2))
        if count and end:
            return Period(_add_months(end + timedelta(days=1), -count), end, "range")
        return None
    if m := re.match(r"^(?:financial year|fiscal year|year) (?:ended|ending) (.+)$", t):
        end = _parse_date(m.group(1))
        return Period(_add_months(end + timedelta(days=1), -12), end, "fy") if end else None
    if m := re.match(rf"^q([1-4]) ?(?:of )?(?:fy ?)?{_FY_TAIL}$", t):
        quarter = int(m.group(1))
        end_year = _fy_end_year(m.group(2), m.group(3))
        return _fiscal_span(end_year, fy_start_month, 3 * (quarter - 1), 3, "quarter")
    if m := re.match(r"^(\d{4}) ?q([1-4])$", t):
        quarter, year = int(m.group(2)), int(m.group(1))
        return _fiscal_span(year, 1, 3 * (quarter - 1), 3, "quarter")
    if m := re.match(rf"^h([12]) ?(?:of )?(?:fy ?)?{_FY_TAIL}$", t):
        half = int(m.group(1))
        return _fiscal_span(
            _fy_end_year(m.group(2), m.group(3)), fy_start_month, 6 * (half - 1), 6, "half"
        )
    if m := re.match(rf"^(\d{{1,2}})m ?(?:fy ?)?{_FY_TAIL}$", t):
        return _fiscal_span(
            _fy_end_year(m.group(2), m.group(3)), fy_start_month, 0, int(m.group(1)), "range"
        )
    if m := re.match(rf"^({_MONTH_WORD}) ?(?:-|to) ?({_MONTH_WORD}) (\d{{4}})$", t):
        first, last, year = _MONTHS[m.group(1)], _MONTHS[m.group(2)], int(m.group(3))
        start_year = year - 1 if last < first else year
        return Period(date(start_year, first, 1), _end_of_month(year, last), "range")
    if m := re.match(rf"^(?:fy|f\.y\.|fiscal|financial year) ?'?{_FY_TAIL}$", t):
        return _fiscal_year(_fy_end_year(m.group(1), m.group(2)), fy_start_month)
    if m := re.match(r"^(\d{4}) ?[-/] ?(\d{2}|\d{4})$", t):
        first, second = int(m.group(1)), _full_year(m.group(2))
        return _fiscal_year(second, fy_start_month) if second == first + 1 else None
    if m := re.match(r"^(?:cy ?)?(\d{4})$", t):
        year = int(m.group(1))
        return Period(date(year, 1, 1), date(year, 12, 31), "calendar")
    if m := re.match(rf"^({_MONTH_WORD}) (\d{{4}})$", t):
        month, year = _MONTHS[m.group(1)], int(m.group(2))
        return Period(date(year, month, 1), _end_of_month(year, month), "month")
    return None


# --------------------------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------------------------

# Generic finance/statistics vocabulary: expanded phrases collapse to their usual acronym so that
# "gross domestic product growth" and "GDP growth" share a key. Nothing here is document-specific.
_ACRONYMS: tuple[tuple[str, str], ...] = (
    ("gross domestic product", "gdp"),
    ("gross value added", "gva"),
    ("consumer price index", "cpi"),
    ("wholesale price index", "wpi"),
    ("current account deficit", "cad"),
    ("current account balance", "cab"),
    ("foreign direct investment", "fdi"),
    ("foreign portfolio investment", "fpi"),
    ("external commercial borrowing", "ecb"),
    ("non performing asset", "npa"),
    ("capital expenditure", "capex"),
    ("earnings per share", "eps"),
    ("profit after tax", "pat"),
    ("earnings before interest tax depreciation and amortisation", "ebitda"),
    ("earnings before interest tax depreciation and amortization", "ebitda"),
    ("year on year", "yoy"),
    ("y o y", "yoy"),
    ("quarter on quarter", "qoq"),
    ("q o q", "qoq"),
)
_STOPWORDS = frozenset(
    {"the", "of", "in", "for", "and", "a", "an", "to", "on", "at", "by", "from", "as", "per", "or"}
)
_BRACKETED_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _singular(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "is", "us")):
        return token[:-1]
    return token


def canonical_key(text: str | None) -> str:
    """Order-free, case-free, acronym-normalised key for an entity or attribute name."""
    if not text:
        return ""
    lowered = unicodedata.normalize("NFKC", text).lower()
    lowered = _BRACKETED_RE.sub(" ", lowered)
    lowered = _NON_ALNUM_RE.sub(" ", lowered).strip()
    for phrase, acronym in _ACRONYMS:
        lowered = re.sub(rf"\b{phrase}\b", acronym, lowered)
    tokens = {_singular(tok) for tok in lowered.split() if tok not in _STOPWORDS}
    return " ".join(sorted(tokens))
