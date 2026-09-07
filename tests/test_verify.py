"""Evidence grounding: a quote the model returns must actually occur on the page."""

from fkl.verify import verify_quote

PAGE = """Table II.3.1
Retail headline inflation  has  softened from 5.4 per cent in FY24 to 4.9 per cent
in April – December 2024.
Revenue from contracts with customers   21    81,415.38   72,253.01
(in  million)
"""


def test_exact_substring_is_verified_with_full_score() -> None:
    result = verify_quote("softened from 5.4 per cent in FY24", PAGE)
    assert result.verified and result.score == 1.0 and result.method == "exact"


def test_whitespace_and_line_breaks_do_not_matter() -> None:
    result = verify_quote(
        "inflation has softened from 5.4 per cent in FY24 to 4.9 per cent in April", PAGE
    )
    assert result.verified and result.method == "normalized"


def test_missing_currency_glyph_in_text_layer_still_verifies() -> None:
    result = verify_quote("(in ₹ million)", PAGE)
    assert result.verified and result.method == "normalized"


def test_small_ocr_style_differences_verify_fuzzily_with_score() -> None:
    result = verify_quote("Revenue from contracts with customer 21 81,415.38 72,253.01", PAGE)
    assert result.verified and result.method == "fuzzy" and 0.85 <= result.score < 1.0


def test_unrelated_quote_is_not_verified() -> None:
    result = verify_quote("net FDI inflows were US$ 10.1 billion in FY24", PAGE)
    assert not result.verified and result.method == "not_found" and result.score < 0.85


def test_image_quote_absent_from_text_is_marked_image_only() -> None:
    result = verify_quote("Adjusted EBITDA 758", PAGE, quote_source="image")
    assert not result.verified and result.method == "image_only" and result.score == 0.0


def test_image_quote_present_in_text_is_verified_normally() -> None:
    result = verify_quote("81,415.38   72,253.01", PAGE, quote_source="image")
    assert result.verified and result.method == "exact"


def test_empty_quote_is_never_verified() -> None:
    assert not verify_quote("", PAGE).verified
    assert not verify_quote("   ", PAGE).verified
