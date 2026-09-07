"""Canonical entity/attribute keys so differently worded facts can be paired."""

import pytest

from fkl.normalize import canonical_key


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Consumer Price Index (CPI) inflation", "CPI Inflation"),
        ("Gross Domestic Product growth", "GDP growth"),
        ("Real GDP growth", "growth of real GDP"),
        ("India", "INDIA"),
        ("Revenue from operations", "revenue from operations (consolidated)"),
        ("Net FDI inflows", "net foreign direct investment inflows"),
        ("Gateways", "gateway"),
        ("Y-o-Y growth in exports", "yoy growth in exports"),
        (
            "Adjusted EBITDA",
            "adjusted earnings before interest, tax, depreciation and amortisation",
        ),
        ("Current account deficit", "CAD"),
    ],
)
def test_canonical_key_matches_equivalent_wordings(left: str, right: str) -> None:
    assert canonical_key(left) == canonical_key(right)


def test_canonical_key_is_sorted_unique_tokens() -> None:
    assert canonical_key("growth of real GDP") == "gdp growth real"


def test_canonical_key_keeps_distinct_concepts_apart() -> None:
    assert canonical_key("headline inflation") != canonical_key("core inflation")
    assert canonical_key("net debt") != canonical_key("gross debt")


def test_canonical_key_of_empty_is_empty() -> None:
    assert canonical_key("") == ""
    assert canonical_key(None) == ""
