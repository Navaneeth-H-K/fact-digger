"""Storage schema: SQLAlchemy models must work on SQLite (tests) and Postgres (production)."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fkl.db import make_engine, normalize_database_url, session_scope
from fkl.models import Document, Fact, Failure, LlmCacheEntry, Page, Relation


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    db = Session(engine)
    yield db
    db.rollback()
    db.close()


def test_session_scope_commits_and_closes() -> None:
    engine = make_engine("sqlite://")
    with session_scope(engine) as db:
        new_document(db)
    with Session(engine) as check:
        assert check.get(Document, "doc-a") is not None


def new_document(db: Session, doc_id: str = "doc-a") -> Document:
    document = Document(
        id=doc_id,
        filename="a.pdf",
        size_bytes=10,
        sha256="0" * 64,
        storage_path=f"pdfs/{doc_id}.pdf",
    )
    db.add(document)
    db.flush()
    return document


def new_page(db: Session, document: Document, index: int = 0) -> Page:
    page = Page(document_id=document.id, index=index, text="Revenue 81,415.38", char_count=17)
    db.add(page)
    db.flush()
    return page


def new_fact(db: Session, document: Document, page: Page, **overrides: object) -> Fact:
    values: dict[str, object] = {
        "document_id": document.id,
        "page_id": page.id,
        "page_index": page.index,
        "entity": "Delhivery Limited",
        "attribute": "Revenue from operations",
        "value_raw": "81,415.38",
        "value_kind": "number",
        "estimate_type": "actual",
        "quote": "Revenue 81,415.38",
        "quote_source": "text",
        "confidence": 0.9,
        "entity_key": "delhivery limited",
        "attribute_key": "operation revenue",
        "period_key": "unknown",
        "basis_key": "",
        "fact_key": "abc",
        "evidence_verified": True,
        "verify_score": 1.0,
        "verify_method": "exact",
    }
    values.update(overrides)
    fact = Fact(**values)
    db.add(fact)
    db.flush()
    return fact


def test_normalize_database_url_rewrites_postgres_scheme_for_psycopg() -> None:
    assert (
        normalize_database_url("postgres://u:p@h:6543/db") == "postgresql+psycopg://u:p@h:6543/db"
    )
    assert normalize_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("sqlite:///./fkl.db") == "sqlite:///./fkl.db"


def test_tables_are_created_and_rows_round_trip(session: Session) -> None:
    document = new_document(session)
    page = new_page(session, document)
    fact = new_fact(session, document, page)
    session.add(
        Failure(
            document_id=document.id,
            page_index=0,
            stage="verify",
            kind="quote_not_found",
            message="x",
            handled="kept, flagged",
        )
    )
    session.add(
        LlmCacheEntry(
            key="k" * 64,
            model="m",
            purpose="extract",
            request={"a": 1},
            response={"b": 2},
            usage={},
        )
    )
    session.flush()
    assert document.status == "uploading"
    assert page.status == "pending"
    assert fact.id is not None and fact.is_duplicate is False and fact.validator_flags == []
    assert isinstance(document.created_at, datetime)


def test_page_index_is_unique_per_document(session: Session) -> None:
    document = new_document(session)
    new_page(session, document, index=0)
    with pytest.raises(IntegrityError):
        new_page(session, document, index=0)


def test_relation_pair_is_unique(session: Session) -> None:
    document = new_document(session)
    other = new_document(session, "doc-b")
    page_a, page_b = new_page(session, document), new_page(session, other)
    a, b = new_fact(session, document, page_a), new_fact(session, other, page_b)
    session.add(
        Relation(
            fact_a_id=a.id,
            fact_b_id=b.id,
            verdict="corroborates",
            status="final",
            method="rule",
            explanation="same",
        )
    )
    session.flush()
    with pytest.raises(IntegrityError):
        session.add(
            Relation(
                fact_a_id=a.id,
                fact_b_id=b.id,
                verdict="contradicts",
                status="final",
                method="rule",
                explanation="dup",
            )
        )
        session.flush()


def test_timestamps_are_timezone_aware_utc(session: Session) -> None:
    document = new_document(session)
    assert document.created_at.tzinfo is not None
    assert document.created_at <= datetime.now(UTC)
