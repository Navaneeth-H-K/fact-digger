"""Anthropic provider (via AgentRouter): request shape, backoff on quota errors, strict fallback,
and the structured-call repair path that turns tool input into validated Pydantic models."""

import base64
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from fkl.llm.client import (
    LLMClient,
    LLMJsonError,
    LLMQuotaError,
    LLMRequest,
    LLMTransientError,
)
from fkl.llm.providers import AnthropicProvider
from fkl.llm.structured import call_structured

SCHEMA = {"type": "object", "properties": {"facts": {"type": "array"}}, "required": ["facts"]}


def request() -> LLMRequest:
    return LLMRequest(
        purpose="extract",
        model="claude-sonnet-4-5-20250929",
        system="sys",
        content=[
            {"type": "image", "media_type": "image/jpeg", "data": b"\xff\xd8jpeg"},
            {"type": "text", "text": "page text"},
        ],
        tool_name="record_page_facts",
        tool_schema=SCHEMA,
        max_tokens=1234,
    )


def tool_message(tool_input: dict[str, Any], stop_reason: str = "tool_use") -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="record_page_facts", input=tool_input)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def text_message(text: str) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def api_error(status: int, message: str = "boom") -> anthropic.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "https://agentrouter.org"))
    if status == 429:
        return anthropic.RateLimitError(message, response=response, body=None)
    if status == 400:
        return anthropic.BadRequestError(message, response=response, body=None)
    return anthropic.APIStatusError(message, response=response, body=None)


def make_provider(
    create: Callable[..., Any], sleeps: list[float] | None = None
) -> AnthropicProvider:
    return AnthropicProvider(
        api_key="k",
        base_url="https://agentrouter.org",
        create=create,
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
        max_attempts=4,
    )


def test_request_is_sent_as_forced_tool_use_with_base64_image() -> None:
    captured: dict[str, Any] = {}

    def create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return tool_message({"facts": []})

    result = make_provider(create).complete(request())
    assert result["tool_input"] == {"facts": []}
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert captured["model"] == "claude-sonnet-4-5-20250929"
    assert captured["system"] == "sys"
    assert captured["max_tokens"] == 1234
    assert captured["tool_choice"] == {"type": "tool", "name": "record_page_facts"}
    assert captured["tools"][0]["input_schema"] == SCHEMA
    image, text = captured["messages"][0]["content"]
    assert image["source"]["data"] == base64.b64encode(b"\xff\xd8jpeg").decode()
    assert image["source"]["media_type"] == "image/jpeg"
    assert text == {"type": "text", "text": "page text"}
    assert "temperature" not in captured


def test_quota_error_is_retried_with_backoff_then_succeeds() -> None:
    attempts = {"n": 0}

    def create(**kwargs: Any) -> Any:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise api_error(402, "Budget pool quota exhausted")
        return tool_message({"facts": [1]})

    sleeps: list[float] = []
    result = make_provider(create, sleeps).complete(request())
    assert result["tool_input"] == {"facts": [1]}
    assert attempts["n"] == 2 and len(sleeps) == 1 and 0 < sleeps[0] <= 30


def test_persistent_rate_limit_raises_quota_error_after_max_attempts() -> None:
    def create(**kwargs: Any) -> Any:
        raise api_error(429)

    sleeps: list[float] = []
    with pytest.raises(LLMQuotaError):
        make_provider(create, sleeps).complete(request())
    assert len(sleeps) == 3 and max(sleeps) <= 30


def test_server_errors_raise_transient_error_after_max_attempts() -> None:
    def create(**kwargs: Any) -> Any:
        raise api_error(503)

    with pytest.raises(LLMTransientError):
        make_provider(create).complete(request())


def test_strict_tool_use_falls_back_once_and_is_remembered() -> None:
    strict_flags: list[bool] = []

    def create(**kwargs: Any) -> Any:
        strict = bool(kwargs["tools"][0].get("strict"))
        strict_flags.append(strict)
        if strict:
            raise api_error(400, "tools.0.strict: Extra inputs are not permitted")
        return tool_message({"facts": []})

    provider = make_provider(create)
    provider.complete(request())
    provider.complete(request())
    assert strict_flags == [True, False, False]


def test_json_in_text_is_used_when_no_tool_block_comes_back() -> None:
    def create(**kwargs: Any) -> Any:
        return text_message('Here you go: {"facts": [{"a": 1}]} thanks')

    assert make_provider(create).complete(request())["tool_input"] == {"facts": [{"a": 1}]}


def test_unparseable_response_raises_json_error() -> None:
    def create(**kwargs: Any) -> Any:
        return text_message("no json here")

    with pytest.raises(LLMJsonError):
        make_provider(create).complete(request())


class Page(BaseModel):
    facts: list[int]


def test_call_structured_repairs_invalid_output_once() -> None:
    answers = [{"facts": "not-a-list"}, {"facts": [1, 2]}]
    seen: list[LLMRequest] = []

    def fake(req: LLMRequest) -> dict[str, Any]:
        seen.append(req)
        return answers.pop(0)

    client = LLMClient(mode="off", fake=fake)
    page = call_structured(client, request(), Page)
    assert page.facts == [1, 2]
    assert len(seen) == 2
    repair_text = seen[1].content[-1]["text"]
    assert "failed validation" in repair_text and "facts" in repair_text


def test_call_structured_gives_up_after_one_repair() -> None:
    client = LLMClient(mode="off", fake=lambda req: {"facts": "still wrong"})
    with pytest.raises(LLMJsonError):
        call_structured(client, request(), Page)


# --------------------------------------------------------------------------------------------
# OpenAI-compatible provider (local models via Ollama / vLLM, or any OpenAI-style gateway)
# --------------------------------------------------------------------------------------------


def openai_tool_message(arguments: str) -> Any:
    tool_call = SimpleNamespace(
        function=SimpleNamespace(name="record_page_facts", arguments=arguments)
    )
    message = SimpleNamespace(tool_calls=[tool_call], content=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
    )


def test_openai_compat_provider_sends_chat_completion_with_forced_function_call() -> None:
    from fkl.llm.providers import OpenAICompatProvider

    captured: dict[str, Any] = {}

    def create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return openai_tool_message('{"facts": [1]}')

    provider = OpenAICompatProvider(
        api_key="none", base_url="http://localhost:11434/v1", create=create
    )
    result = provider.complete(request())
    assert result["tool_input"] == {"facts": [1]}
    assert result["usage"] == {"input_tokens": 7, "output_tokens": 3}
    assert captured["model"] == "claude-sonnet-4-5-20250929" and captured["max_tokens"] == 1234
    system, user = captured["messages"]
    assert system == {"role": "system", "content": "sys"}
    image, text = user["content"]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert text == {"type": "text", "text": "page text"}
    assert captured["tools"][0]["function"]["parameters"] == SCHEMA
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "record_page_facts"},
    }


def test_openai_compat_provider_falls_back_to_json_in_content() -> None:
    from fkl.llm.providers import OpenAICompatProvider

    def create(**kwargs: Any) -> Any:
        message = SimpleNamespace(tool_calls=None, content='{"facts": []}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    provider = OpenAICompatProvider(api_key="none", base_url="http://x/v1", create=create)
    assert provider.complete(request())["tool_input"] == {"facts": []}
