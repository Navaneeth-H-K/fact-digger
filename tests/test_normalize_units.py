"""Unit and scale normalisation: rupee/dollar spellings, percent variants, lakh/crore/million ladders."""

import pytest

from fkl.normalize import normalize_scale, normalize_unit, to_base_value


@pytest.mark.parametrize(
    ("unit_raw", "expected"),
    [
        ("%", "percent"),
        ("per cent", "percent"),
        ("percent", "percent"),
        ("Per cent", "percent"),
        ("% of GDP", "percent"),
        ("per cent of GDP", "percent"),
        ("percentage points", "pp"),
        ("pp", "pp"),
        ("ppt", "pp"),
        ("bps", "pp"),
        ("basis points", "pp"),
        ("₹", "INR"),
        ("Rs", "INR"),
        ("Rs.", "INR"),
        ("INR", "INR"),
        ("rupees", "INR"),
        ("₹ million", "INR"),
        ("₹ Cr", "INR"),
        ("US$", "USD"),
        ("USD", "USD"),
        ("$", "USD"),
        ("US$ billion", "USD"),
        ("months", "months"),
        ("months of imports", "months"),
        ("years", "years"),
        ("days", "days"),
        ("x", "ratio"),
        ("times", "ratio"),
        ("sq ft", "sq_ft"),
        ("million square feet", "sq_ft"),
        ("million sq. ft.", "sq_ft"),
        ("shipments", "count"),
        (None, "count"),
        ("", "count"),
    ],
)
def test_normalize_unit_maps_printed_spellings_to_canonical(
    unit_raw: str | None, expected: str
) -> None:
    assert normalize_unit(unit_raw) == expected


@pytest.mark.parametrize(
    ("scale_raw", "expected"),
    [
        (None, 1.0),
        ("one", 1.0),
        ("thousand", 1e3),
        ("'000", 1e3),
        ("K", 1e3),
        ("lakh", 1e5),
        ("lac", 1e5),
        ("million", 1e6),
        ("mn", 1e6),
        ("Mn", 1e6),
        ("crore", 1e7),
        ("Cr", 1e7),
        ("lakh crore", 1e12),
        ("billion", 1e9),
        ("bn", 1e9),
        ("trillion", 1e12),
    ],
)
def test_normalize_scale_maps_scale_words_to_factors(
    scale_raw: str | None, expected: float
) -> None:
    assert normalize_scale(scale_raw) == pytest.approx(expected)


def test_normalize_scale_rejects_unknown_words() -> None:
    assert normalize_scale("dozen") is None


@pytest.mark.parametrize(
    ("value_raw", "unit_raw", "scale_raw", "expected"),
    [
        ("81,415.38", "₹ million", None, 81_415_380_000.0),  # Delhivery AR: ₹ million
        ("8,142", "₹ Cr", None, 81_420_000_000.0),  # Delhivery deck: ₹ crore, same fact
        ("757.86", "₹", "million", 757_860_000.0),
        ("76", "₹", "Cr", 760_000_000.0),
        ("0.10", "₹", "million", 100_000.0),  # sitting fee ₹0.10 million
        ("1", "₹", "lakh", 100_000.0),  # sitting fee ₹1 lakh — same fact
        ("668.3", "US$ billion", None, 668_300_000_000.0),
        ("45.6", "US$", "bn", 45_600_000_000.0),
        ("5.4", "per cent", None, 5.4),  # percent ignores scale
        ("5.4", "%", "million", 5.4),
        ("73", "bps", None, 0.73),  # basis points → percentage points
        ("10.9", "months", None, 10.9),
        ("18.82", "million sq ft", None, 18_820_000.0),
        ("3,25,540", "US$ million", None, 325_540_000_000.0),  # Indian grouping + header unit
    ],
)
def test_to_base_value_applies_scale_from_value_unit_or_scale_field(
    value_raw: str, unit_raw: str | None, scale_raw: str | None, expected: float
) -> None:
    assert to_base_value(value_raw, unit_raw, scale_raw) == pytest.approx(expected, rel=1e-9)


def test_to_base_value_returns_none_for_placeholders() -> None:
    assert to_base_value("n.a.", "₹ million", None) is None
