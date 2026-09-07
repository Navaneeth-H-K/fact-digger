"""Prompts and request builders. System prompts are frozen strings so the provider can cache them;
everything that varies per call goes into the user turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from fkl.llm.client import ContentBlock, LLMRequest
from fkl.schemas import DocumentMeta, PageExtraction, Verdict, tool_schema

RECORD_PAGE_FACTS = "record_page_facts"
RECORD_DOCUMENT_META = "record_document_meta"
RECORD_VERDICT = "record_verdict"

MAX_PAGE_TEXT_CHARS = 6000
MAX_VOCABULARY_TERMS = 150

EXTRACTION_SYSTEM = """You extract checkable facts from one page of a document.

You see the rendered page image and the machine-extracted text layer. The text layer is often
corrupted: table rows shift, columns interleave, currency glyphs vanish or turn into stray letters,
footnote markers glue onto numbers. Trust the IMAGE for what a number is and where it sits in a
table; use the TEXT LAYER only to copy quotes.

What counts as a fact: a specific claim about a named entity with a numeric value (or a definite
categorical statement such as a rating, a rank or a classification) that another document could
agree or disagree with. Include prose numbers, headline table cells (totals, latest period, named
indicators) and KPIs on slides. Exclude page numbers, section numbers, table-of-contents lines,
chart axis ticks, dates on their own, boilerplate and values that are only labels. In dense tables
prefer the latest period and totals. Return at most 25 facts, most important first.

Fields:
- entity: who or what the fact is about. attribute: the measured quantity as a short noun phrase
  with no period or unit in it. Reuse names from the existing vocabulary when they mean the same.
- value: exactly as printed, including brackets or "(-)". unit: as printed. scale: the scale word
  from the row, column or table header ("million", "crore", "lakh", "billion"), never guessed.
- period: as printed. period_start/period_end: your ISO-date reading using the document's fiscal
  year convention. Quarters, halves and cumulative spans are their own periods; "as on" dates are
  points in time (start = end).
- estimate_type: from the document's own labels (provisional, advance estimate, revised, budget,
  projection, target, scenario). Use third_party and fill attributed_to when the document reports
  another organisation's figure.
- measurement_basis: any qualifier that changes comparability ("consolidated", "standalone",
  "BoP basis", "as % of GDP", "excluding X", "pro forma", "authorities' definition").
- quote: a contiguous verbatim substring of the TEXT LAYER that contains the value (≤300 chars,
  may include the row label). If the value is visible in the image but absent or garbled in the
  text layer, set quote_source to "image" and write the label and value as you read them.
  Never paraphrase a quote.
- confidence: how sure you are the value, unit, period and entity are all right.

Also report page_kind and a short list of problems you noticed while reading the page."""

META_SYSTEM = """You read the first pages of a document and record its metadata: title, publisher,
publication date (ISO; first of the month if only the month is known), document type, and the
month in which the issuer's fiscal year starts (4 for April–March years, 1 for calendar years).
Leave a field null when the pages do not say."""

ADJUDICATION_SYSTEM = """You compare two facts extracted from two documents and decide how they relate.

Verdicts:
- corroborates: same quantity, same period, same basis, compatible values (allow rounding).
- superseded: same quantity and period, but a later data vintage or a revision replaces the
  earlier value; name which fact is newer.
- context_explained: the values differ because the period, scope/basis, unit or attribution
  differs; name that dimension. This is not a contradiction.
- contradicts: same quantity, period, basis and unit carry incompatible values.
- unresolved: the evidence does not settle it; give your best hypothesis in the explanation.

Use only the fields and quotes provided. Keep the explanation under 60 words and cite the
distinguishing phrase from each quote."""


@dataclass(frozen=True)
class DocumentContext:
    filename: str
    title: str | None
    publisher: str | None
    publication_date: date | None
    fiscal_year_start_month: int


def _document_line(doc: DocumentContext) -> str:
    return (
        f"Document: {doc.filename}; title: {doc.title or 'unknown'}; publisher: "
        f"{doc.publisher or 'unknown'}; published: {doc.publication_date or 'unknown'}; "
        f"fiscal year starts in month {doc.fiscal_year_start_month}."
    )


def extraction_request(
    *,
    model: str,
    image_jpeg: bytes,
    page_text: str,
    document: DocumentContext,
    page_index: int,
    page_label: str | None,
    vocabulary: list[str],
) -> LLMRequest:
    terms = ", ".join(vocabulary[:MAX_VOCABULARY_TERMS]) or "(none yet)"
    text = (
        f"{_document_line(document)}\n"
        f"Page index {page_index}, printed label {page_label or 'none'}.\n"
        f"Existing attribute vocabulary (reuse a name when it means the same thing): {terms}\n\n"
        f"TEXT LAYER:\n{page_text[:MAX_PAGE_TEXT_CHARS]}"
    )
    content: list[ContentBlock] = [
        {"type": "image", "media_type": "image/jpeg", "data": image_jpeg},
        {"type": "text", "text": text},
    ]
    return LLMRequest(
        purpose="extract",
        model=model,
        system=EXTRACTION_SYSTEM,
        content=content,
        tool_name=RECORD_PAGE_FACTS,
        tool_schema=tool_schema(PageExtraction),
        max_tokens=6000,
    )


def meta_request(*, model: str, filename: str, first_pages_text: str) -> LLMRequest:
    text = f"Filename: {filename}\n\nFIRST PAGES:\n{first_pages_text[: MAX_PAGE_TEXT_CHARS * 2]}"
    return LLMRequest(
        purpose="meta",
        model=model,
        system=META_SYSTEM,
        content=[{"type": "text", "text": text}],
        tool_name=RECORD_DOCUMENT_META,
        tool_schema=tool_schema(DocumentMeta),
        max_tokens=600,
    )


def adjudication_request(*, model: str, fact_a: str, fact_b: str, hypothesis: str) -> LLMRequest:
    text = (
        f"FACT A:\n{fact_a}\n\nFACT B:\n{fact_b}\n\n"
        f"Rule-engine hypothesis: {hypothesis}\n\nDecide the verdict."
    )
    return LLMRequest(
        purpose="adjudicate",
        model=model,
        system=ADJUDICATION_SYSTEM,
        content=[{"type": "text", "text": text}],
        tool_name=RECORD_VERDICT,
        tool_schema=tool_schema(Verdict),
        max_tokens=800,
    )
