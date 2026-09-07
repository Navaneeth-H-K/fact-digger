"""HTTP API. Thin: routes validate input, open a session, call the pipeline, shape the output."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from fkl.db import session_scope
from fkl.models import Document, Fact, Page
from fkl.pdf import inventory, render_jpeg
from fkl.pipeline import process_document
from fkl.runtime import Runtime, build_runtime
from fkl.schemas import (
    DocumentOut,
    FactOut,
    PageOut,
    ProgressOut,
    UploadRequest,
    UploadTargetOut,
    UploadTicket,
)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = runtime or build_runtime()
        yield

    app = FastAPI(title="Fact Knowledge Layer", version="0.1.0", lifespan=lifespan)
    _register_routes(app)
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


def _get_document(db: Session, document_id: str) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return document


def _register_routes(app: FastAPI) -> None:
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


app = create_app()
