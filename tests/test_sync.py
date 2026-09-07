"""Copy a processed layer from one database/storage to another (local record run -> live site)."""

from datetime import date
from pathlib import Path

from sqlalchemy import select

from fkl.db import make_engine, session_scope
from fkl.models import Document, Fact, Failure, Page, Relation
from fkl.storage import LocalDirStorage
from fkl.sync import copy_layer
from tests.test_link import seed_documents


def test_copy_layer_moves_rows_and_files_and_is_idempotent(tmp_path: Path) -> None:
    source_engine = make_engine("sqlite://")
    seed_documents(source_engine)
    with session_scope(source_engine) as db:
        db.add(
            Relation(
                fact_a_id=1,
                fact_b_id=2,
                verdict="corroborates",
                status="final",
                method="rule",
                explanation="same",
            )
        )
        db.add(
            Failure(
                document_id="doc-es",
                page_index=0,
                stage="verify",
                kind="quote_not_found",
                message="m",
                handled="h",
            )
        )
    source_storage = LocalDirStorage(tmp_path / "src")
    source_storage.put("pdfs/doc-es.pdf", b"%PDF-es")
    source_storage.put("pdfs/doc-rbi.pdf", b"%PDF-rbi")

    target_engine = make_engine("sqlite://")
    target_storage = LocalDirStorage(tmp_path / "dst")

    summary = copy_layer(source_engine, source_storage, target_engine, target_storage)
    assert summary == {
        "documents": 2,
        "pages": 2,
        "facts": 7,
        "relations": 1,
        "failures": 1,
        "files": 2,
    }

    with session_scope(target_engine) as db:
        documents = db.scalars(select(Document).order_by(Document.id)).all()
        assert [d.id for d in documents] == ["doc-es", "doc-rbi"]
        assert documents[0].publication_date == date(2025, 1, 30)
        assert db.scalar(select(Fact.id).where(Fact.attribute == "real gdp growth")) is not None
        assert db.scalars(select(Page)).all()[0].status == "done"
        assert db.scalars(select(Relation)).one().verdict == "corroborates"
    assert target_storage.get("pdfs/doc-rbi.pdf") == b"%PDF-rbi"

    again = copy_layer(source_engine, source_storage, target_engine, target_storage)
    assert again["documents"] == 2
    with session_scope(target_engine) as db:
        assert len(db.scalars(select(Fact)).all()) == 7  # replaced, not duplicated
