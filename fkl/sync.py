"""Copy a processed layer between databases and storages, keeping ids intact.

Used to seed the live site from a local record run so the same facts, relations and failures
are visible there without spending model quota a second time.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, delete, select, text
from sqlalchemy.orm import Session

from fkl.db import session_scope
from fkl.models import Base, Document, Fact, Failure, Page, Relation
from fkl.storage import Storage

_SEQUENCED_TABLES = ("pages", "facts", "relations", "failures")


def _row_values(instance: Base) -> dict[str, Any]:
    return {column.name: getattr(instance, column.name) for column in instance.__table__.columns}


def _copy_rows(source: Session, target: Session, model: type[Base], condition: Any) -> int:
    rows = source.scalars(select(model).where(condition)).all()
    for row in rows:
        target.add(model(**_row_values(row)))
    target.flush()
    return len(rows)


def _reset_sequences(engine: Engine) -> None:
    """After inserting explicit ids into Postgres, move the serial sequences past them."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as connection:
        for table in _SEQUENCED_TABLES:
            connection.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
                )
            )


def copy_layer(
    source_engine: Engine,
    source_storage: Storage,
    target_engine: Engine,
    target_storage: Storage,
) -> dict[str, int]:
    """Replace every source document (and everything hanging off it) in the target."""
    summary = {"documents": 0, "pages": 0, "facts": 0, "relations": 0, "failures": 0, "files": 0}
    with session_scope(source_engine) as source, session_scope(target_engine) as target:
        document_ids = list(source.scalars(select(Document.id).order_by(Document.created_at)))
        if not document_ids:
            return summary
        target.execute(delete(Document).where(Document.id.in_(document_ids)))
        target.flush()
        in_docs = Document.id.in_(document_ids)
        summary["documents"] = _copy_rows(source, target, Document, in_docs)
        summary["pages"] = _copy_rows(source, target, Page, Page.document_id.in_(document_ids))
        summary["facts"] = _copy_rows(source, target, Fact, Fact.document_id.in_(document_ids))
        fact_ids = select(Fact.id).where(Fact.document_id.in_(document_ids))
        summary["relations"] = _copy_rows(
            source, target, Relation, Relation.fact_a_id.in_(fact_ids)
        )
        summary["failures"] = _copy_rows(
            source, target, Failure, Failure.document_id.in_(document_ids)
        )
        for path in source.scalars(select(Document.storage_path).where(in_docs)):
            if source_storage.exists(path):
                target_storage.put(path, source_storage.get(path))
                summary["files"] += 1
    _reset_sequences(target_engine)
    return summary
