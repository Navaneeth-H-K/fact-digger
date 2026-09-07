"""Validated structured output: call the model, validate against a Pydantic model, repair once."""

from __future__ import annotations

from dataclasses import replace
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from fkl.llm.client import LLMClient, LLMJsonError, LLMRequest

ModelT = TypeVar("ModelT", bound=BaseModel)

_REPAIR_TEMPLATE = (
    "Your previous tool call failed validation with these errors:\n{errors}\n"
    "Call the tool again with a corrected, complete input. Previous input was:\n{previous}"
)


def _repair_request(req: LLMRequest, previous: object, errors: str) -> LLMRequest:
    note = {"type": "text", "text": _REPAIR_TEMPLATE.format(errors=errors, previous=previous)}
    return replace(req, content=[*req.content, note])


def call_structured(client: LLMClient, req: LLMRequest, model_cls: type[ModelT]) -> ModelT:
    """Return a validated `model_cls`; one repair round-trip is allowed before giving up."""
    result = client.call(req)
    try:
        return model_cls.model_validate(result.data)
    except ValidationError as first_error:
        repaired = client.call(_repair_request(req, result.data, str(first_error)))
        try:
            return model_cls.model_validate(repaired.data)
        except ValidationError as second_error:
            raise LLMJsonError(
                f"{req.purpose}: output failed validation twice: {second_error}"
            ) from second_error
