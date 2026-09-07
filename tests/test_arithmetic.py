"""Arithmetic tie-out: parts printed in one document that sum to a total printed in another."""

from datetime import date

from sqlalchemy import select

from fkl.compare import FactView, find_arithmetic_relations
from fkl.db import make_engine, session_scope
from fkl.llm.client import LLMClient
from fkl.models import Document, Page, Relation
from fkl.pipeline import PipelineDeps, link
from tests.test_link import seed_fact

Q4 = "2024-01-01:2024-03-31"


def fact(id: int, doc: str, attribute: str, value: float, unit: str = "count") -> FactView:
    return FactView(
        id=id,
        document_id=doc,
        entity_key="delhivery",
        attribute_key=attribute,
        value_num=value,
        unit=unit,
        period_key=Q4,
        estimate_type="actual",
        basis_key="",
        attributed_to=None,
        evidence_verified=True,
        publication_date=date(2024, 5, 17),
    )


def test_parts_in_one_document_tie_to_a_total_in_another() -> None:
    facts = [
        fact(1, "deck", "size team", 63_713),
        fact(2, "deck", "agent partner", 34_422),
        fact(3, "annual-report", "strength workforce", 98_135),
        fact(4, "annual-report", "centre delivery", 4_445),
    ]
    edges = find_arithmetic_relations(facts)
    assert len(edges) == 1
    edge = edges[0]
    assert edge.total.id == 3 and {p.id for p in edge.parts} == {1, 2}
    assert "63,713 + 34,422 = 98,135" in edge.explanation


def test_gateway_scope_arithmetic_is_found_with_rounding_tolerance() -> None:
    facts = [
        fact(1, "prospectus", "gateway excluding spoton", 82),
        fact(2, "prospectus", "gateway spoton", 40),
        fact(3, "deck", "gateway", 122),
        fact(4, "annual-report", "income other", 4_526_960_000, unit="INR"),
        fact(5, "annual-report", "operation revenue", 81_415_380_000, unit="INR"),
        fact(6, "deck", "income total", 85_940_000_000, unit="INR"),  # crore rounding
    ]
    edges = find_arithmetic_relations(facts)
    totals = {edge.total.id for edge in edges}
    assert totals == {3, 6}


def test_percentages_ratios_and_single_document_triples_are_ignored() -> None:
    facts = [
        fact(1, "a", "inflation food", 1.2, unit="percent"),
        fact(2, "a", "inflation core", 4.2, unit="percent"),
        fact(3, "b", "inflation headline", 5.4, unit="percent"),
        fact(4, "a", "employee permanent", 15_392),
        fact(5, "a", "worker contracted", 36_956),
        fact(6, "a", "size team", 52_348),
    ]
    assert find_arithmetic_relations(facts) == []


def test_link_persists_derived_relations_between_parts_and_total() -> None:
    engine = make_engine("sqlite://")
    with session_scope(engine) as db:
        docs = []
        for doc_id in ("deck", "annual-report"):
            document = Document(
                id=doc_id,
                filename=f"{doc_id}.pdf",
                size_bytes=1,
                sha256=doc_id.ljust(64, "0"),
                storage_path=f"pdfs/{doc_id}.pdf",
                status="extracted",
                publication_date=date(2024, 5, 17),
                page_count=1,
                meta_done=True,
            )
            db.add(document)
            page = Page(document_id=doc_id, index=0, text="p", char_count=1, status="done")
            db.add(page)
            db.flush()
            docs.append((document, page))
        (deck, deck_page), (ar, ar_page) = docs
        seed_fact(
            db,
            deck,
            deck_page,
            entity="Delhivery",
            attribute="team size",
            value_num=63_713,
            unit="count",
            period_key=Q4,
        )
        seed_fact(
            db,
            deck,
            deck_page,
            entity="Delhivery",
            attribute="partner agents",
            value_num=34_422,
            unit="count",
            period_key=Q4,
        )
        seed_fact(
            db,
            ar,
            ar_page,
            entity="Delhivery",
            attribute="workforce strength",
            value_num=98_135,
            unit="count",
            period_key=Q4,
        )
    deps = PipelineDeps(
        client=LLMClient(mode="off", fake=lambda req: {}),
        extract_model="m",
        adjudicate_model="m",
        page_concurrency=1,
    )
    progress = link(engine, deps)
    assert progress.by_verdict.get("derived") == 2
    with session_scope(engine) as db:
        derived = db.scalars(select(Relation).where(Relation.verdict == "derived")).all()
        assert {r.method for r in derived} == {"arithmetic"} and all(
            r.status == "final" for r in derived
        )
        assert all("98,135" in r.explanation for r in derived)
