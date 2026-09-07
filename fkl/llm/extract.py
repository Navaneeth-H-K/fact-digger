"""Run the page-extraction call and validate its facts one by one."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from fkl.llm.client import LLMClient, LLMRequest
from fkl.llm.structured import call_structured
from fkl.schemas import ExtractedFact, PageExtractionLoose


@dataclass
class ExtractionOutcome:
    page_kind: str
    facts: list[ExtractedFact]
    invalid: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def extract_page(client: LLMClient, req: LLMRequest) -> ExtractionOutcome:
    """One malformed fact must not cost the whole page, so facts are validated individually."""
    loose = call_structured(client, req, PageExtractionLoose)
    outcome = ExtractionOutcome(page_kind=loose.page_kind, facts=[], problems=list(loose.problems))
    for raw in loose.facts:
        try:
            outcome.facts.append(ExtractedFact.model_validate(raw))
        except ValidationError as error:
            outcome.invalid.append((raw, str(error)))
    return outcome
