"""Linking endpoint and the read endpoints for facts, relations, failures and the schema."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fkl.api import create_app
from tests.test_api_upload import make_runtime
from tests.test_link import Adjudicator, seed_documents


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    runtime = make_runtime(tmp_path, fake=Adjudicator(fail_times=1))
    seed_documents(runtime.engine)
    with TestClient(create_app(runtime)) as test_client:
        yield test_client


def linked(client: TestClient) -> dict[str, Any]:
    first = client.post("/link").json()
    second = client.post("/link").json()  # the adjudicator fails once, then succeeds
    assert first["failed"] == 0 and second["pending_llm"] == 0
    return second


def test_link_endpoint_reports_progress(client: TestClient) -> None:
    progress = linked(client)
    assert progress["total_relations"] == 3 and progress["final"] == 3
    assert progress["by_verdict"] == {"contradicts": 1, "corroborates": 1, "superseded": 1}


def test_relations_embed_both_facts_and_filter_by_verdict_status_document(
    client: TestClient,
) -> None:
    linked(client)
    everything = client.get("/relations").json()
    assert len(everything) == 3
    contradiction = client.get("/relations", params={"verdict": "contradicts"}).json()
    assert len(contradiction) == 1
    relation = contradiction[0]
    assert relation["fact_a"]["document_filename"] == "doc-es.pdf"
    assert relation["fact_b"]["document_filename"] == "doc-rbi.pdf"
    assert relation["method"] == "llm" and "8.8" in relation["explanation"]
    assert len(client.get("/relations", params={"status": "final"}).json()) == 3
    assert len(client.get("/relations", params={"document_id": "doc-es"}).json()) == 3
    assert client.get("/relations", params={"document_id": "nope"}).json() == []


def test_facts_listing_filters_and_paginates(client: TestClient) -> None:
    linked(client)
    assert len(client.get("/facts").json()) == 7
    assert len(client.get("/facts", params={"document_id": "doc-rbi"}).json()) == 3
    assert len(client.get("/facts", params={"q": "deficit"}).json()) == 2
    assert len(client.get("/facts", params={"attribute": "gdp"}).json()) == 2
    page = client.get("/facts", params={"limit": 2, "offset": 5}).json()
    assert len(page) == 2
    fact_id = client.get("/facts", params={"q": "deficit"}).json()[0]["id"]
    detail = client.get(f"/facts/{fact_id}").json()
    assert detail["attribute"] == "general government deficit"
    assert len(detail["relations"]) == 1 and detail["relations"][0]["verdict"] == "contradicts"
    assert client.get("/facts/999999").status_code == 404


def test_failures_listing_filters_by_stage_and_kind(client: TestClient) -> None:
    linked(client)
    failures = client.get("/failures", params={"stage": "link"}).json()
    assert len(failures) == 1 and failures[0]["kind"] == "adjudicate_failed"
    assert failures[0]["handled"].startswith("attempt 1")
    assert client.get("/failures", params={"kind": "llm_quota"}).json() == []


def test_schema_aggregates_discovered_attributes(client: TestClient) -> None:
    linked(client)
    schema = client.get("/schema").json()
    keys = [entry["attribute_key"] for entry in schema]
    assert keys[0] in {"cpi headline inflation", "gdp growth real", "deficit general government"}
    entry = next(e for e in schema if e["attribute_key"] == "cpi headline inflation")
    assert entry["count"] == 2 and entry["units"] == ["percent"]
    assert entry["entities"] == ["India"] and entry["estimate_types"] == ["actual"]
    assert entry["display_name"] == "cpi headline inflation"


def test_stats_endpoint_returns_layer_counts(client: TestClient) -> None:
    linked(client)
    s = client.get("/stats").json()
    assert s["documents"] == 2 and s["facts"] == 7 and s["relations"] == 3
    assert s["by_verdict"] == {"contradicts": 1, "corroborates": 1, "superseded": 1}
    assert s["failures"] >= 1 and isinstance(s["by_failure_kind"], dict)
    assert s["attributes"] >= 1 and s["pages"] == 2
