"""Provider-agnostic LLM client with fake injection and record/replay caching.

The client knows nothing about prompts. Callers build an LLMRequest (system prompt, content
blocks, and the tool schema the model must fill) and receive the tool input as a dict. Modes:

- off:    no provider; a fake callable answers (unit tests).
- live:   call the provider; never touch the cache.
- record: serve from cache when present, else call the provider and store the answer.
- replay: serve from cache only; a miss is an error (key-free demo runs).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from fkl.llm.cache import LLMCache

Mode = Literal["off", "live", "record", "replay"]
ContentBlock = dict[str, Any]


class LLMError(Exception):
    """Base class for LLM failures the pipeline knows how to report."""


class LLMCacheMissError(LLMError):
    pass


class LLMQuotaError(LLMError):
    """Rate limited or out of quota (HTTP 402/429) after all retries."""


class LLMTransientError(LLMError):
    """Server or network trouble after all retries."""


class LLMJsonError(LLMError):
    """The model's output could not be turned into the requested structure."""


@dataclass(frozen=True)
class LLMRequest:
    purpose: str
    model: str
    system: str
    content: list[ContentBlock]
    tool_name: str
    tool_schema: dict[str, Any]
    max_tokens: int = 4000


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    cached: bool = False


class Provider(Protocol):
    def complete(self, req: LLMRequest) -> dict[str, Any]:
        """Return {"tool_input": dict, "usage": dict}; raise LLMError subclasses on failure."""
        ...


def _content_for_key(content: list[ContentBlock]) -> list[ContentBlock]:
    """Image bytes are replaced by their digest so keys stay small and JSON-serialisable."""
    out: list[ContentBlock] = []
    for block in content:
        if block.get("type") == "image":
            data = block["data"]
            digest = hashlib.sha256(data).hexdigest() if isinstance(data, bytes) else str(data)
            out.append({**block, "data": digest})
        else:
            out.append(block)
    return out


def request_fingerprint(req: LLMRequest) -> dict[str, Any]:
    return {
        "model": req.model,
        "system": req.system,
        "content": _content_for_key(req.content),
        "tool_name": req.tool_name,
        "tool_schema": req.tool_schema,
    }


def cache_key(req: LLMRequest) -> str:
    serialized = json.dumps(request_fingerprint(req), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class LLMClient:
    def __init__(
        self,
        mode: Mode,
        provider: Provider | None = None,
        cache: LLMCache | None = None,
        fake: Callable[[LLMRequest], dict[str, Any]] | None = None,
    ) -> None:
        if mode == "off" and fake is None:
            raise ValueError("LLM mode 'off' requires a fake callable")
        if mode in ("live", "record") and provider is None:
            raise ValueError(f"LLM mode '{mode}' requires a provider")
        if mode in ("record", "replay") and cache is None:
            raise ValueError(f"LLM mode '{mode}' requires a cache")
        self.mode = mode
        self.provider = provider
        self.cache = cache
        self.fake = fake

    def call(self, req: LLMRequest) -> LLMResult:
        if self.mode == "off":
            assert self.fake is not None
            return LLMResult(data=self.fake(req))

        key = cache_key(req)
        if self.mode in ("record", "replay"):
            assert self.cache is not None
            entry = self.cache.get(key)
            if entry is not None:
                return LLMResult(data=entry["response"], usage=entry.get("usage", {}), cached=True)
            if self.mode == "replay":
                raise LLMCacheMissError(f"no cached response for {req.purpose} ({key[:12]}…)")

        assert self.provider is not None
        raw = self.provider.complete(req)
        result = LLMResult(data=raw["tool_input"], usage=raw.get("usage", {}))
        if self.mode == "record":
            assert self.cache is not None
            self.cache.put(
                key,
                model=req.model,
                purpose=req.purpose,
                request=request_fingerprint(req),
                response=result.data,
                usage=result.usage,
            )
        return result
