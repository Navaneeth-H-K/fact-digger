"""Rules-first comparison of facts across documents.

The rule engine settles every pair it can explain deterministically (same period and value ->
corroborates; different periods -> explained by period; a later revision of the same period ->
superseded) and hands only the genuinely ambiguous pairs to the LLM with a stated hypothesis.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from rapidfuzz import fuzz

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


# --------------------------------------------------------------------------------------------
# Candidate blocking
# --------------------------------------------------------------------------------------------

_MIN_JACCARD = 0.5
_MIN_TOKEN_SET_RATIO = 85


def _tokens(key: str) -> frozenset[str]:
    return frozenset(key.split())


def _entities_match(a: FactView, b: FactView) -> bool:
    left, right = _tokens(a.entity_key), _tokens(b.entity_key)
    return left == right or left <= right or right <= left


def _attribute_score(a: FactView, b: FactView) -> float:
    """1.0 for identical keys, else Jaccard overlap, else a fuzzy ratio; 0.0 when not a match."""
    if a.attribute_key == b.attribute_key:
        return 1.0
    left, right = _tokens(a.attribute_key), _tokens(b.attribute_key)
    union = left | right
    jaccard = len(left & right) / len(union) if union else 0.0
    if jaccard >= _MIN_JACCARD:
        return jaccard
    ratio = fuzz.token_set_ratio(a.attribute_key, b.attribute_key)
    return ratio / 100 if ratio >= _MIN_TOKEN_SET_RATIO else 0.0


def build_candidates(
    facts: Sequence[FactView], max_partners: int = 50
) -> list[tuple[FactView, FactView]]:
    """Pairs worth classifying: different documents, same unit, matching entity and attribute.

    Attributes are matched through an inverted index over their tokens, using each fact's two
    rarest tokens as anchors, so the cost stays near-linear in the number of facts and no attribute
    name is ever hard-coded. Each fact keeps at most `max_partners` best-scoring partners.
    """
    eligible = [f for f in facts if f.value_num is not None and f.attribute_key]
    postings: dict[str, list[FactView]] = {}
    for f in eligible:
        for token in _tokens(f.attribute_key):
            postings.setdefault(token, []).append(f)

    seen: set[tuple[int, int]] = set()
    partner_count: dict[int, int] = {}
    pairs: list[tuple[FactView, FactView]] = []
    for f in eligible:
        anchors = sorted(_tokens(f.attribute_key), key=lambda t: len(postings[t]))[:2]
        candidates = {g.id: g for token in anchors for g in postings[token] if g.id != f.id}
        scored = (
            (score, g)
            for g in candidates.values()
            if g.document_id != f.document_id
            and g.unit == f.unit
            and _entities_match(f, g)
            and (score := _attribute_score(f, g)) > 0.0
        )
        for _, g in sorted(scored, key=lambda item: (-item[0], item[1].id)):
            key = (min(f.id, g.id), max(f.id, g.id))
            if key in seen:
                continue
            if partner_count.get(f.id, 0) >= max_partners:
                break
            if partner_count.get(g.id, 0) >= max_partners:
                continue
            seen.add(key)
            partner_count[f.id] = partner_count.get(f.id, 0) + 1
            partner_count[g.id] = partner_count.get(g.id, 0) + 1
            pairs.append((f, g) if f.id < g.id else (g, f))
    return sorted(pairs, key=lambda p: (p[0].id, p[1].id))


# --------------------------------------------------------------------------------------------
# Arithmetic tie-out: parts that sum to a total across documents
# --------------------------------------------------------------------------------------------

_ADDITIVE_UNITS = frozenset({"count", "INR", "USD", "sq_ft"})
_MAX_GROUP_SIZE = 40


@dataclass(frozen=True)
class ArithmeticEdge:
    parts: tuple[FactView, FactView]
    total: FactView
    explanation: str


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:g}"


def find_arithmetic_relations(facts: Sequence[FactView]) -> list[ArithmeticEdge]:
    """Find triples where two parts printed somewhere add up to a total printed elsewhere.

    Only absolute quantities take part (counts, money, area), never percentages or ratios where
    coincidental sums are common. Each triple must span at least two documents and use three
    distinct attributes, so it ties one document's breakdown to another's headline figure.
    """
    groups: dict[tuple[str, str, str], list[FactView]] = {}
    for f in facts:
        if f.unit in _ADDITIVE_UNITS and f.value_num is not None and f.value_num > 0:
            groups.setdefault((f.entity_key, f.period_key, f.unit), []).append(f)

    edges: list[ArithmeticEdge] = []
    for members in groups.values():
        if len(members) < 3 or len(members) > _MAX_GROUP_SIZE:
            continue
        ordered = sorted(members, key=lambda f: f.value_num or 0.0)
        for total in ordered:
            for i, a in enumerate(ordered):
                if a.value_num is None or total.value_num is None or a.value_num >= total.value_num:
                    break
                for b in ordered[i + 1 :]:
                    if b.value_num is None or b.value_num >= total.value_num:
                        break
                    if len({a.attribute_key, b.attribute_key, total.attribute_key}) < 3:
                        continue
                    if len({a.document_id, b.document_id, total.document_id}) < 2:
                        continue
                    if values_equal(a.value_num + b.value_num, total.value_num, total.unit):
                        big, small = b, a  # ascending order, so b is the larger part
                        explanation = (
                            f"{_fmt(big.value_num or 0)} + {_fmt(small.value_num or 0)} = "
                            f"{_fmt(total.value_num)} ({big.attribute_key} + "
                            f"{small.attribute_key} = {total.attribute_key})"
                        )
                        edges.append(ArithmeticEdge((big, small), total, explanation))
    return edges
