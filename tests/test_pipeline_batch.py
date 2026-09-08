"""Time-boxed, idempotent, resumable document processing (the loop the API and UI drive)."""

import time
from datetime import date
from typing import Any

from sqlalchemy import select

from fkl.db import make_engine, session_scope
from fkl.llm.client import LLMClient, LLMRequest
from fkl.models import Document, Failure, Page
from fkl.pipeline import MAX_PAGE_ATTEMPTS, PipelineDeps, process_document
from tests.test_pipeline import INFLATION_FACT, seed_document

META: dict[str, Any] = {
    "title": "Annual Report 2023-24",
    "publisher": "Delhivery Limited",
    "publication_date": "2024-07-05",
    "document_type": "annual report",
    "fiscal_year_start_month": 4,
}


class Fake:
    """Scriptable fake model: optional per-page failures and an optional delay per call."""

    def __init__(self, fail_pages: set[int] | None = None, delay: float = 0.0) -> None:
        self.fail_pages = fail_pages or set()
        self.delay = delay
        self.calls: list[str] = []

    def __call__(self, req: LLMRequest) -> dict[str, Any]:
        self.calls.append(req.purpose)
        time.sleep(self.delay)
        if req.purpose == "meta":
            return META
        page_index = int(req.content[-1]["text"].split("Page index ")[1].split(",")[0])
        if page_index in self.fail_pages:
            self.fail_pages.discard(page_index)  # fail once, then succeed
            raise RuntimeError(f"page {page_index} exploded")
        return {"page_kind": "prose", "facts": [INFLATION_FACT], "problems": []}


def make_deps(fake: Fake, budget_s: float = 30.0) -> PipelineDeps:
    return PipelineDeps(
        client=LLMClient(mode="off", fake=fake),
        extract_model="m",
        adjudicate_model="m",
        fiscal_year_start_month=4,
        page_concurrency=1,
        budget_s=budget_s,
    )


def seed(engine: Any, pdf_bytes: bytes, meta_done: bool = False) -> str:
    with session_scope(engine) as db:
        document = seed_document(db, pdf_bytes)
        document.meta_done = meta_done
        document.publication_date = None if not meta_done else date(2024, 7, 5)
        return document.id


def test_processes_all_pages_records_meta_and_marks_document_extracted(
    sample_pdf_bytes: bytes,
) -> None:
    engine = make_engine("sqlite://")
    doc_id = seed(engine, sample_pdf_bytes)
    fake = Fake()
    progress = process_document(engine, doc_id, sample_pdf_bytes, make_deps(fake))
    assert (progress.done, progress.skipped, progress.failed, progress.pending) == (2, 1, 0, 0)
    assert progress.processed_this_call == 2 and progress.status == "extracted"
    assert progress.estimated_calls_remaining == 0
    assert fake.calls.count("meta") == 1
    with session_scope(engine) as db:
        document = db.get(Document, doc_id)
        assert document is not None and document.meta_done
        assert document.publication_date == date(2024, 7, 5)
        assert document.title == "Annual Report 2023-24"


def test_stops_dispatching_at_the_deadline_and_resumes_on_the_next_call(
    sample_pdf_bytes: bytes,
) -> None:
    engine = make_engine("sqlite://")
    doc_id = seed(engine, sample_pdf_bytes, meta_done=True)
    fake = Fake(delay=0.15)
    first = process_document(engine, doc_id, sample_pdf_bytes, make_deps(fake, budget_s=0.1))
    assert first.processed_this_call == 1 and first.pending == 1
    assert first.status == "processing" and first.estimated_calls_remaining == 1
    second = process_document(engine, doc_id, sample_pdf_bytes, make_deps(fake, budget_s=30))
    assert second.pending == 0 and second.status == "extracted"


def test_failed_page_is_retried_on_a_later_call(sample_pdf_bytes: bytes) -> None:
    engine = make_engine("sqlite://")
    doc_id = seed(engine, sample_pdf_bytes, meta_done=True)
    fake = Fake(fail_pages={2})
    first = process_document(engine, doc_id, sample_pdf_bytes, make_deps(fake))
    assert first.failed == 1 and first.done == 1 and first.status == "processing"
    assert first.last_errors and "page 2 exploded" in first.last_errors[0]
    second = process_document(engine, doc_id, sample_pdf_bytes, make_deps(fake))
    assert second.failed == 0 and second.done == 2 and second.status == "extracted"
    with session_scope(engine) as db:
        page = db.scalars(select(Page).where(Page.index == 2)).one()
        assert page.attempts == 2 and page.status == "done"


def test_gives_up_after_max_attempts_unless_retry_is_requested(sample_pdf_bytes: bytes) -> None:
    engine = make_engine("sqlite://")
    doc_id = seed(engine, sample_pdf_bytes, meta_done=True)

    class AlwaysFails(Fake):
        def __call__(self, req: LLMRequest) -> dict[str, Any]:
            raise RuntimeError("quota gone")

    fake = AlwaysFails()
    for _ in range(MAX_PAGE_ATTEMPTS):
        progress = process_document(engine, doc_id, sample_pdf_bytes, make_deps(fake))
    assert progress.failed == 2 and progress.pending == 0 and progress.status == "extracted"
    idle = process_document(engine, doc_id, sample_pdf_bytes, make_deps(fake))
    assert idle.processed_this_call == 0
    retried = process_document(engine, doc_id, sample_pdf_bytes, make_deps(fake), retry_failed=True)
    assert retried.processed_this_call == 2


def test_meta_failure_is_logged_and_does_not_block_extraction(sample_pdf_bytes: bytes) -> None:
    engine = make_engine("sqlite://")
    doc_id = seed(engine, sample_pdf_bytes)

    class MetaBreaks(Fake):
        def __call__(self, req: LLMRequest) -> dict[str, Any]:
            if req.purpose == "meta":
                raise RuntimeError("no meta")
            return super().__call__(req)

    progress = process_document(engine, doc_id, sample_pdf_bytes, make_deps(MetaBreaks()))
    assert progress.done == 2
    with session_scope(engine) as db:
        document = db.get(Document, doc_id)
        assert document is not None and document.meta_done is False
        failure = db.scalars(select(Failure).where(Failure.stage == "meta")).one()
        assert "no meta" in failure.message


def test_quota_exhaustion_pauses_the_batch_without_spending_page_attempts(
    sample_pdf_bytes: bytes,
) -> None:
    from fkl.llm.client import LLMQuotaError

    engine = make_engine("sqlite://")
    doc_id = seed(engine, sample_pdf_bytes, meta_done=True)

    class QuotaGone(Fake):
        def __call__(self, req: LLMRequest) -> dict[str, Any]:
            self.calls.append(req.purpose)
            raise LLMQuotaError("model quota exhausted: 402")

    fake = QuotaGone()
    progress = process_document(engine, doc_id, sample_pdf_bytes, make_deps(fake))
    assert progress.paused_reason is not None and "quota" in progress.paused_reason
    assert (
        progress.processed_this_call == 0 and len(fake.calls) == 1
    )  # circuit breaker: one attempt
    assert progress.pending == 2 and progress.failed == 0 and progress.status == "processing"
    with session_scope(engine) as db:
        pages = db.scalars(select(Page).where(Page.status != "skipped").order_by(Page.index)).all()
        assert all(p.status == "pending" and p.attempts == 0 for p in pages)
        failure = db.scalars(select(Failure).where(Failure.kind == "llm_quota")).one()
        assert "retried" in failure.handled

    resumed = process_document(engine, doc_id, sample_pdf_bytes, make_deps(Fake()))
    assert resumed.paused_reason is None and resumed.status == "extracted" and resumed.done == 2


def test_time_budget_starts_after_the_metadata_call(sample_pdf_bytes: bytes) -> None:
    engine = make_engine("sqlite://")
    doc_id = seed(engine, sample_pdf_bytes)  # meta not done yet

    class SlowMeta(Fake):
        def __call__(self, req: LLMRequest) -> dict[str, Any]:
            if req.purpose == "meta":
                time.sleep(0.2)
            return super().__call__(req)

    progress = process_document(
        engine, doc_id, sample_pdf_bytes, make_deps(SlowMeta(), budget_s=0.1)
    )
    assert progress.processed_this_call >= 1  # pages still get their own budget
