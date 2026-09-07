"""LLM adapter: fake injection for tests, deterministic cache keys, record/replay behaviour."""

from pathlib import Path
from typing import Any

import pytest

from fkl.db import make_engine
from fkl.llm.cache import DbCache, JsonDirCache
from fkl.llm.client import LLMCacheMissError, LLMClient, LLMRequest, cache_key

TOOL = {"type": "object", "properties": {"facts": {"type": "array"}}}


def request(image: bytes = b"jpeg-1", model: str = "m1") -> LLMRequest:
    return LLMRequest(
        purpose="extract",
        model=model,
        system="You extract facts.",
        content=[
            {"type": "image", "media_type": "image/jpeg", "data": image},
            {"type": "text", "text": "Page 1 text"},
        ],
        tool_name="record_page_facts",
        tool_schema=TOOL,
        max_tokens=1000,
    )


class _StubProvider:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, req: LLMRequest) -> dict[str, Any]:
        self.calls += 1
        return {"tool_input": self.payload, "usage": {"input_tokens": 10, "output_tokens": 5}}


def test_fake_mode_returns_the_fake_result_and_never_touches_a_provider() -> None:
    seen: list[str] = []

    def fake(req: LLMRequest) -> dict[str, Any]:
        seen.append(req.purpose)
        return {"facts": []}

    client = LLMClient(mode="off", fake=fake)
    result = client.call(request())
    assert result.data == {"facts": []} and result.cached is False
    assert seen == ["extract"]


def test_off_mode_without_a_fake_is_a_configuration_error() -> None:
    with pytest.raises(ValueError):
        LLMClient(mode="off")


def test_cache_key_is_stable_and_sensitive_to_model_and_image_bytes() -> None:
    assert cache_key(request()) == cache_key(request())
    assert cache_key(request()) != cache_key(request(image=b"jpeg-2"))
    assert cache_key(request()) != cache_key(request(model="m2"))
    assert len(cache_key(request())) == 64


def test_record_mode_calls_provider_once_then_replay_serves_from_cache(tmp_path: Path) -> None:
    provider = _StubProvider({"facts": [1]})
    cache = JsonDirCache(tmp_path)
    recorder = LLMClient(mode="record", provider=provider, cache=cache)
    first = recorder.call(request())
    assert first.data == {"facts": [1]} and first.cached is False and provider.calls == 1

    replayer = LLMClient(mode="replay", cache=cache)
    second = replayer.call(request())
    assert second.data == {"facts": [1]} and second.cached is True
    assert provider.calls == 1


def test_replay_mode_raises_on_cache_miss(tmp_path: Path) -> None:
    client = LLMClient(mode="replay", cache=JsonDirCache(tmp_path))
    with pytest.raises(LLMCacheMissError):
        client.call(request())


def test_live_mode_does_not_write_the_cache(tmp_path: Path) -> None:
    cache = JsonDirCache(tmp_path)
    client = LLMClient(mode="live", provider=_StubProvider({"facts": []}), cache=cache)
    client.call(request())
    assert cache.get(cache_key(request())) is None


def test_db_cache_round_trips_entries() -> None:
    cache = DbCache(make_engine("sqlite://"))
    key = cache_key(request())
    assert cache.get(key) is None
    cache.put(
        key, model="m1", purpose="extract", request={"r": 1}, response={"facts": []}, usage={}
    )
    entry = cache.get(key)
    assert entry is not None and entry["response"] == {"facts": []}


def test_json_dir_cache_stores_one_file_per_key(tmp_path: Path) -> None:
    cache = JsonDirCache(tmp_path)
    key = cache_key(request())
    cache.put(
        key, model="m1", purpose="extract", request={"r": 1}, response={"a": 1}, usage={"t": 1}
    )
    assert (tmp_path / f"{key}.json").is_file()
    entry = cache.get(key)
    assert entry is not None and entry["usage"] == {"t": 1}
