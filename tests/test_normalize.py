"""Number, unit, scale, period and key normalisation. Fixtures come from the starter PDFs."""

import pytest

from fkl.normalize import parse_number, parse_range


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5.4", 5.4),
        ("3,25,540", 325540.0),  # Indian digit grouping (RBI Appendix Table 8)
        ("1,234,567.89", 1234567.89),  # Western grouping
        ("(-) 2.5", -2.5),  # RBI negative convention
        ("(2.5)", -2.5),  # accounting negative
        ("(2,491.86)", -2491.86),  # Delhivery loss for the year
        ("-2.5", -2.5),
        ("−2.5", -2.5),  # Unicode minus
        ("5.4%", 5.4),
        ("₹ 1,23,456", 123456.0),  # rupee glyph present
        ("US$ 45.6 bn", 45.6),
        ("USD 640.3 billion", 640.3),
        ("12.3x", 12.3),
        ("81,415.38", 81415.38),
        ("2030.59", 2030.59),  # glued footnote marker is NOT repaired here; verification catches it
        ("6.4 per cent", 6.4),
        ("+0.1%", 0.1),
        ("J81,415.38", 81415.38),  # rupee glyph rendered as a stray letter (Delhivery AR)
    ],
)
def test_parse_number_handles_printed_conventions(raw: str, expected: float) -> None:
    assert parse_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["n.a.", "NA", "...", "-", "", "Nil", "abc", "FY24"])
def test_parse_number_returns_none_for_placeholders_and_text(raw: str) -> None:
    assert parse_number(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("6.3-6.8", (6.3, 6.8)),
        ("6.3 - 6.8", (6.3, 6.8)),
        ("6.3 to 6.8", (6.3, 6.8)),
        ("between 6.3 and 6.8 per cent", (6.3, 6.8)),
        ("6.3–6.8", (6.3, 6.8)),  # en dash
    ],
)
def test_parse_range_reads_low_and_high(raw: str, expected: tuple[float, float]) -> None:
    assert parse_range(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["5.4", "2023-24", "n.a.", "-2.5"])
def test_parse_range_returns_none_when_not_a_range(raw: str) -> None:
    assert parse_range(raw) is None
