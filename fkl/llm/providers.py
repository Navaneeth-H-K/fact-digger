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


def _require_message_shape(response: Any, attribute: str) -> None:
    """Proxies sometimes answer 2xx with an HTML/text page or a bare JSON string; the SDK then
    hands back a str (or a message with no content). Surface the body instead of crashing."""
    payload = getattr(response, attribute, None)
    if isinstance(response, str) or not payload:
        raise LLMJsonError(f"proxy returned a non-message response: {str(response)[:300]}")


class AnthropicProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        create: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
        timeout: float = 180.0,
        user_agent: str | None = None,
    ) -> None:
        self._create: Callable[..., Any]
        if create is None:
            client = anthropic.Anthropic(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
                timeout=timeout,
                default_headers={"User-Agent": user_agent} if user_agent else None,
            )
            self._create = client.messages.create
        else:
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
                if error.status_code == 402:
                    raise LLMQuotaError(f"model quota exhausted: {error}") from error
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
        _require_message_shape(message, "content")
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


def inline_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local `$ref`s into the schema tree and drop `$defs`.

    OpenAI-compatible gateways (Groq, Gemini, Ollama) often reject the referenced form that
    Pydantic emits for nested models; the inlined form is equivalent.
    """
    definitions = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                merged = {**definitions[name], **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged)
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    result: dict[str, Any] = resolve(schema)
    return result


class OpenAICompatProvider:
    """Any OpenAI-style chat-completions endpoint: Ollama, vLLM, LM Studio, or a gateway.

    Lets the whole system run fully offline with a local vision model (slower, less accurate).
    Structured output is requested as a forced function call and, failing that, parsed from the
    message content.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        create: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
        timeout: float = 300.0,
    ) -> None:
        self._create: Callable[..., Any]
        if create is None:
            import openai

            client = openai.OpenAI(
                api_key=api_key, base_url=base_url, max_retries=0, timeout=timeout
            )
            self._create = client.chat.completions.create
        else:
            self._create = create
        self._sleep = sleep
        self._max_attempts = max_attempts

    @staticmethod
    def _user_content(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for block in blocks:
            if block["type"] == "image":
                encoded = base64.b64encode(block["data"]).decode("ascii")
                converted.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{block['media_type']};base64,{encoded}"},
                    }
                )
            else:
                converted.append({"type": "text", "text": block["text"]})
        return converted

    def _send(self, req: LLMRequest) -> Any:
        return self._create(
            model=req.model,
            max_tokens=req.max_tokens,
            messages=[
                {"role": "system", "content": req.system},
                {"role": "user", "content": self._user_content(req.content)},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": req.tool_name,
                        "description": f"Record the structured result of the {req.purpose} step.",
                        "parameters": inline_schema_refs(req.tool_schema),
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": req.tool_name}},
        )

    def _send_with_retries(self, req: LLMRequest) -> Any:
        import openai

        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._send(req)
            except openai.RateLimitError as error:
                last_error = error
            except openai.APIStatusError as error:
                if error.status_code == 402:
                    raise LLMQuotaError(f"model quota exhausted: {error}") from error
                if error.status_code < 500:
                    raise LLMTransientError(f"unexpected status {error.status_code}") from error
                last_error = error
            except (openai.APIConnectionError, openai.APITimeoutError) as error:
                last_error = error
            if attempt < self._max_attempts - 1:
                self._sleep(_backoff_seconds(attempt))
        assert last_error is not None
        if getattr(last_error, "status_code", None) in _QUOTA_STATUSES:
            raise LLMQuotaError("quota exhausted") from last_error
        raise LLMTransientError(f"gave up after {self._max_attempts} attempts") from last_error

    @staticmethod
    def _tool_input(completion: Any, tool_name: str) -> dict[str, Any]:
        _require_message_shape(completion, "choices")
        message = completion.choices[0].message
        for call in getattr(message, "tool_calls", None) or []:
            if call.function.name == tool_name:
                try:
                    parsed = json.loads(call.function.arguments)
                except json.JSONDecodeError as error:
                    raise LLMJsonError("function arguments were not valid JSON") from error
                if isinstance(parsed, dict):
                    return parsed
        match = _JSON_OBJECT_RE.search(getattr(message, "content", None) or "")
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError as error:
                raise LLMJsonError("response content contained malformed JSON") from error
            if isinstance(parsed, dict):
                return parsed
        raise LLMJsonError("response contained neither a function call nor a JSON object")

    def complete(self, req: LLMRequest) -> dict[str, Any]:
        completion = self._send_with_retries(req)
        usage = getattr(completion, "usage", None)
        return {
            "tool_input": self._tool_input(completion, req.tool_name),
            "usage": {
                "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            },
            "stop_reason": getattr(completion.choices[0], "finish_reason", None),
        }
