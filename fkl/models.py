"""SQLAlchemy schema for documents, pages, facts, relations, failures and the LLM cache.

Column types are restricted to what both SQLite and Postgres support so the same models back
the test suite, local runs and the Supabase deployment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo on the way back)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="uploading")
    page_count: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(500))
    publisher: Mapped[str | None] = mapped_column(String(255))
    publication_date: Mapped[Any | None] = mapped_column(Date)
    document_type: Mapped[str | None] = mapped_column(String(100))
    fiscal_year_start_month: Mapped[int | None] = mapped_column(Integer)
    meta_done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

    pages: Mapped[list[Page]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    facts: Mapped[list[Fact]] = relationship(cascade="all, delete-orphan", passive_deletes=True)


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("document_id", "index", name="uq_page_document_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text, default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    facts_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

    document: Mapped[Document] = relationship(back_populates="pages")


class Fact(Base):
    __tablename__ = "facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    page_index: Mapped[int] = mapped_column(Integer)
    page_label: Mapped[str | None] = mapped_column(String(32))

    # As extracted (verbatim from the model)
    entity: Mapped[str] = mapped_column(String(255))
    attribute: Mapped[str] = mapped_column(String(255))
    value_raw: Mapped[str] = mapped_column(String(255))
    value_kind: Mapped[str] = mapped_column(String(16))
    unit_raw: Mapped[str | None] = mapped_column(String(64))
    scale_raw: Mapped[str | None] = mapped_column(String(32))
    period_raw: Mapped[str | None] = mapped_column(String(128))
    estimate_type: Mapped[str] = mapped_column(String(32), default="unknown")
    measurement_basis: Mapped[str | None] = mapped_column(String(255))
    attributed_to: Mapped[str | None] = mapped_column(String(255))
    quote: Mapped[str] = mapped_column(Text)
    quote_source: Mapped[str] = mapped_column(String(8), default="text")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    notes: Mapped[str | None] = mapped_column(Text)

    # Normalised
    value_num: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16), default="count")
    scale: Mapped[str | None] = mapped_column(String(32))
    period_start: Mapped[Any | None] = mapped_column(Date)
    period_end: Mapped[Any | None] = mapped_column(Date)
    period_kind: Mapped[str] = mapped_column(String(16), default="unknown")

    # Keys
    entity_key: Mapped[str] = mapped_column(String(255), index=True)
    attribute_key: Mapped[str] = mapped_column(String(255), index=True)
    period_key: Mapped[str] = mapped_column(String(32), index=True)
    basis_key: Mapped[str] = mapped_column(String(255), default="")
    fact_key: Mapped[str] = mapped_column(String(64), index=True)

    # Evidence
    evidence_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verify_score: Mapped[float] = mapped_column(Float, default=0.0)
    verify_method: Mapped[str] = mapped_column(String(16), default="not_found")

    # Flags
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    validator_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    paired_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Relation(Base):
    __tablename__ = "relations"
    __table_args__ = (UniqueConstraint("fact_a_id", "fact_b_id", name="uq_relation_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fact_a_id: Mapped[int] = mapped_column(ForeignKey("facts.id", ondelete="CASCADE"), index=True)
    fact_b_id: Mapped[int] = mapped_column(ForeignKey("facts.id", ondelete="CASCADE"), index=True)
    verdict: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16), default="final", index=True)
    method: Mapped[str] = mapped_column(String(16), default="rule")
    dimension: Mapped[str | None] = mapped_column(String(24))
    explanation: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    winner_fact_id: Mapped[int | None] = mapped_column(Integer)
    rule_hypothesis: Mapped[str | None] = mapped_column(String(255))
    llm_raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class Failure(Base):
    __tablename__ = "failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_index: Mapped[int | None] = mapped_column(Integer)
    fact_id: Mapped[int | None] = mapped_column(Integer)
    relation_id: Mapped[int | None] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    message: Mapped[str] = mapped_column(Text)
    handled: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class LlmCacheEntry(Base):
    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(16))
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
