"""Describe a pair of facts for the adjudication prompt and obtain a validated verdict."""

from __future__ import annotations

from fkl.llm.client import LLMClient
from fkl.llm.prompts import adjudication_request
from fkl.llm.structured import call_structured
from fkl.models import Document, Fact
from fkl.schemas import Verdict


def describe_fact(fact: Fact, document: Document) -> str:
    """Everything the adjudicator may use, and nothing it may not (no other facts)."""
    period = fact.period_raw or "unknown"
    if fact.period_start and fact.period_end:
        period += f" ({fact.period_start} to {fact.period_end})"
    lines = [
        f"entity: {fact.entity}",
        f"attribute: {fact.attribute}",
        f"value: {fact.value_raw} {fact.unit_raw or ''} {fact.scale_raw or ''}".rstrip(),
        f"normalised value: {fact.value_num} {fact.unit}",
        f"period: {period}",
        f"estimate type: {fact.estimate_type}",
        f"measurement basis: {fact.measurement_basis or 'not stated'}",
        f"attributed to: {fact.attributed_to or 'the document itself'}",
        f"document: {document.title or document.filename}; publisher: "
        f"{document.publisher or 'unknown'}; published: {document.publication_date or 'unknown'}",
        f"page label: {fact.page_label or fact.page_index}",
        f"evidence verified: {fact.evidence_verified} ({fact.verify_method})",
        f'quote: "{fact.quote}"',
    ]
    return "\n".join(lines)


def adjudicate(
    client: LLMClient,
    model: str,
    fact_a: Fact,
    document_a: Document,
    fact_b: Fact,
    document_b: Document,
    hypothesis: str,
) -> Verdict:
    request = adjudication_request(
        model=model,
        fact_a=describe_fact(fact_a, document_a),
        fact_b=describe_fact(fact_b, document_b),
        hypothesis=hypothesis,
    )
    return call_structured(client, request, Verdict)
