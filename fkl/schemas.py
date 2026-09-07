"""Pydantic models at the boundaries: what the LLM must return, and what the API serves.

The LLM tool schemas are generated from these models, so the field descriptions double as
instructions to the model.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EstimateType = Literal[
    "actual",
    "provisional",
    "advance_estimate",
    "revised",
    "budget",
    "projection",
    "target",
    "scenario",
    "third_party",
    "unknown",
]
Scale = Literal["one", "thousand", "lakh", "million", "crore", "billion", "trillion"]
ValueKind = Literal["number", "text", "range"]
QuoteSource = Literal["text", "image"]
PageKind = Literal["prose", "table", "chart", "mixed", "cover_or_toc", "blank_or_image_only"]


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity: str = Field(
        min_length=1, description="Who or what the fact is about, as named on the page."
    )
    attribute: str = Field(
        min_length=1,
        description="The measured quantity as a short noun phrase, without period or unit.",
    )
    value: str = Field(
        min_length=1, description="The value exactly as printed, e.g. '(-) 2.5' or '3,25,540'."
    )
    value_kind: ValueKind = "number"
    unit: str | None = Field(
        default=None, description="Unit as printed: 'per cent', '₹', 'US$', 'months'."
    )
    scale: Scale | None = Field(
        default=None, description="Scale word from the row/column/table header."
    )
    period: str | None = Field(
        default=None, description="Period as printed: 'FY24', 'Q1 FY25', 'as on 31 March 2025'."
    )
    period_start: str | None = Field(
        default=None, description="ISO date guess for the period start."
    )
    period_end: str | None = Field(default=None, description="ISO date guess for the period end.")
    estimate_type: EstimateType = "unknown"
    measurement_basis: str | None = Field(
        default=None,
        description="Any qualifier that affects comparability: 'consolidated', 'BoP basis', 'as % of GDP'.",
    )
    attributed_to: str | None = Field(
        default=None,
        description="The organisation the document credits when reporting someone else's figure.",
    )
    quote: str = Field(
        min_length=1,
        max_length=600,
        description="Verbatim contiguous text from the page containing the value (≤300 chars).",
    )
    quote_source: QuoteSource = "text"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    notes: str | None = None


class PageExtraction(BaseModel):
    page_kind: PageKind = "mixed"
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=25)
    problems: list[str] = Field(
        default_factory=list, description="What was hard to read on this page."
    )


class PageExtractionLoose(BaseModel):
    """Lenient shape used for parsing so one malformed fact drops only itself."""

    model_config = ConfigDict(extra="ignore")

    page_kind: str = "mixed"
    facts: list[dict[str, Any]] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class DocumentMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    publisher: str | None = None
    publication_date: str | None = Field(
        default=None, description="ISO date; first of month if only the month is known."
    )
    document_type: str | None = Field(
        default=None, description="e.g. 'annual report', 'investor presentation'."
    )
    fiscal_year_start_month: int | None = Field(default=None, ge=1, le=12)


class Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["corroborates", "contradicts", "superseded", "context_explained", "unresolved"]
    dimension: Literal[
        "period", "estimate_type", "basis", "unit", "vintage", "attribution", "value", "none"
    ] = "none"
    explanation: str = Field(min_length=1, max_length=800)
    newer_fact: Literal["a", "b", "none"] = "none"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


def tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for forced tool use; closed objects so the model cannot invent fields."""
    schema = model.model_json_schema()
    schema["additionalProperties"] = False
    for definition in schema.get("$defs", {}).values():
        if definition.get("type") == "object":
            definition["additionalProperties"] = False
    return schema


# --------------------------------------------------------------------------------------------
# API models
# --------------------------------------------------------------------------------------------


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class UploadTargetOut(BaseModel):
    url: str
    method: str
    token: str | None = None


class UploadTicket(BaseModel):
    document_id: str
    existing: bool
    upload: UploadTargetOut | None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    size_bytes: int
    sha256: str
    status: str
    page_count: int | None
    title: str | None
    publisher: str | None
    publication_date: date | None
    document_type: str | None
    fiscal_year_start_month: int | None
    meta_done: bool
    pages: dict[str, int] = Field(default_factory=dict, description="Page counts by status.")
    facts_count: int = 0
