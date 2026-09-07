"""Per-page extraction: model output is verified, normalised, de-duplicated and persisted."""

from datetime import date
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from fkl.db import make_engine, session_scope
from fkl.llm.client import LLMClient, LLMRequest
from fkl.models import Document, Fact, Failure, Page
from fkl.pdf import inventory
from fkl.pipeline import PipelineDeps, process_page

REVENUE_FACT: dict[str, Any] = {
    "entity": "Delhivery Limited",
    "attribute": "Revenue from contracts with customers",
    "value": "81,415.38",
    "value_kind": "number",
    "unit": "₹",
    "scale": "million",
    "period": "FY24",
    "period_start": None,
    "period_end": None,
    "estimate_type": "actual",
    "measurement_basis": "consolidated",
    "attributed_to": None,
    "quote": "Revenue from contracts with customers 21 81,415.38",
    "quote_source": "text",
    "confidence": 0.9,
    "notes": None,
}
INFLATION_FACT: dict[str, Any] = {
    **REVENUE_FACT,
    "entity": "India",
    "attribute": "Retail headline inflation",
    "value": "5.4",
    "unit": "per cent",
    "scale": None,
    "measurement_basis": None,
    "quote": "softened from 5.4 per cent in FY24",
}
IMAGE_FACT: dict[str, Any] = {
    **REVENUE_FACT,
    "attribute": "Adjusted EBITDA",
    "value": "758",
    "quote": "Adjusted EBITDA 758",
    "quote_source": "image",
}
DUPLICATE_FACT: dict[str, Any] = {**REVENUE_FACT, "quote": "customers 21 81,415.38 72,253.01"}
MALFORMED_FACT: dict[str, Any] = {"entity": "Nobody"}


def canned_extraction(req: LLMRequest) -> dict[str, Any]:
    assert req.purpose == "extract"
    assert req.content[0]["type"] == "image" and req.content[0]["data"][:2] == b"\xff\xd8"
    text_block = req.content[-1]["text"]
    assert "81,415.38" in text_block and "reuse" in text_block.lower()
    return {
        "page_kind": "mixed",
        "facts": [REVENUE_FACT, INFLATION_FACT, IMAGE_FACT, DUPLICATE_FACT, MALFORMED_FACT],
        "problems": ["rupee glyph missing from text layer"],
    }


@pytest.fixture
def deps() -> PipelineDeps:
    return PipelineDeps(
        client=LLMClient(mode="off", fake=canned_extraction),
        extract_model="fake-extract",
        adjudicate_model="fake-adjudicate",
        fiscal_year_start_month=4,
    )


def seed_document(db: Session, pdf_bytes: bytes) -> Document:
    document = Document(
        id="doc-a",
        filename="a.pdf",
        size_bytes=len(pdf_bytes),
        sha256="0" * 64,
        storage_path="pdfs/doc-a.pdf",
        status="uploaded",
        publication_date=date(2024, 7, 5),
        fiscal_year_start_month=4,
        meta_done=True,
    )
    db.add(document)
    for info in inventory(pdf_bytes):
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
    db.flush()
    return document


def test_process_page_verifies_normalises_and_persists_facts(
    sample_pdf_bytes: bytes, deps: PipelineDeps
) -> None:
    engine = make_engine("sqlite://")
    with session_scope(engine) as db:
        document = seed_document(db, sample_pdf_bytes)
        page = db.scalars(select(Page).where(Page.index == 0)).one()
        stored = process_page(db, document, page, sample_pdf_bytes, deps)
        assert stored == 4

    with session_scope(engine) as db:
        facts = db.scalars(select(Fact).order_by(Fact.id)).all()
        revenue, inflation, image, duplicate = facts
        assert revenue.value_num == pytest.approx(81_415_380_000.0)
        assert (revenue.unit, revenue.scale, revenue.period_key) == (
            "INR",
            "million",
            "2023-04-01:2024-03-31",
        )
        assert revenue.period_kind == "fy" and revenue.period_start == date(2023, 4, 1)
        assert (revenue.evidence_verified, revenue.verify_method) == (True, "exact")
        assert revenue.entity_key == "delhivery limited"
        assert revenue.attribute_key == "contract customer revenue with"
        assert revenue.basis_key == "consolidated" and revenue.page_label == "212"

        assert inflation.unit == "percent" and inflation.value_num == pytest.approx(5.4)
        assert inflation.evidence_verified and inflation.verify_method == "exact"

        assert image.verify_method == "image_only" and image.evidence_verified is False
        assert duplicate.is_duplicate is True and revenue.is_duplicate is False
        assert duplicate.fact_key == revenue.fact_key

        page = db.scalars(select(Page).where(Page.index == 0)).one()
        assert page.status == "done" and page.facts_count == 4 and page.attempts == 1

        failures = db.scalars(select(Failure).order_by(Failure.id)).all()
        kinds = sorted(f.kind for f in failures)
        assert kinds == ["image_only_evidence", "llm_json"]
        assert all(f.handled for f in failures)


def test_process_page_marks_failure_when_model_raises(
    sample_pdf_bytes: bytes, deps: PipelineDeps
) -> None:
    def broken(req: LLMRequest) -> dict[str, Any]:
        raise RuntimeError("proxy down")

    deps = PipelineDeps(
        client=LLMClient(mode="off", fake=broken),
        extract_model="m",
        adjudicate_model="m",
        fiscal_year_start_month=4,
    )
    engine = make_engine("sqlite://")
    with session_scope(engine) as db:
        document = seed_document(db, sample_pdf_bytes)
        page = db.scalars(select(Page).where(Page.index == 0)).one()
        assert process_page(db, document, page, sample_pdf_bytes, deps) == 0
        assert page.status == "failed" and page.attempts == 1
        assert "proxy down" in (page.last_error or "")
        failure = db.scalars(select(Failure)).one()
        assert (failure.stage, failure.kind) == ("extract", "llm_other")
