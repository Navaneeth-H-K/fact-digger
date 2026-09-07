"""Processing endpoint drives the time-boxed loop; page routes expose text, facts and images."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fkl.api import create_app
from fkl.llm.client import LLMRequest
from tests.test_api_upload import make_runtime, upload
from tests.test_pipeline import INFLATION_FACT, REVENUE_FACT
from tests.test_pipeline_batch import META


def scripted_model(req: LLMRequest) -> dict[str, Any]:
    if req.purpose == "meta":
        return META
    return {"page_kind": "prose", "facts": [REVENUE_FACT, INFLATION_FACT], "problems": []}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(make_runtime(tmp_path, fake=scripted_model))) as test_client:
        yield test_client


def uploaded_document(client: TestClient, pdf: bytes) -> str:
    ticket = upload(client, pdf)
    assert client.post(f"/documents/{ticket['document_id']}/finalize").status_code == 200
    document_id: str = ticket["document_id"]
    return document_id


def test_process_runs_pages_until_done_and_is_safe_to_repeat(
    client: TestClient, sample_pdf_bytes: bytes
) -> None:
    doc_id = uploaded_document(client, sample_pdf_bytes)
    first = client.post(f"/documents/{doc_id}/process", params={"budget_s": 30})
    assert first.status_code == 200, first.text
    progress = first.json()
    assert (progress["status"], progress["done"], progress["skipped"], progress["pending"]) == (
        "extracted",
        2,
        1,
        0,
    )
    assert progress["processed_this_call"] == 2 and progress["estimated_calls_remaining"] == 0

    again = client.post(f"/documents/{doc_id}/process").json()
    assert again["processed_this_call"] == 0 and again["status"] == "extracted"

    document = client.get(f"/documents/{doc_id}").json()
    assert document["meta_done"] is True and document["publication_date"] == "2024-07-05"
    assert document["facts_count"] == 2  # page 3 repeats page 1: marked duplicate, not counted


def test_process_requires_a_finalized_document(client: TestClient, sample_pdf_bytes: bytes) -> None:
    ticket = upload(client, sample_pdf_bytes)
    assert client.post(f"/documents/{ticket['document_id']}/process").status_code == 409
    assert client.post("/documents/nope/process").status_code == 404


def test_page_route_returns_text_status_and_facts(
    client: TestClient, sample_pdf_bytes: bytes
) -> None:
    doc_id = uploaded_document(client, sample_pdf_bytes)
    client.post(f"/documents/{doc_id}/process")
    page = client.get(f"/documents/{doc_id}/pages/0").json()
    assert page["index"] == 0 and page["label"] == "212" and page["status"] == "done"
    assert "81,415.38" in page["text"]
    attributes = {fact["attribute"] for fact in page["facts"]}
    assert attributes == {"Revenue from contracts with customers", "Retail headline inflation"}
    revenue = next(f for f in page["facts"] if f["attribute"].startswith("Revenue"))
    assert revenue["evidence_verified"] is True and revenue["unit"] == "INR"
    assert revenue["period_key"] == "2023-04-01:2024-03-31"
    assert client.get(f"/documents/{doc_id}/pages/99").status_code == 404


def test_page_image_is_rendered_as_jpeg(client: TestClient, sample_pdf_bytes: bytes) -> None:
    doc_id = uploaded_document(client, sample_pdf_bytes)
    response = client.get(f"/documents/{doc_id}/pages/0/image.jpg", params={"width": 400})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content[:2] == b"\xff\xd8"
