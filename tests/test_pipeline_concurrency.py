"""Workers must not hold write locks during model calls, and must never crash the batch."""

from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from fkl.db import make_engine, session_scope
from fkl.llm.client import LLMClient, LLMRequest
from fkl.models import Page
from fkl.pipeline import PipelineDeps, _run_time_boxed, process_document
from tests.test_pipeline import INFLATION_FACT
from tests.test_pipeline_batch import META, seed


def test_file_sqlite_engine_uses_wal_and_a_generous_busy_timeout(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'layer.db'}")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() >= 30_000


def test_no_write_lock_is_held_while_the_model_call_is_in_flight(
    tmp_path: Path, sample_pdf_bytes: bytes
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'layer.db'}")
    doc_id = seed(engine, sample_pdf_bytes, meta_done=True)
    concurrent_writes: list[bool] = []

    def model(req: LLMRequest) -> dict[str, Any]:
        if req.purpose == "meta":
            return META
        # Another worker (a second session) must be able to write while this call runs.
        with session_scope(engine) as other:
            other.execute(
                text("UPDATE documents SET title = 'touched' WHERE id = :id"), {"id": doc_id}
            )
        concurrent_writes.append(True)
        return {"page_kind": "prose", "facts": [INFLATION_FACT], "problems": []}

    deps = PipelineDeps(
        client=LLMClient(mode="off", fake=model),
        extract_model="m",
        adjudicate_model="m",
        page_concurrency=1,
    )
    progress = process_document(engine, doc_id, sample_pdf_bytes, deps)
    assert progress.done == 2 and concurrent_writes == [True, True]
    with session_scope(engine) as db:
        assert all(p.attempts == 1 for p in db.scalars(select(Page).where(Page.status == "done")))


def test_a_crashing_worker_is_reported_not_raised() -> None:
    def work(item: int) -> tuple[bool, str | None, bool]:
        if item == 2:
            raise RuntimeError("worker exploded")
        return True, None, False

    processed, errors, halted = _run_time_boxed([1, 2, 3], work, deadline=1e12, concurrency=2)
    assert processed == 2 and halted is False
    assert errors == ["worker exploded"]
