"""HTTP API. Thin: routes validate input, open a session, call the pipeline, shape the output."""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session, aliased

from fkl.db import session_scope
from fkl.models import Document, Fact, Failure, Page, Relation
from fkl.pdf import inventory, render_jpeg
from fkl.pipeline import link, process_document
from fkl.runtime import Runtime, build_runtime
from fkl.schemas import (
    DocumentOut,
    ExportOut,
    FactDetailOut,
    FactOut,
    FailureOut,
    LinkProgressOut,
    PageOut,
    ProgressOut,
    RelationOut,
    SchemaEntry,
    TimelineEntry,
    TimelineOut,
    UploadRequest,
    UploadTargetOut,
    UploadTicket,
)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(runtime: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = runtime or build_runtime()
        yield

    app = FastAPI(title="Fact Knowledge Layer", version="0.1.0", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    _register_routes(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def _db(runtime: Annotated[Runtime, Depends(_runtime)]) -> Iterator[Session]:
    with session_scope(runtime.engine) as session:
        yield session


RuntimeDep = Annotated[Runtime, Depends(_runtime)]
DbDep = Annotated[Session, Depends(_db)]


def _document_out(db: Session, document: Document) -> DocumentOut:
    page_rows = db.execute(
        select(Page.status, func.count())
        .where(Page.document_id == document.id)
        .group_by(Page.status)
    ).all()
    facts_count = db.scalar(
        select(func.count()).where(Fact.document_id == document.id, Fact.is_duplicate.is_(False))
    )
    scalar_fields = {
        name: getattr(document, name)
        for name in DocumentOut.model_fields
        if name not in ("pages", "facts_count")
    }
    return DocumentOut(
        **scalar_fields,
        pages={status: count for status, count in page_rows},
        facts_count=facts_count or 0,
    )


_SEVERITY = (
    "contradicts",
    "unresolved",
    "superseded",
    "context_explained",
    "corroborates",
    "derived",
)


def _severity_order() -> Any:
    return case(
        {verdict: rank for rank, verdict in enumerate(_SEVERITY)},
        value=Relation.verdict,
        else_=len(_SEVERITY),
    )


def _fact_out(fact: Fact, filename: str | None) -> FactOut:
    out = FactOut.model_validate(fact)
    out.document_filename = filename
    return out


def _relation_out(db: Session, relation: Relation) -> RelationOut:
    fact_a, fact_b = db.get(Fact, relation.fact_a_id), db.get(Fact, relation.fact_b_id)
    assert fact_a is not None and fact_b is not None
    document_a, document_b = (
        db.get(Document, fact_a.document_id),
        db.get(Document, fact_b.document_id),
    )
    scalar_fields = {
        name: getattr(relation, name)
        for name in RelationOut.model_fields
        if name not in ("fact_a", "fact_b")
    }
    return RelationOut(
        **scalar_fields,
        fact_a=_fact_out(fact_a, document_a.filename if document_a else None),
        fact_b=_fact_out(fact_b, document_b.filename if document_b else None),
    )


def _get_document(db: Session, document_id: str) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return document


def _register_routes(app: FastAPI) -> None:
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/health")
    def health(runtime: RuntimeDep, db: DbDep) -> dict[str, str]:
        try:
            db.execute(text("SELECT 1"))
            database = "ok"
        except Exception as error:  # noqa: BLE001 - health must never raise
            database = f"error: {error}"
        return {
            "database": database,
            "llm_mode": runtime.deps.client.mode,
            "storage": runtime.settings.storage_backend,
        }

    @app.post("/documents", response_model=UploadTicket, status_code=201)
    def create_document(
        payload: UploadRequest, runtime: RuntimeDep, db: DbDep, response: Response
    ) -> UploadTicket:
        if payload.size_bytes > runtime.settings.upload_max_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        existing = db.scalars(
            select(Document).where(Document.sha256 == payload.sha256.lower())
        ).first()
        if existing is not None and existing.status != "uploading":
            response.status_code = 200
            return UploadTicket(document_id=existing.id, existing=True, upload=None)
        document = existing
        if document is None:
            document_id = uuid.uuid4().hex
            document = Document(
                id=document_id,
                filename=payload.filename,
                size_bytes=payload.size_bytes,
                sha256=payload.sha256.lower(),
                storage_path=f"pdfs/{document_id}.pdf",
            )
            db.add(document)
            db.flush()
        target = runtime.storage.create_upload_target(document.storage_path)
        return UploadTicket(
            document_id=document.id,
            existing=False,
            upload=UploadTargetOut(url=target.url, method=target.method, token=target.token),
        )

    @app.post("/documents/upload", status_code=204)
    async def upload_bytes(
        file: UploadFile, runtime: RuntimeDep, db: DbDep, path: str = Query(min_length=1)
    ) -> Response:
        document = db.scalars(select(Document).where(Document.storage_path == path)).first()
        if document is None or document.status != "uploading":
            raise HTTPException(status_code=404, detail="no pending upload for this path")
        data = await file.read()
        if len(data) > runtime.settings.upload_max_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        runtime.storage.put(path, data)
        return Response(status_code=204)

    @app.post("/documents/{document_id}/finalize", response_model=DocumentOut)
    def finalize_document(document_id: str, runtime: RuntimeDep, db: DbDep) -> DocumentOut:
        document = _get_document(db, document_id)
        if document.status != "uploading":
            return _document_out(db, document)
        if not runtime.storage.exists(document.storage_path):
            raise HTTPException(status_code=409, detail="file bytes have not arrived yet")
        data = runtime.storage.get(document.storage_path)
        if len(data) != document.size_bytes or hashlib.sha256(data).hexdigest() != document.sha256:
            raise HTTPException(
                status_code=400, detail="uploaded bytes do not match size or sha256"
            )
        pages = inventory(data)
        for info in pages:
            db.add(
                Page(
                    document_id=document.id,
                    index=info.index,
                    label=info.label,
                    text=info.text,
                    char_count=info.char_count,
                    status="skipped" if info.is_blank else "pending",
                )
            )
        document.page_count = len(pages)
        document.status = "uploaded"
        db.flush()
        return _document_out(db, document)

    @app.get("/documents", response_model=list[DocumentOut])
    def list_documents(db: DbDep) -> list[DocumentOut]:
        documents = db.scalars(select(Document).order_by(Document.created_at)).all()
        return [_document_out(db, document) for document in documents]

    @app.get("/documents/{document_id}", response_model=DocumentOut)
    def get_document(document_id: str, db: DbDep) -> DocumentOut:
        return _document_out(db, _get_document(db, document_id))

    @app.delete("/documents/{document_id}", status_code=204)
    def delete_document(document_id: str, db: DbDep) -> Response:
        db.delete(_get_document(db, document_id))
        return Response(status_code=204)

    @app.post("/documents/{document_id}/process", response_model=ProgressOut)
    def process(
        document_id: str,
        runtime: RuntimeDep,
        db: DbDep,
        budget_s: float | None = Query(default=None, gt=0, le=290),
        retry_failed: bool = False,
    ) -> ProgressOut:
        document = _get_document(db, document_id)
        if document.status == "uploading":
            raise HTTPException(status_code=409, detail="finalize the upload first")
        pdf_bytes = runtime.storage.get(document.storage_path)
        db.close()  # the batch runner opens its own sessions, one per page
        deps = runtime.deps
        if budget_s is not None:
            deps = replace(deps, budget_s=budget_s)
        progress = process_document(
            runtime.engine, document_id, pdf_bytes, deps, retry_failed=retry_failed
        )
        return ProgressOut.model_validate(progress)

    @app.get("/documents/{document_id}/pages/{index}", response_model=PageOut)
    def get_page(document_id: str, index: int, db: DbDep) -> PageOut:
        page = db.scalars(
            select(Page).where(Page.document_id == document_id, Page.index == index)
        ).first()
        if page is None:
            raise HTTPException(status_code=404, detail="page not found")
        facts = db.scalars(select(Fact).where(Fact.page_id == page.id).order_by(Fact.id)).all()
        out = PageOut.model_validate(page)
        out.facts = [FactOut.model_validate(fact) for fact in facts]
        return out

    @app.get("/documents/{document_id}/pages/{index}/image.jpg")
    def get_page_image(
        document_id: str,
        index: int,
        runtime: RuntimeDep,
        db: DbDep,
        width: int = Query(default=1200, ge=200, le=2400),
    ) -> Response:
        document = _get_document(db, document_id)
        if document.page_count is None or not 0 <= index < document.page_count:
            raise HTTPException(status_code=404, detail="page not found")
        data = render_jpeg(runtime.storage.get(document.storage_path), index, width=width)
        return Response(content=data, media_type="image/jpeg")

    @app.post("/link", response_model=LinkProgressOut)
    def link_documents(
        runtime: RuntimeDep, db: DbDep, budget_s: float | None = Query(default=None, gt=0, le=290)
    ) -> LinkProgressOut:
        db.close()
        deps = runtime.deps if budget_s is None else replace(runtime.deps, budget_s=budget_s)
        return LinkProgressOut.model_validate(link(runtime.engine, deps))

    @app.get("/relations", response_model=list[RelationOut])
    def list_relations(
        db: DbDep,
        verdict: str | None = None,
        status: str | None = None,
        document_id: str | None = None,
        attribute: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[RelationOut]:
        fact_a = aliased(Fact)
        fact_b = aliased(Fact)
        query = (
            select(Relation)
            .join(fact_a, Relation.fact_a_id == fact_a.id)
            .join(fact_b, Relation.fact_b_id == fact_b.id)
        )
        if verdict:
            query = query.where(Relation.verdict == verdict)
        if status:
            query = query.where(Relation.status == status)
        if document_id:
            query = query.where(
                or_(fact_a.document_id == document_id, fact_b.document_id == document_id)
            )
        if attribute:
            pattern = f"%{attribute.lower()}%"
            query = query.where(
                or_(fact_a.attribute_key.like(pattern), fact_b.attribute_key.like(pattern))
            )
        query = query.order_by(_severity_order(), Relation.confidence.desc(), Relation.id)
        relations = db.scalars(query.limit(limit).offset(offset)).all()
        return [_relation_out(db, relation) for relation in relations]

    @app.get("/facts", response_model=list[FactOut])
    def list_facts(
        db: DbDep,
        document_id: str | None = None,
        attribute: str | None = None,
        entity: str | None = None,
        verified: bool | None = None,
        q: str | None = None,
        include_duplicates: bool = False,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[FactOut]:
        query = select(Fact, Document.filename).join(Document, Fact.document_id == Document.id)
        if not include_duplicates:
            query = query.where(Fact.is_duplicate.is_(False))
        if document_id:
            query = query.where(Fact.document_id == document_id)
        if attribute:
            query = query.where(Fact.attribute_key.like(f"%{attribute.lower()}%"))
        if entity:
            query = query.where(Fact.entity_key.like(f"%{entity.lower()}%"))
        if verified is not None:
            query = query.where(Fact.evidence_verified.is_(verified))
        if q:
            pattern = f"%{q}%"
            query = query.where(
                or_(
                    Fact.entity.ilike(pattern),
                    Fact.attribute.ilike(pattern),
                    Fact.quote.ilike(pattern),
                )
            )
        rows = db.execute(query.order_by(Fact.id).limit(limit).offset(offset)).all()
        return [_fact_out(fact, filename) for fact, filename in rows]

    @app.get("/facts/timeline", response_model=TimelineOut)
    def fact_timeline(db: DbDep, entity_key: str, attribute_key: str) -> TimelineOut:
        rows = sorted(
            db.execute(
                select(Fact, Document)
                .join(Document, Fact.document_id == Document.id)
                .where(
                    Fact.entity_key == entity_key,
                    Fact.attribute_key == attribute_key,
                    Fact.is_duplicate.is_(False),
                )
            ).all(),
            key=lambda row: (row[0].period_key, row[1].publication_date or date.min, row[0].id),
        )
        latest_per_period: dict[str, int] = {}
        for fact, _document in rows:
            latest_per_period[fact.period_key] = fact.id  # sorted ascending, so the last wins
        winners = {
            loser: winner
            for loser, winner in db.execute(
                select(
                    case(
                        (Relation.winner_fact_id == Relation.fact_a_id, Relation.fact_b_id),
                        else_=Relation.fact_a_id,
                    ),
                    Relation.winner_fact_id,
                ).where(
                    Relation.verdict == "superseded",
                    Relation.winner_fact_id.is_not(None),
                    or_(
                        Relation.fact_a_id.in_([f.id for f, _ in rows]),
                        Relation.fact_b_id.in_([f.id for f, _ in rows]),
                    ),
                )
            ).all()
        }
        entries = []
        for fact, document in rows:
            current = latest_per_period[fact.period_key] == fact.id
            superseded_by = (
                None if current else winners.get(fact.id, latest_per_period[fact.period_key])
            )
            entries.append(
                TimelineEntry(
                    fact=_fact_out(fact, document.filename),
                    period_key=fact.period_key,
                    publication_date=document.publication_date,
                    document_title=document.title,
                    is_current=current,
                    superseded_by=superseded_by,
                )
            )
        return TimelineOut(entity_key=entity_key, attribute_key=attribute_key, entries=entries)

    @app.get("/facts/{fact_id}", response_model=FactDetailOut)
    def get_fact(fact_id: int, db: DbDep) -> FactDetailOut:
        fact = db.get(Fact, fact_id)
        if fact is None:
            raise HTTPException(status_code=404, detail="fact not found")
        document = db.get(Document, fact.document_id)
        detail = FactDetailOut.model_validate(fact)
        detail.document_filename = document.filename if document else None
        relations = db.scalars(
            select(Relation)
            .where(or_(Relation.fact_a_id == fact_id, Relation.fact_b_id == fact_id))
            .order_by(_severity_order(), Relation.id)
        ).all()
        detail.relations = [_relation_out(db, relation) for relation in relations]
        return detail

    @app.get("/failures", response_model=list[FailureOut])
    def list_failures(
        db: DbDep,
        stage: str | None = None,
        kind: str | None = None,
        document_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> list[FailureOut]:
        query = select(Failure)
        if stage:
            query = query.where(Failure.stage == stage)
        if kind:
            query = query.where(Failure.kind == kind)
        if document_id:
            query = query.where(Failure.document_id == document_id)
        failures = db.scalars(query.order_by(Failure.id.desc()).limit(limit)).all()
        return [FailureOut.model_validate(failure) for failure in failures]

    @app.get("/schema", response_model=list[SchemaEntry])
    def get_schema(db: DbDep) -> list[SchemaEntry]:
        rows = db.execute(
            select(
                Fact.attribute_key, Fact.attribute, Fact.unit, Fact.entity, Fact.estimate_type
            ).where(Fact.is_duplicate.is_(False))
        ).all()
        groups: dict[str, list[tuple[str, str, str, str]]] = {}
        for key, attribute, unit, entity, estimate_type in rows:
            groups.setdefault(key, []).append((attribute, unit, entity, estimate_type))
        entries = []
        for key, members in groups.items():
            names = Counter(attribute for attribute, *_ in members)
            entries.append(
                SchemaEntry(
                    attribute_key=key,
                    display_name=names.most_common(1)[0][0],
                    count=len(members),
                    units=sorted({unit for _, unit, *_ in members}),
                    entities=sorted({entity for *_, entity, _ in members})[:10],
                    estimate_types=sorted({estimate for *_, estimate in members}),
                )
            )
        return sorted(entries, key=lambda entry: (-entry.count, entry.attribute_key))

    @app.get("/export", response_model=ExportOut)
    def export_layer(db: DbDep, document_id: str | None = None) -> ExportOut:
        documents = db.scalars(select(Document).order_by(Document.created_at)).all()
        if document_id:
            documents = [d for d in documents if d.id == document_id]
        ids = [d.id for d in documents]
        pages = db.execute(
            select(Page.document_id, Page.index, Page.label, Page.status, Page.facts_count)
            .where(Page.document_id.in_(ids))
            .order_by(Page.document_id, Page.index)
        ).all()
        fact_rows = db.execute(
            select(Fact, Document.filename)
            .join(Document, Fact.document_id == Document.id)
            .where(Fact.document_id.in_(ids))
            .order_by(Fact.id)
        ).all()
        fact_a, fact_b = aliased(Fact), aliased(Fact)
        relations = db.scalars(
            select(Relation)
            .join(fact_a, Relation.fact_a_id == fact_a.id)
            .join(fact_b, Relation.fact_b_id == fact_b.id)
            .where(or_(fact_a.document_id.in_(ids), fact_b.document_id.in_(ids)))
            .order_by(_severity_order(), Relation.id)
        ).all()
        failures = db.scalars(
            select(Failure)
            .where(or_(Failure.document_id.in_(ids), Failure.document_id.is_(None)))
            .order_by(Failure.id)
        ).all()
        return ExportOut(
            generated_at=datetime.now(UTC),
            documents=[_document_out(db, d) for d in documents],
            pages=[
                {
                    "document_id": doc,
                    "index": index,
                    "label": label,
                    "status": status,
                    "facts_count": count,
                }
                for doc, index, label, status, count in pages
            ],
            facts=[_fact_out(fact, filename) for fact, filename in fact_rows],
            relations=[_relation_out(db, relation) for relation in relations],
            failures=[FailureOut.model_validate(failure) for failure in failures],
            attribute_schema=get_schema(db),
        )


app = create_app()
