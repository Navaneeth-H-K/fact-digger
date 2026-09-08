"""Upload flow: ticket -> bytes to storage -> finalize (inventory). Idempotent and verified."""

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fkl.api import create_app
from fkl.config import Settings
from fkl.db import make_engine
from fkl.llm.client import LLMClient, LLMRequest
from fkl.pipeline import PipelineDeps
from fkl.runtime import Runtime
from fkl.storage import LocalDirStorage


def fake_model(req: LLMRequest) -> dict[str, Any]:
    return {"page_kind": "prose", "facts": [], "problems": []}


def make_runtime(tmp_path: Path, fake: Any = fake_model) -> Runtime:
    settings = Settings(
        _env_file=None,  # tests never read the developer's .env
        database_url="sqlite://",
        storage_backend="local",
        local_storage_dir=str(tmp_path),
    )
    return Runtime(
        settings=settings,
        engine=make_engine("sqlite://"),
        storage=LocalDirStorage(tmp_path),
        deps=PipelineDeps(
            client=LLMClient(mode="off", fake=fake),
            extract_model="m",
            adjudicate_model="m",
            fiscal_year_start_month=4,
            page_concurrency=1,
            budget_s=30,
        ),
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(make_runtime(tmp_path))) as test_client:
        yield test_client


def upload(client: TestClient, pdf: bytes, filename: str = "sample.pdf") -> dict[str, Any]:
    ticket = client.post(
        "/documents",
        json={
            "filename": filename,
            "size_bytes": len(pdf),
            "sha256": hashlib.sha256(pdf).hexdigest(),
        },
    )
    assert ticket.status_code == 201, ticket.text
    body: dict[str, Any] = ticket.json()
    target = body["upload"]
    sent = client.request(
        target["method"], target["url"], files={"file": (filename, pdf, "application/pdf")}
    )
    assert sent.status_code == 204, sent.text
    return body


def test_ticket_upload_and_finalize_inventories_pages(
    client: TestClient, sample_pdf_bytes: bytes
) -> None:
    ticket = upload(client, sample_pdf_bytes)
    assert ticket["existing"] is False
    finalized = client.post(f"/documents/{ticket['document_id']}/finalize")
    assert finalized.status_code == 200, finalized.text
    document = finalized.json()
    assert document["status"] == "uploaded" and document["page_count"] == 3
    assert document["pages"] == {"pending": 2, "skipped": 1}


def test_finalize_rejects_sha_mismatch_and_keeps_document_uploading(
    client: TestClient, sample_pdf_bytes: bytes
) -> None:
    ticket = client.post(
        "/documents",
        json={"filename": "x.pdf", "size_bytes": len(sample_pdf_bytes), "sha256": "f" * 64},
    ).json()
    target = ticket["upload"]
    client.request(target["method"], target["url"], files={"file": ("x.pdf", sample_pdf_bytes)})
    finalized = client.post(f"/documents/{ticket['document_id']}/finalize")
    assert finalized.status_code == 400
    assert client.get(f"/documents/{ticket['document_id']}").json()["status"] == "uploading"


def test_finalize_is_idempotent(client: TestClient, sample_pdf_bytes: bytes) -> None:
    ticket = upload(client, sample_pdf_bytes)
    first = client.post(f"/documents/{ticket['document_id']}/finalize").json()
    second = client.post(f"/documents/{ticket['document_id']}/finalize").json()
    assert first == second


def test_same_file_twice_returns_the_existing_document(
    client: TestClient, sample_pdf_bytes: bytes
) -> None:
    ticket = upload(client, sample_pdf_bytes)
    client.post(f"/documents/{ticket['document_id']}/finalize")
    again = client.post(
        "/documents",
        json={
            "filename": "renamed.pdf",
            "size_bytes": len(sample_pdf_bytes),
            "sha256": hashlib.sha256(sample_pdf_bytes).hexdigest(),
        },
    )
    assert again.status_code == 200
    assert again.json()["existing"] is True
    assert again.json()["document_id"] == ticket["document_id"]


def test_finalize_before_bytes_arrive_is_a_client_error(client: TestClient) -> None:
    ticket = client.post(
        "/documents", json={"filename": "x.pdf", "size_bytes": 5, "sha256": "a" * 64}
    ).json()
    assert client.post(f"/documents/{ticket['document_id']}/finalize").status_code == 409


def test_list_get_and_delete_documents(client: TestClient, sample_pdf_bytes: bytes) -> None:
    ticket = upload(client, sample_pdf_bytes)
    doc_id = ticket["document_id"]
    client.post(f"/documents/{doc_id}/finalize")
    listed = client.get("/documents").json()
    assert [d["id"] for d in listed] == [doc_id]
    assert client.get(f"/documents/{doc_id}").json()["filename"] == "sample.pdf"
    assert client.delete(f"/documents/{doc_id}").status_code == 204
    assert client.get(f"/documents/{doc_id}").status_code == 404
    assert client.get("/documents").json() == []


def test_oversized_upload_is_rejected(tmp_path: Path, sample_pdf_bytes: bytes) -> None:
    runtime = make_runtime(tmp_path)
    runtime.settings.upload_max_bytes = 10
    with TestClient(create_app(runtime)) as small_client:
        response = small_client.post(
            "/documents",
            json={"filename": "big.pdf", "size_bytes": len(sample_pdf_bytes), "sha256": "b" * 64},
        )
        assert response.status_code == 413


def test_health_reports_database_and_llm_mode(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["database"] == "ok" and body["llm_mode"] == "off"


def test_root_serves_the_single_page_ui(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Fact Knowledge Layer" in response.text
    assert client.get("/static/app.js").status_code == 200


def test_llm_diagnostic_is_hidden_without_a_configured_token(client: TestClient) -> None:
    assert client.get("/diagnostics/llm", params={"token": "x"}).status_code == 404


def test_llm_diagnostic_reports_provider_outcome_when_token_matches(
    tmp_path: Path, sample_pdf_bytes: bytes
) -> None:
    from fkl.llm.client import LLMQuotaError

    def quota_gone(req: LLMRequest) -> dict[str, Any]:
        raise LLMQuotaError("model quota exhausted: 402 Budget pool quota has been exhausted")

    runtime = make_runtime(tmp_path, fake=quota_gone)
    runtime.settings.diagnostics_token = "secret-token"
    with TestClient(create_app(runtime)) as diag_client:
        assert diag_client.get("/diagnostics/llm", params={"token": "wrong"}).status_code == 403
        body = diag_client.get("/diagnostics/llm", params={"token": "secret-token"}).json()
        assert body["ok"] is False and body["error_type"] == "LLMQuotaError"
        assert "402" in body["error"] and body["model"] == "m"
        assert body["elapsed_s"] >= 0
