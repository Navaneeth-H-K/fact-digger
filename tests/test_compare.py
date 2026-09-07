"""Rules-first comparison of facts. Fixtures mirror real cases from the starter PDFs."""

from datetime import date

import pytest

from fkl.compare import FactView, classify_pair, validate_fact, values_equal

FY24 = "2023-04-01:2024-03-31"
FY25 = "2024-04-01:2025-03-31"
Q2_FY25 = "2024-07-01:2024-09-30"
APR_DEC_24 = "2024-04-01:2024-12-31"


def fact(**overrides: object) -> FactView:
    base: dict[str, object] = {
        "id": 1,
        "document_id": "doc-a",
        "entity_key": "india",
        "attribute_key": "cpi headline inflation",
        "value_num": 5.4,
        "unit": "percent",
        "period_key": FY24,
        "estimate_type": "actual",
        "basis_key": "",
        "attributed_to": None,
        "evidence_verified": True,
        "publication_date": date(2025, 1, 30),
    }
    base.update(overrides)
    return FactView(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("a", "b", "unit", "expected"),
    [
        (5.4, 5.4, "percent", True),
        (5.4, 5.45, "percent", True),  # one-decimal rounding
        (5.4, 5.5, "percent", False),
        (81_415_380_000.0, 81_420_000_000.0, "INR", True),  # rupee million vs crore rounding
        (14_270_000.0, 14_610_000.0, "sq_ft", False),  # 2.4% apart is a real gap
        (98_135.0, 98_135.0, "count", True),
        (9.0, 8.0, "count", False),
        (0.02, 0.025, "ratio", True),
        (0.02, 0.04, "ratio", False),
        (668.3e9, 668.0e9, "USD", True),  # "$668 billion" vs "US$ 668.3 billion"
    ],
)
def test_values_equal_uses_unit_aware_tolerance(
    a: float, b: float, unit: str, expected: bool
) -> None:
    assert values_equal(a, b, unit) is expected


def test_same_period_same_value_corroborates_by_rule() -> None:
    result = classify_pair(
        fact(), fact(id=2, document_id="doc-b", publication_date=date(2025, 5, 29))
    )
    assert result is not None
    assert (result.verdict, result.status, result.method) == ("corroborates", "final", "rule")


def test_crore_and_million_values_already_in_base_units_corroborate() -> None:
    a = fact(attribute_key="operation revenue", value_num=81_415_380_000.0, unit="INR")
    b = fact(
        id=2,
        document_id="doc-b",
        attribute_key="operation revenue",
        value_num=81_420_000_000.0,
        unit="INR",
    )
    result = classify_pair(a, b)
    assert result is not None and result.verdict == "corroborates"


def test_later_revision_of_same_period_is_superseded_with_newer_winner() -> None:
    first = fact(
        attribute_key="gdp growth real",
        value_num=6.4,
        period_key=FY25,
        estimate_type="advance_estimate",
    )
    second = fact(
        id=2,
        document_id="doc-b",
        attribute_key="gdp growth real",
        value_num=6.5,
        period_key=FY25,
        estimate_type="revised",
        publication_date=date(2025, 5, 29),
    )
    result = classify_pair(first, second)
    assert result is not None
    assert (result.verdict, result.status, result.dimension, result.winner_id) == (
        "superseded",
        "final",
        "vintage",
        2,
    )


def test_different_periods_are_explained_by_context_without_llm() -> None:
    quarter = fact(attribute_key="cad", value_num=1.2, period_key=Q2_FY25)
    nine_months = fact(
        id=2, document_id="doc-b", attribute_key="cad", value_num=1.3, period_key=APR_DEC_24
    )
    result = classify_pair(quarter, nine_months)
    assert result is not None
    assert (result.verdict, result.status, result.dimension) == (
        "context_explained",
        "final",
        "period",
    )


def test_projection_versus_actual_is_explained_by_estimate_type() -> None:
    projection = fact(value_num=4.2, period_key=FY25, estimate_type="projection")
    actual = fact(id=2, document_id="doc-b", value_num=4.6, period_key=FY25, estimate_type="actual")
    result = classify_pair(projection, actual)
    assert result is not None
    assert (result.verdict, result.dimension) == ("context_explained", "estimate_type")


def test_differing_basis_goes_to_llm_with_basis_hypothesis() -> None:
    rbi = fact(
        attribute_key="deficit fiscal general government",
        value_num=8.8,
        basis_key="budget document union",
    )
    imf = fact(
        id=2,
        document_id="doc-b",
        attribute_key="deficit fiscal general government",
        value_num=8.1,
        basis_key="gfsm staff",
    )
    result = classify_pair(rbi, imf)
    assert result is not None
    assert (result.status, result.dimension) == ("pending_llm", "basis")


def test_same_key_different_values_is_a_contradiction_hypothesis_for_the_llm() -> None:
    result = classify_pair(fact(value_num=8.8), fact(id=2, document_id="doc-b", value_num=8.1))
    assert result is not None
    assert (result.verdict, result.status, result.dimension) == (
        "contradicts",
        "pending_llm",
        "value",
    )


def test_unknown_period_defers_to_llm() -> None:
    result = classify_pair(fact(period_key="unknown"), fact(id=2, document_id="doc-b"))
    assert result is not None and result.status == "pending_llm" and result.dimension == "period"


def test_incompatible_units_produce_no_relation() -> None:
    assert classify_pair(fact(unit="percent"), fact(id=2, document_id="doc-b", unit="INR")) is None


def test_attributed_third_party_figure_equal_to_first_party_corroborates_on_attribution() -> None:
    reported = fact(estimate_type="third_party", attributed_to="IMF")
    own = fact(id=2, document_id="doc-b")
    result = classify_pair(reported, own)
    assert result is not None
    assert (result.verdict, result.dimension) == ("corroborates", "attribution")


def test_unverified_evidence_halves_confidence() -> None:
    verified = classify_pair(fact(), fact(id=2, document_id="doc-b"))
    unverified = classify_pair(fact(evidence_verified=False), fact(id=2, document_id="doc-b"))
    assert verified is not None and unverified is not None
    assert unverified.confidence == pytest.approx(verified.confidence / 2)


def test_validate_fact_flags_period_after_publication_for_actuals() -> None:
    flags = validate_fact(
        fact(period_key="2025-04-01:2025-10-31"),
        period_end=date(2025, 10, 31),
        publication_date=date(2025, 1, 30),
    )
    assert "period_after_publication" in flags


def test_validate_fact_allows_projections_beyond_publication() -> None:
    flags = validate_fact(
        fact(estimate_type="projection"),
        period_end=date(2026, 3, 31),
        publication_date=date(2025, 1, 30),
    )
    assert "period_after_publication" not in flags


def test_validate_fact_flags_implausible_percent_and_missing_scale() -> None:
    assert "percent_out_of_range" in validate_fact(
        fact(value_num=3230.0), period_end=None, publication_date=None
    )
    flags = validate_fact(
        fact(unit="INR", value_num=12_842_738.0),
        period_end=None,
        publication_date=None,
        scale_known=False,
    )
    assert "scale_missing_for_currency" in flags
