"""Rules-first comparison of facts across documents.

The rule engine settles every pair it can explain deterministically (same period and value ->
corroborates; different periods -> explained by period; a later revision of the same period ->
superseded) and hands only the genuinely ambiguous pairs to the LLM with a stated hypothesis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Verdict = Literal["corroborates", "contradicts", "superseded", "context_explained", "unresolved"]
Status = Literal["final", "pending_llm"]
Dimension = Literal[
    "period", "estimate_type", "basis", "unit", "vintage", "attribution", "value", "none"
]

# Estimate types that describe a plan or a forecast rather than a measurement of the period.
_FORWARD_LOOKING = frozenset({"budget", "projection", "target", "scenario"})
_MEASURED = frozenset({"actual", "provisional", "revised", "advance_estimate", "unknown"})
_ONE_DECIMAL_UNITS = frozenset({"percent", "pp", "months", "years", "days"})
_RULE_CONFIDENCE = 0.9
_PENDING_CONFIDENCE = 0.5


@dataclass(frozen=True)
class FactView:
    """The comparison-relevant slice of a stored fact. Built from ORM rows by the pipeline."""

    id: int
    document_id: str
    entity_key: str
    attribute_key: str
    value_num: float | None
    unit: str
    period_key: str
    estimate_type: str
    basis_key: str
    attributed_to: str | None
    evidence_verified: bool
    publication_date: date | None


@dataclass(frozen=True)
class RuleResult:
    verdict: Verdict
    status: Status
    dimension: Dimension
    hypothesis: str
    confidence: float
    winner_id: int | None = None
    method: str = "rule"


def values_equal(a: float, b: float, unit: str) -> bool:
    """Equality under the rounding each unit is normally printed with.

    Percentages and durations are printed to one decimal, so half a unit of the last digit is
    tolerated; monetary amounts and counts are compared relatively to absorb crore/million rounding.
    """
    if unit in _ONE_DECIMAL_UNITS:
        return abs(a - b) <= 0.051
    if unit == "ratio":
        return abs(a - b) <= 0.011
    largest = max(abs(a), abs(b))
    return abs(a - b) <= 0.005 * largest if largest else True


def _newer(a: FactView, b: FactView) -> FactView | None:
    if a.publication_date is None or b.publication_date is None:
        return None
    if a.publication_date == b.publication_date:
        return None
    return a if a.publication_date > b.publication_date else b


class _Pair:
    """Builds RuleResults for one candidate pair, applying the shared confidence policy."""

    def __init__(self, a: FactView, b: FactView) -> None:
        self.a, self.b = a, b
        self.both_verified = a.evidence_verified and b.evidence_verified

    def result(
        self,
        verdict: Verdict,
        status: Status,
        dimension: Dimension,
        hypothesis: str,
        winner_id: int | None = None,
    ) -> RuleResult:
        confidence = _RULE_CONFIDENCE if status == "final" else _PENDING_CONFIDENCE
        if not self.both_verified:
            confidence /= 2
        return RuleResult(verdict, status, dimension, hypothesis, confidence, winner_id)


def classify_pair(a: FactView, b: FactView) -> RuleResult | None:
    """Classify a candidate pair; None means the facts are not comparable at all."""
    if a.unit != b.unit or a.value_num is None or b.value_num is None:
        return None
    pair = _Pair(a, b)
    equal = values_equal(a.value_num, b.value_num, a.unit)

    if "unknown" in (a.period_key, b.period_key):
        return pair.result("unresolved", "pending_llm", "period", "period of a fact is unclear")
    if a.period_key != b.period_key:
        return pair.result("context_explained", "final", "period", "values cover different periods")

    if (a.attributed_to is None) != (b.attributed_to is None):
        if equal:
            return pair.result("corroborates", "final", "attribution", "third-party figure matches")
        return pair.result(
            "unresolved", "pending_llm", "attribution", "attributed figure differs from first-party"
        )

    if a.estimate_type != b.estimate_type:
        if equal:
            return pair.result(
                "corroborates", "final", "estimate_type", "same value despite different labels"
            )
        if a.estimate_type in _FORWARD_LOOKING or b.estimate_type in _FORWARD_LOOKING:
            return pair.result(
                "context_explained", "final", "estimate_type", "forecast or budget vs measurement"
            )
        newer = _newer(a, b)
        if newer is not None and a.estimate_type in _MEASURED and b.estimate_type in _MEASURED:
            return pair.result(
                "superseded", "final", "vintage", "later publication revises the period", newer.id
            )
        return pair.result(
            "superseded", "pending_llm", "vintage", "revision suspected; publication order unclear"
        )

    if a.basis_key and b.basis_key and a.basis_key != b.basis_key:
        verdict: Verdict = "corroborates" if equal else "unresolved"
        return pair.result(verdict, "pending_llm", "basis", "measurement bases differ")

    if equal:
        return pair.result("corroborates", "final", "none", "same period, basis and value")
    return pair.result(
        "contradicts", "pending_llm", "value", "same period and basis, values differ"
    )


def validate_fact(
    fact: FactView,
    *,
    period_end: date | None,
    publication_date: date | None,
    scale_known: bool = True,
) -> list[str]:
    """Cheap plausibility flags. They never block a fact; they are shown and lower confidence."""
    flags: list[str] = []
    if (
        period_end is not None
        and publication_date is not None
        and fact.estimate_type in _MEASURED
        and period_end > publication_date + timedelta(days=31)
    ):
        flags.append("period_after_publication")
    if fact.unit == "percent" and fact.value_num is not None and abs(fact.value_num) > 1000:
        flags.append("percent_out_of_range")
    if fact.unit in {"INR", "USD"} and not scale_known:
        flags.append("scale_missing_for_currency")
    return flags
