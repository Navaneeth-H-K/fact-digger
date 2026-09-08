"""Cross-document linking: rule pass creates relations, the LLM adjudicates only ambiguous pairs."""

from datetime import date
from typing import Any

from sqlalchemy import Engine, select

from fkl.db import make_engine, session_scope
from fkl.llm.client import LLMClient, LLMRequest
from fkl.models import Document, Fact, Failure, Page, Relation
from fkl.pipeline import MAX_ADJUDICATION_ATTEMPTS, PipelineDeps, link

FY24 = "2023-04-01:2024-03-31"
FY25 = "2024-04-01:2025-03-31"


def seed_fact(
    db: Any,
    document: Document,
    page: Page,
    *,
    entity: str = "India",
    attribute: str,
    value_num: float,
    unit: str = "percent",
    period_key: str = FY24,
    estimate_type: str = "actual",
    basis_key: str = "",
    verified: bool = True,
) -> Fact:
    fact = Fact(
        document_id=document.id,
        page_id=page.id,
        page_index=page.index,
        entity=entity,
        attribute=attribute,
        value_raw=str(value_num),
        value_kind="number",
        estimate_type=estimate_type,
        quote=f"{attribute} {value_num}",
        confidence=0.9,
        value_num=value_num,
        unit=unit,
        entity_key=entity.lower(),
        attribute_key=" ".join(sorted(attribute.lower().split())),
        period_key=period_key,
        basis_key=basis_key,
        fact_key=f"{entity}|{attribute}|{period_key}|{unit}|{estimate_type}|{basis_key}",
        evidence_verified=verified,
        verify_score=1.0 if verified else 0.0,
        verify_method="exact" if verified else "not_found",
    )
    db.add(fact)
    db.flush()
    return fact


def seed_documents(engine: Engine) -> tuple[str, str]:
    with session_scope(engine) as db:
        docs = []
        for doc_id, published, title in (
            ("doc-es", date(2025, 1, 30), "Economic Survey 2024-25"),
            ("doc-rbi", date(2025, 5, 29), "RBI Annual Report 2024-25"),
        ):
            document = Document(
                id=doc_id,
                filename=f"{doc_id}.pdf",
                size_bytes=1,
                sha256=doc_id.ljust(64, "0"),
                storage_path=f"pdfs/{doc_id}.pdf",
                status="extracted",
                title=title,
                publisher=title.split(" ")[0],
                publication_date=published,
                page_count=1,
                meta_done=True,
            )
            db.add(document)
            page = Page(document_id=doc_id, index=0, text="page", char_count=4, status="done")
            db.add(page)
            db.flush()
            docs.append((document, page))
        (es, es_page), (rbi, rbi_page) = docs
        seed_fact(db, es, es_page, attribute="cpi headline inflation", value_num=5.4)
        seed_fact(db, rbi, rbi_page, attribute="cpi headline inflation", value_num=5.4)
        seed_fact(
            db,
            es,
            es_page,
            attribute="real gdp growth",
            value_num=6.4,
            period_key=FY25,
            estimate_type="advance_estimate",
        )
        seed_fact(
            db,
            rbi,
            rbi_page,
            attribute="real gdp growth",
            value_num=6.5,
            period_key=FY25,
            estimate_type="revised",
        )
        seed_fact(db, es, es_page, attribute="general government deficit", value_num=8.8)
        seed_fact(db, rbi, rbi_page, attribute="general government deficit", value_num=8.1)
        seed_fact(db, es, es_page, attribute="unrelated indicator", value_num=1.0)
    return "doc-es", "doc-rbi"


class Adjudicator:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.requests: list[LLMRequest] = []

    def __call__(self, req: LLMRequest) -> dict[str, Any]:
        assert req.purpose == "adjudicate"
        self.requests.append(req)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("adjudicator unavailable")
        return {
            "verdict": "contradicts",
            "dimension": "value",
            "explanation": "Both cover FY24 on the same basis: 8.8 per cent vs 8.1 per cent.",
            "newer_fact": "none",
            "confidence": 0.8,
        }


def deps_for(fake: Any) -> PipelineDeps:
    return PipelineDeps(
        client=LLMClient(mode="off", fake=fake),
        extract_model="m",
        adjudicate_model="opus",
        page_concurrency=1,
        budget_s=30,
    )


def test_rule_pass_and_adjudication_produce_expected_relations() -> None:
    engine = make_engine("sqlite://")
    seed_documents(engine)
    adjudicator = Adjudicator()
    progress = link(engine, deps_for(adjudicator))
    assert progress.total_relations == 3
    assert progress.final == 3 and progress.pending_llm == 0 and progress.failed == 0
    assert progress.adjudicated_this_call == 1
    assert progress.by_verdict == {"contradicts": 1, "corroborates": 1, "superseded": 1}

    with session_scope(engine) as db:
        relations = {r.rule_hypothesis: r for r in db.scalars(select(Relation)).all()}
        corroborated = next(r for r in relations.values() if r.verdict == "corroborates")
        assert corroborated.method == "rule" and corroborated.status == "final"
        superseded = next(r for r in relations.values() if r.verdict == "superseded")
        winner = db.get(Fact, superseded.winner_fact_id)
        assert winner is not None and winner.document_id == "doc-rbi"
        contradiction = next(r for r in relations.values() if r.verdict == "contradicts")
        assert contradiction.method == "llm" and contradiction.llm_raw is not None
        assert "8.8" in contradiction.explanation
        assert all(f.paired_at is not None for f in db.scalars(select(Fact)).all())

    request_text = adjudicator.requests[0].content[0]["text"]
    assert "FACT A" in request_text and "Economic Survey" in request_text and "8.1" in request_text
    assert adjudicator.requests[0].model == "opus"


def test_link_is_idempotent_and_only_pairs_new_facts() -> None:
    engine = make_engine("sqlite://")
    seed_documents(engine)
    adjudicator = Adjudicator()
    link(engine, deps_for(adjudicator))
    second = link(engine, deps_for(adjudicator))
    assert second.total_relations == 3 and second.adjudicated_this_call == 0
    assert len(adjudicator.requests) == 1


def test_failed_adjudication_is_retried_then_marked_failed() -> None:
    engine = make_engine("sqlite://")
    seed_documents(engine)
    adjudicator = Adjudicator(fail_times=MAX_ADJUDICATION_ATTEMPTS + 1)
    for _ in range(MAX_ADJUDICATION_ATTEMPTS):
        progress = link(engine, deps_for(adjudicator))
    assert progress.failed == 1 and progress.pending_llm == 0
    with session_scope(engine) as db:
        relation = db.scalars(select(Relation).where(Relation.status == "failed")).one()
        assert relation.attempts == MAX_ADJUDICATION_ATTEMPTS
        assert relation.verdict == "contradicts"  # the rule hypothesis stays visible
        failures = db.scalars(select(Failure).where(Failure.stage == "link")).all()
        assert len(failures) == MAX_ADJUDICATION_ATTEMPTS
        assert failures[0].kind == "adjudicate_failed"


def test_quota_exhaustion_pauses_adjudication_without_spending_attempts() -> None:
    from fkl.llm.client import LLMQuotaError

    engine = make_engine("sqlite://")
    seed_documents(engine)

    def quota_gone(req: LLMRequest) -> dict[str, Any]:
        raise LLMQuotaError("model quota exhausted: 402")

    progress = link(engine, deps_for(quota_gone))
    assert progress.paused_reason is not None and "quota" in progress.paused_reason
    assert (
        progress.pending_llm == 1 and progress.failed == 0 and progress.adjudicated_this_call == 0
    )
    with session_scope(engine) as db:
        relation = db.scalars(select(Relation).where(Relation.status == "pending_llm")).one()
        assert relation.attempts == 0
    resumed = link(engine, deps_for(Adjudicator()))
    assert resumed.paused_reason is None and resumed.pending_llm == 0
