"""The processing pipeline: page image + text -> model -> verified, normalised, stored facts.

Every step that can be decided in code is decided in code: quote verification, unit and period
normalisation, keys, plausibility flags and duplicate detection. The model only reads.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, and_, func, or_, select, update
from sqlalchemy.orm import Session

from fkl.compare import (
    FactView,
    build_candidates,
    classify_pair,
    find_arithmetic_relations,
    validate_fact,
    values_equal,
)
from fkl.db import session_scope
from fkl.llm.adjudicate import adjudicate
from fkl.llm.client import LLMClient, LLMJsonError, LLMQuotaError
from fkl.llm.extract import extract_page
from fkl.llm.prompts import DocumentContext, extraction_request, meta_request
from fkl.llm.structured import call_structured
from fkl.models import Document, Fact, Failure, Page, Relation, utcnow
from fkl.normalize import (
    Period,
    canonical_key,
    normalize_unit,
    parse_period,
    period_key,
    to_base_value,
)
from fkl.pdf import render_jpeg
from fkl.schemas import DocumentMeta, ExtractedFact
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


# --------------------------------------------------------------------------------------------
# Document-level batch processing (time-boxed, idempotent, resumable)
# --------------------------------------------------------------------------------------------

STALE_PROCESSING_SECONDS = 300
_MAX_REPORTED_ERRORS = 5


@dataclass(frozen=True)
class ProcessProgress:
    document_id: str
    status: str
    total_pages: int
    done: int
    failed: int
    skipped: int
    pending: int
    processed_this_call: int
    estimated_calls_remaining: int
    last_errors: list[str]


def ensure_document_meta(db: Session, document: Document, deps: PipelineDeps) -> None:
    """Read title, publisher, publication date and fiscal-year convention from the first pages.

    Failure here is logged and tolerated: extraction still works, only vintage reasoning weakens.
    """
    if document.meta_done:
        return
    first_pages = db.scalars(
        select(Page).where(Page.document_id == document.id).order_by(Page.index).limit(3)
    ).all()
    request = meta_request(
        model=deps.extract_model,
        filename=document.filename,
        first_pages_text="\n\n".join(page.text for page in first_pages),
    )
    try:
        meta = call_structured(deps.client, request, DocumentMeta)
    except Exception as error:  # noqa: BLE001 - recorded, never fatal
        db.add(
            Failure(
                document_id=document.id,
                stage="meta",
                kind=_failure_kind(error),
                message=str(error)[:2000],
                handled="continuing without metadata; unknown publication date weakens vintage rules",
            )
        )
        return
    document.title = meta.title or document.title
    document.publisher = meta.publisher or document.publisher
    document.publication_date = _iso(meta.publication_date) or document.publication_date
    document.document_type = meta.document_type or document.document_type
    document.fiscal_year_start_month = meta.fiscal_year_start_month or deps.fiscal_year_start_month
    document.meta_done = True


def _stale_cutoff() -> datetime:
    return utcnow() - timedelta(seconds=STALE_PROCESSING_SECONDS)


def _claimable_condition() -> Any:
    return or_(
        Page.status == "pending",
        and_(Page.status == "failed", Page.attempts < MAX_PAGE_ATTEMPTS),
        and_(Page.status == "processing", Page.updated_at < _stale_cutoff()),
    )


def _claimable_page_ids(db: Session, document_id: str, retry_failed: bool) -> list[int]:
    if retry_failed:
        db.execute(
            update(Page)
            .where(Page.document_id == document_id, Page.status == "failed")
            .values(attempts=0)
        )
    query = (
        select(Page.id)
        .where(Page.document_id == document_id, _claimable_condition())
        .order_by(Page.index)
    )
    return list(db.scalars(query))


def _claim(db: Session, page_id: int) -> bool:
    """Atomically take ownership of a page so overlapping calls never process it twice."""
    result = db.execute(
        update(Page)
        .where(Page.id == page_id, _claimable_condition())
        .values(status="processing", updated_at=utcnow())
    )
    db.commit()
    return bool(getattr(result, "rowcount", 0) == 1)


WorkResult = tuple[bool, str | None]


def _run_time_boxed(
    item_ids: list[int],
    work: Callable[[int], WorkResult],
    deadline: float,
    concurrency: int,
) -> tuple[int, list[str]]:
    """Dispatch items to a small pool until the deadline; report how many were claimed."""
    queue = iter(item_ids)
    processed = 0
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        in_flight: set[Future[WorkResult]] = set()

        def submit_next() -> bool:
            if time.monotonic() >= deadline:
                return False
            item_id = next(queue, None)
            if item_id is None:
                return False
            in_flight.add(pool.submit(work, item_id))
            return True

        for _ in range(concurrency):
            if not submit_next():
                break
        while in_flight:
            finished, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in finished:
                claimed, error = future.result()
                processed += int(claimed)
                if error:
                    errors.append(error)
                submit_next()
    return processed, errors


def _run_page(
    engine: Engine, document_id: str, page_id: int, pdf_bytes: bytes, deps: PipelineDeps
) -> WorkResult:
    """Worker: claim, process and commit one page in its own transaction."""
    with session_scope(engine) as db:
        if not _claim(db, page_id):
            return False, None
        document = db.get(Document, document_id)
        page = db.get(Page, page_id)
        if document is None or page is None:
            return False, "document or page vanished"
        try:
            process_page(db, document, page, pdf_bytes, deps)
        except Exception as error:  # noqa: BLE001 - keep the batch alive, report the page
            page.status = "failed"
            page.last_error = str(error)[:2000]
            db.flush()
        return True, page.last_error


def _progress(
    engine: Engine, document_id: str, processed: int, errors: list[str]
) -> ProcessProgress:
    with session_scope(engine) as db:
        rows = db.execute(
            select(Page.status, func.count())
            .where(Page.document_id == document_id)
            .group_by(Page.status)
        ).all()
        counts: dict[str, int] = {status: count for status, count in rows}
        remaining = (
            db.scalar(
                select(func.count()).where(Page.document_id == document_id, _claimable_condition())
            )
            or 0
        )
        document = db.get(Document, document_id)
        assert document is not None
        pending = counts.get("pending", 0) + counts.get("processing", 0)
        document.status = "extracted" if pending == 0 and remaining == 0 else "processing"
        return ProcessProgress(
            document_id=document_id,
            status=document.status,
            total_pages=sum(counts.values()),
            done=counts.get("done", 0),
            failed=counts.get("failed", 0),
            skipped=counts.get("skipped", 0),
            pending=pending,
            processed_this_call=processed,
            estimated_calls_remaining=math.ceil(remaining / max(processed, 1)) if remaining else 0,
            last_errors=errors[-_MAX_REPORTED_ERRORS:],
        )


def process_document(
    engine: Engine,
    document_id: str,
    pdf_bytes: bytes,
    deps: PipelineDeps,
    *,
    retry_failed: bool = False,
) -> ProcessProgress:
    """Process as many claimable pages as the time budget allows; safe to call repeatedly.

    Each page is committed on its own, so a killed invocation loses at most the pages in flight,
    and the next call simply continues.
    """
    deadline = time.monotonic() + deps.budget_s
    with session_scope(engine) as db:
        document = db.get(Document, document_id)
        if document is None:
            raise LookupError(f"unknown document {document_id}")
        if document.status == "uploaded":
            document.status = "processing"
        ensure_document_meta(db, document, deps)
        page_ids = _claimable_page_ids(db, document_id, retry_failed)

    processed, errors = _run_time_boxed(
        page_ids,
        lambda page_id: _run_page(engine, document_id, page_id, pdf_bytes, deps),
        deadline,
        deps.page_concurrency,
    )
    return _progress(engine, document_id, processed, errors)


# --------------------------------------------------------------------------------------------
# Cross-document linking: rule pass, then LLM adjudication of the ambiguous residue
# --------------------------------------------------------------------------------------------

MAX_ADJUDICATION_ATTEMPTS = 3


@dataclass(frozen=True)
class LinkProgress:
    total_relations: int
    final: int
    pending_llm: int
    failed: int
    adjudicated_this_call: int
    by_verdict: dict[str, int]


def _fact_views(db: Session) -> list[FactView]:
    rows = db.execute(
        select(Fact, Document.publication_date)
        .join(Document, Fact.document_id == Document.id)
        .where(Fact.is_duplicate.is_(False), Fact.value_num.is_not(None))
    ).all()
    return [
        FactView(
            id=fact.id,
            document_id=fact.document_id,
            entity_key=fact.entity_key,
            attribute_key=fact.attribute_key,
            value_num=fact.value_num,
            unit=fact.unit,
            period_key=fact.period_key,
            estimate_type=fact.estimate_type,
            basis_key=fact.basis_key,
            attributed_to=fact.attributed_to,
            evidence_verified=fact.evidence_verified,
            publication_date=published,
        )
        for fact, published in rows
    ]


def rule_pass(db: Session) -> int:
    """Pair every not-yet-paired fact against the whole layer and classify by rules.

    Only pairs that involve at least one new fact are considered, which is what makes adding a
    document incremental: existing relations are never recomputed.
    """
    unpaired = set(db.scalars(select(Fact.id).where(Fact.paired_at.is_(None))).all())
    if not unpaired:
        return 0
    existing: set[tuple[int, int]] = {
        (a, b) for a, b in db.execute(select(Relation.fact_a_id, Relation.fact_b_id)).all()
    }
    created = 0
    views = _fact_views(db)
    for a, b in build_candidates(views):
        if (a.id not in unpaired and b.id not in unpaired) or (a.id, b.id) in existing:
            continue
        result = classify_pair(a, b)
        if result is None:
            continue
        db.add(
            Relation(
                fact_a_id=a.id,
                fact_b_id=b.id,
                verdict=result.verdict,
                status=result.status,
                method=result.method,
                dimension=result.dimension,
                explanation=result.hypothesis,
                confidence=result.confidence,
                winner_fact_id=result.winner_id,
                rule_hypothesis=result.hypothesis,
            )
        )
        existing.add((a.id, b.id))
        created += 1
    for edge in find_arithmetic_relations(views):
        involved = {edge.total.id, *(part.id for part in edge.parts)}
        if not involved & unpaired:
            continue
        for part in edge.parts:
            key = (min(part.id, edge.total.id), max(part.id, edge.total.id))
            if key in existing:
                continue
            db.add(
                Relation(
                    fact_a_id=key[0],
                    fact_b_id=key[1],
                    verdict="derived",
                    status="final",
                    method="arithmetic",
                    dimension="none",
                    explanation=edge.explanation,
                    confidence=0.85,
                    rule_hypothesis="arithmetic tie-out: parts sum to total",
                )
            )
            existing.add(key)
            created += 1
    db.execute(update(Fact).where(Fact.paired_at.is_(None)).values(paired_at=utcnow()))
    db.flush()
    return created


def _adjudicate_relation(engine: Engine, relation_id: int, deps: PipelineDeps) -> WorkResult:
    with session_scope(engine) as db:
        relation = db.get(Relation, relation_id)
        if relation is None or relation.status != "pending_llm":
            return False, None
        fact_a, fact_b = db.get(Fact, relation.fact_a_id), db.get(Fact, relation.fact_b_id)
        if fact_a is None or fact_b is None:
            return False, "facts vanished"
        document_a, document_b = (
            db.get(Document, fact_a.document_id),
            db.get(Document, fact_b.document_id),
        )
        assert document_a is not None and document_b is not None
        relation.attempts += 1
        try:
            verdict = adjudicate(
                deps.client,
                deps.adjudicate_model,
                fact_a,
                document_a,
                fact_b,
                document_b,
                relation.rule_hypothesis or "",
            )
        except Exception as error:  # noqa: BLE001 - recorded; the relation keeps its hypothesis
            exhausted = relation.attempts >= MAX_ADJUDICATION_ATTEMPTS
            if exhausted:
                relation.status = "failed"
            db.add(
                Failure(
                    document_id=fact_a.document_id,
                    fact_id=fact_a.id,
                    relation_id=relation.id,
                    stage="link",
                    kind="adjudicate_failed",
                    message=str(error)[:2000],
                    handled=(
                        "kept the rule hypothesis as the verdict and marked the relation failed"
                        if exhausted
                        else f"attempt {relation.attempts} of {MAX_ADJUDICATION_ATTEMPTS}; will retry"
                    ),
                )
            )
            return True, str(error)
        both_verified = fact_a.evidence_verified and fact_b.evidence_verified
        relation.verdict = verdict.verdict
        relation.dimension = verdict.dimension
        relation.explanation = verdict.explanation
        relation.confidence = verdict.confidence if both_verified else verdict.confidence / 2
        relation.winner_fact_id = {"a": fact_a.id, "b": fact_b.id}.get(verdict.newer_fact)
        relation.method = "llm"
        relation.status = "final"
        relation.llm_raw = verdict.model_dump()
        return True, None


def _link_progress(engine: Engine, adjudicated: int) -> LinkProgress:
    with session_scope(engine) as db:
        status_rows = db.execute(
            select(Relation.status, func.count()).group_by(Relation.status)
        ).all()
        by_status: dict[str, int] = {status: count for status, count in status_rows}
        verdict_rows = db.execute(
            select(Relation.verdict, func.count()).group_by(Relation.verdict)
        ).all()
        return LinkProgress(
            total_relations=sum(by_status.values()),
            final=by_status.get("final", 0),
            pending_llm=by_status.get("pending_llm", 0),
            failed=by_status.get("failed", 0),
            adjudicated_this_call=adjudicated,
            by_verdict={verdict: count for verdict, count in verdict_rows},
        )


def link(engine: Engine, deps: PipelineDeps) -> LinkProgress:
    """Run the rule pass over new facts, then adjudicate pending pairs within the time budget."""
    deadline = time.monotonic() + deps.budget_s
    with session_scope(engine) as db:
        rule_pass(db)
        pending = list(
            db.scalars(
                select(Relation.id)
                .where(
                    Relation.status == "pending_llm",
                    Relation.attempts < MAX_ADJUDICATION_ATTEMPTS,
                )
                .order_by(Relation.id)
            )
        )
    adjudicated, _errors = _run_time_boxed(
        pending,
        lambda relation_id: _adjudicate_relation(engine, relation_id, deps),
        deadline,
        deps.page_concurrency,
    )
    return _link_progress(engine, adjudicated)
