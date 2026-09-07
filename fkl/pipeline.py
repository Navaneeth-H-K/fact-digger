"""The processing pipeline: page image + text -> model -> verified, normalised, stored facts.

Every step that can be decided in code is decided in code: quote verification, unit and period
normalisation, keys, plausibility flags and duplicate detection. The model only reads.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fkl.compare import FactView, validate_fact, values_equal
from fkl.llm.client import LLMClient, LLMJsonError, LLMQuotaError
from fkl.llm.extract import extract_page
from fkl.llm.prompts import DocumentContext, extraction_request
from fkl.models import Document, Fact, Failure, Page
from fkl.normalize import (
    Period,
    canonical_key,
    normalize_unit,
    parse_period,
    period_key,
    to_base_value,
)
from fkl.pdf import render_jpeg
from fkl.schemas import ExtractedFact
from fkl.verify import verify_quote

MAX_PAGE_ATTEMPTS = 3
_CURRENCY_UNITS = frozenset({"INR", "USD"})


@dataclass(frozen=True)
class PipelineDeps:
    client: LLMClient
    extract_model: str
    adjudicate_model: str
    fiscal_year_start_month: int = 4
    page_concurrency: int = 4
    budget_s: float = 45.0
    image_width: int = 1200


def document_context(document: Document, default_fy_start: int) -> DocumentContext:
    return DocumentContext(
        filename=document.filename,
        title=document.title,
        publisher=document.publisher,
        publication_date=document.publication_date,
        fiscal_year_start_month=document.fiscal_year_start_month or default_fy_start,
    )


def attribute_vocabulary(db: Session, limit: int = 150) -> list[str]:
    """Most frequent attribute names so far; fed back to the model to keep naming consistent."""
    rows = db.execute(
        select(func.max(Fact.attribute), func.count())
        .where(Fact.is_duplicate.is_(False))
        .group_by(Fact.attribute_key)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    return [name for name, _ in rows]


def _iso(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _period_for(extracted: ExtractedFact, fy_start_month: int) -> Period | None:
    parsed = parse_period(extracted.period, fy_start_month)
    if parsed is not None:
        return parsed
    start, end = _iso(extracted.period_start), _iso(extracted.period_end)
    if start and end and start <= end:
        return Period(start, end, "asof" if start == end else "range")
    return None


def _scale_known(extracted: ExtractedFact, unit: str) -> bool:
    if unit not in _CURRENCY_UNITS or extracted.scale:
        return True
    haystack = f"{extracted.unit or ''} {extracted.value}".lower()
    return any(
        word in haystack for word in ("lakh", "crore", "million", "billion", "mn", "bn", "cr")
    )


def _fact_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def build_fact(
    document: Document, page: Page, extracted: ExtractedFact, fy_start_month: int
) -> Fact:
    """Turn a validated model output into a stored fact with evidence, normalised values and keys."""
    verification = verify_quote(extracted.quote, page.text, extracted.quote_source)
    unit = normalize_unit(extracted.unit)
    value_num = (
        to_base_value(extracted.value, extracted.unit, extracted.scale)
        if extracted.value_kind == "number"
        else None
    )
    period = _period_for(extracted, fy_start_month)
    entity_key = canonical_key(extracted.entity)
    attribute_key = canonical_key(extracted.attribute)
    basis_key = canonical_key(extracted.measurement_basis)
    key = period_key(period)
    fact = Fact(
        document_id=document.id,
        page_id=page.id,
        page_index=page.index,
        page_label=page.label,
        entity=extracted.entity,
        attribute=extracted.attribute,
        value_raw=extracted.value,
        value_kind=extracted.value_kind,
        unit_raw=extracted.unit,
        scale_raw=extracted.scale,
        period_raw=extracted.period,
        estimate_type=extracted.estimate_type,
        measurement_basis=extracted.measurement_basis,
        attributed_to=extracted.attributed_to,
        quote=extracted.quote,
        quote_source=extracted.quote_source,
        confidence=extracted.confidence,
        notes=extracted.notes,
        value_num=value_num,
        unit=unit,
        scale=extracted.scale,
        period_start=period.start if period else None,
        period_end=period.end if period else None,
        period_kind=period.kind if period else "unknown",
        entity_key=entity_key,
        attribute_key=attribute_key,
        period_key=key,
        basis_key=basis_key,
        fact_key=_fact_key(
            entity_key, attribute_key, key, unit, extracted.estimate_type, basis_key
        ),
        evidence_verified=verification.verified,
        verify_score=verification.score,
        verify_method=verification.method,
    )
    fact.validator_flags = validate_fact(
        FactView(
            id=0,
            document_id=document.id,
            entity_key=entity_key,
            attribute_key=attribute_key,
            value_num=value_num,
            unit=unit,
            period_key=key,
            estimate_type=extracted.estimate_type,
            basis_key=basis_key,
            attributed_to=extracted.attributed_to,
            evidence_verified=verification.verified,
            publication_date=document.publication_date,
        ),
        period_end=period.end if period else None,
        publication_date=document.publication_date,
        scale_known=_scale_known(extracted, unit),
    )
    return fact


def _mark_duplicate(db: Session, fact: Fact) -> None:
    """A repeat of an already stored fact (same key, same value) in the same document."""
    existing = db.scalars(
        select(Fact).where(
            Fact.document_id == fact.document_id,
            Fact.fact_key == fact.fact_key,
            Fact.is_duplicate.is_(False),
        )
    ).first()
    if existing is None or existing.value_num is None or fact.value_num is None:
        return
    fact.is_duplicate = values_equal(existing.value_num, fact.value_num, fact.unit)


def _evidence_failures(fact: Fact) -> list[Failure]:
    failures: list[Failure] = []
    common = {"document_id": fact.document_id, "page_index": fact.page_index, "fact_id": fact.id}
    if fact.verify_method == "not_found":
        failures.append(
            Failure(
                stage="verify",
                kind="quote_not_found",
                message=f"quote not found on page (score {fact.verify_score}): {fact.quote[:200]}",
                handled="kept the fact, flagged unverified; relations involving it get half confidence",
                **common,
            )
        )
    elif fact.verify_method == "image_only":
        failures.append(
            Failure(
                stage="verify",
                kind="image_only_evidence",
                message=f"value read from the page image, absent from text layer: {fact.quote[:200]}",
                handled="kept the fact; evidence is the page image, shown with a 'visual evidence' badge",
                **common,
            )
        )
    if fact.validator_flags:
        failures.append(
            Failure(
                stage="validate",
                kind="validator",
                message=", ".join(fact.validator_flags),
                handled="kept the fact with plausibility flags shown in the UI",
                **common,
            )
        )
    return failures


def _failure_kind(error: Exception) -> str:
    if isinstance(error, LLMQuotaError):
        return "llm_quota"
    if isinstance(error, LLMJsonError):
        return "llm_json"
    return "llm_other"


def process_page(
    db: Session, document: Document, page: Page, pdf_bytes: bytes, deps: PipelineDeps
) -> int:
    """Extract, verify, normalise and store the facts of one page. Returns facts stored."""
    page.attempts += 1
    context = document_context(document, deps.fiscal_year_start_month)
    try:
        image = render_jpeg(pdf_bytes, page.index, width=deps.image_width)
        request = extraction_request(
            model=deps.extract_model,
            image_jpeg=image,
            page_text=page.text,
            document=context,
            page_index=page.index,
            page_label=page.label,
            vocabulary=attribute_vocabulary(db),
        )
        outcome = extract_page(deps.client, request)
    except Exception as error:  # noqa: BLE001 - every failure is recorded, never raised
        page.status = "failed"
        page.last_error = str(error)[:2000]
        retry_note = (
            f"attempt {page.attempts} of {MAX_PAGE_ATTEMPTS}; will retry on a later batch"
            if page.attempts < MAX_PAGE_ATTEMPTS
            else "gave up after the maximum number of attempts"
        )
        db.add(
            Failure(
                document_id=document.id,
                page_index=page.index,
                stage="extract",
                kind=_failure_kind(error),
                message=str(error)[:2000],
                handled=retry_note,
            )
        )
        db.flush()
        return 0

    for raw, reason in outcome.invalid:
        db.add(
            Failure(
                document_id=document.id,
                page_index=page.index,
                stage="parse",
                kind="llm_json",
                message=f"fact failed validation: {reason[:500]} | raw: {str(raw)[:300]}",
                handled="dropped this fact only; the rest of the page was kept",
            )
        )

    stored = 0
    for extracted in outcome.facts:
        fact = build_fact(document, page, extracted, context.fiscal_year_start_month)
        _mark_duplicate(db, fact)
        db.add(fact)
        db.flush()
        for failure in _evidence_failures(fact):
            db.add(failure)
        stored += 1

    page.status = "done"
    page.facts_count = stored
    page.last_error = None
    db.flush()
    return stored
