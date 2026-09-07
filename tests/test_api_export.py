"""Export of the whole layer (for committed samples) and the per-fact vintage timeline."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fkl.api import create_app
from tests.test_api_upload import make_runtime
from tests.test_link import Adjudicator, seed_documents


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    runtime = make_runtime(tmp_path, fake=Adjudicator())
    seed_documents(runtime.engine)
    with TestClient(create_app(runtime)) as test_client:
        test_client.post("/link")
        yield test_client


def test_export_contains_every_layer_and_is_gzipped_when_accepted(client: TestClient) -> None:
    response = client.get("/export", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    body = response.json()
    assert set(body) >= {
        "generated_at",
        "documents",
        "pages",
        "facts",
        "relations",
        "failures",
        "schema",
    }
    assert len(body["documents"]) == 2 and len(body["facts"]) == 7
    assert len(body["relations"]) == 3 and body["pages"][0].keys() >= {
        "document_id",
        "index",
        "status",
    }
    assert body["relations"][0]["fact_a"]["document_filename"].endswith(".pdf")


def test_export_can_be_scoped_to_one_document(client: TestClient) -> None:
    body = client.get("/export", params={"document_id": "doc-rbi"}).json()
    assert [d["id"] for d in body["documents"]] == ["doc-rbi"]
    assert all(f["document_id"] == "doc-rbi" for f in body["facts"])
    assert len(body["relations"]) == 3  # relations touching the document are kept


def test_timeline_orders_vintages_and_marks_the_current_value(client: TestClient) -> None:
    timeline = client.get(
        "/facts/timeline", params={"entity_key": "india", "attribute_key": "gdp growth real"}
    ).json()
    assert timeline["entity_key"] == "india"
    entries = timeline["entries"]
    assert [e["fact"]["value_num"] for e in entries] == [6.4, 6.5]
    assert [e["publication_date"] for e in entries] == ["2025-01-30", "2025-05-29"]
    assert [e["is_current"] for e in entries] == [False, True]
    assert entries[0]["superseded_by"] == entries[1]["fact"]["id"]
    assert entries[1]["superseded_by"] is None
    assert entries[0]["period_key"] == entries[1]["period_key"]


def test_timeline_for_unknown_key_is_empty(client: TestClient) -> None:
    timeline = client.get(
        "/facts/timeline", params={"entity_key": "mars", "attribute_key": "x"}
    ).json()
    assert timeline["entries"] == []
