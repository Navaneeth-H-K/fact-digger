"""Model providers. AnthropicProvider talks to Claude through any Anthropic-compatible endpoint
(AgentRouter in this project) and turns quota, transport and format problems into typed errors.

Retries are handled here rather than by the SDK because AgentRouter signals an exhausted daily
quota with HTTP 402, which the SDK does not retry. Backoff is capped so a time-boxed batch call
still returns before the platform deadline.
"""

from __future__ import annotations

import base64
import json
import random
import re
import time
from collections.abc import Callable
from typing import Any

import anthropic

from fkl.llm.client import (
    ContentBlock,
    LLMJsonError,
    LLMQuotaError,
    LLMRequest,
    LLMTransientError,
)

_QUOTA_STATUSES = frozenset({402, 429})
_MAX_BACKOFF_SECONDS = 30.0
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _to_anthropic_content(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for block in blocks:
        if block["type"] == "image":
            converted.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block["media_type"],
                        "data": base64.b64encode(block["data"]).decode("ascii"),
                    },
                }
            )
        else:
            converted.append({"type": "text", "text": block["text"]})
    return converted


def _backoff_seconds(attempt: int) -> float:
    return min(2.0**attempt + random.uniform(0, 1), _MAX_BACKOFF_SECONDS)


def _looks_like_strict_rejection(error: anthropic.BadRequestError) -> bool:
    return "strict" in str(error).lower()


class AnthropicProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        create: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
        timeout: float = 120.0,
    ) -> None:
        if create is None:
            client = anthropic.Anthropic(
                api_key=api_key, base_url=base_url, max_retries=0, timeout=timeout
            )
            create = client.messages.create
        self._create = create
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._strict_supported = True

    def _tool(self, req: LLMRequest) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "name": req.tool_name,
            "description": f"Record the structured result of the {req.purpose} step.",
            "input_schema": req.tool_schema,
        }
        if self._strict_supported:
            tool["strict"] = True
        return tool

    def _send(self, req: LLMRequest) -> Any:
        return self._create(
            model=req.model,
            system=req.system,
            max_tokens=req.max_tokens,
            messages=[{"role": "user", "content": _to_anthropic_content(req.content)}],
            tools=[self._tool(req)],
            tool_choice={"type": "tool", "name": req.tool_name},
        )

    def _send_with_retries(self, req: LLMRequest) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._send(req)
            except anthropic.BadRequestError as error:
                if self._strict_supported and _looks_like_strict_rejection(error):
                    self._strict_supported = False
                    continue
                raise LLMJsonError(f"request rejected: {error}") from error
            except anthropic.APIStatusError as error:
                if error.status_code not in _QUOTA_STATUSES and error.status_code < 500:
                    raise LLMTransientError(f"unexpected status {error.status_code}") from error
                last_error = error
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as error:
                last_error = error
            if attempt < self._max_attempts - 1:
                self._sleep(_backoff_seconds(attempt))
        assert last_error is not None
        status = getattr(last_error, "status_code", None)
        if status in _QUOTA_STATUSES:
            raise LLMQuotaError(
                f"quota exhausted after {self._max_attempts} attempts"
            ) from last_error
        raise LLMTransientError(f"gave up after {self._max_attempts} attempts") from last_error

    @staticmethod
    def _tool_input(message: Any, tool_name: str) -> dict[str, Any]:
        for block in message.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                data: dict[str, Any] = dict(block.input)
                return data
        text = "".join(getattr(block, "text", "") for block in message.content)
        match = _JSON_OBJECT_RE.search(text)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError as error:
                raise LLMJsonError("response text contained malformed JSON") from error
            if isinstance(parsed, dict):
                return parsed
        raise LLMJsonError("response contained neither a tool call nor a JSON object")

    def complete(self, req: LLMRequest) -> dict[str, Any]:
        message = self._send_with_retries(req)
        usage = getattr(message, "usage", None)
        return {
            "tool_input": self._tool_input(message, req.tool_name),
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
            },
            "stop_reason": getattr(message, "stop_reason", None),
        }
