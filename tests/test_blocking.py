"""Candidate-pair blocking: which facts are even worth comparing, without hard-coded names."""

from datetime import date

from fkl.compare import FactView, build_candidates

FY24 = "2023-04-01:2024-03-31"


def fact(id: int, doc: str, entity: str, attribute: str, unit: str = "percent") -> FactView:
    return FactView(
        id=id,
        document_id=doc,
        entity_key=entity,
        attribute_key=attribute,
        value_num=1.0,
        unit=unit,
        period_key=FY24,
        estimate_type="actual",
        basis_key="",
        attributed_to=None,
        evidence_verified=True,
        publication_date=date(2025, 1, 1),
    )


def pair_ids(facts: list[FactView]) -> set[tuple[int, int]]:
    return {(a.id, b.id) for a, b in build_candidates(facts)}


def test_pairs_only_across_documents_with_matching_entity_attribute_and_unit() -> None:
    facts = [
        fact(1, "a", "india", "cpi headline inflation"),
        fact(2, "b", "india", "cpi headline inflation"),  # identical key
        fact(3, "b", "india", "core inflation"),  # different concept
        fact(4, "c", "delhivery", "count gateway", unit="count"),  # different entity
        fact(5, "a", "india", "cpi headline inflation"),  # same doc as 1
        fact(6, "c", "india", "cpi headline inflation", unit="INR"),  # unit mismatch
    ]
    assert pair_ids(facts) == {(1, 2), (2, 5)}


def test_entity_matches_when_one_key_is_a_token_subset_of_the_other() -> None:
    facts = [
        fact(1, "a", "india", "gdp growth real"),
        fact(2, "b", "economy india", "gdp growth real"),
    ]
    assert pair_ids(facts) == {(1, 2)}


def test_attribute_matches_on_token_overlap_not_exact_string() -> None:
    facts = [
        fact(1, "a", "delhivery", "operation revenue", unit="INR"),
        fact(2, "b", "delhivery", "consolidated operation revenue", unit="INR"),
    ]
    assert pair_ids(facts) == {(1, 2)}


def test_partner_count_is_capped_per_fact() -> None:
    facts = [fact(0, "a", "india", "gdp growth")]
    facts += [fact(i, "b", "india", "gdp growth") for i in range(1, 61)]
    partners = [b for a, b in build_candidates(facts, max_partners=50) if a.id == 0]
    assert len(partners) == 50


def test_facts_without_a_numeric_value_are_skipped() -> None:
    textual = FactView(
        id=1,
        document_id="a",
        entity_key="india",
        attribute_key="gdp growth",
        value_num=None,
        unit="percent",
        period_key=FY24,
        estimate_type="actual",
        basis_key="",
        attributed_to=None,
        evidence_verified=True,
        publication_date=None,
    )
    assert build_candidates([textual, fact(2, "b", "india", "gdp growth")]) == []
