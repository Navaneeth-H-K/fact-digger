"""Record/replay cache for LLM responses.

Recording a run makes the demo reproducible without an API key: the committed cache replays the
exact model outputs for the starter documents. Two backends: a directory of JSON files (local runs,
committed samples) and the llm_cache table (serverless deployment).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from fkl.models import LlmCacheEntry

CacheEntry = dict[str, Any]


class LLMCache(Protocol):
    def get(self, key: str) -> CacheEntry | None: ...

    def put(
        self,
        key: str,
        *,
        model: str,
        purpose: str,
        request: dict[str, Any],
        response: dict[str, Any],
        usage: dict[str, Any],
    ) -> None: ...


class JsonDirCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> CacheEntry | None:
        path = self._path(key)
        if not path.is_file():
            return None
        entry: CacheEntry = json.loads(path.read_text(encoding="utf-8"))
        return entry

    def put(
        self,
        key: str,
        *,
        model: str,
        purpose: str,
        request: dict[str, Any],
        response: dict[str, Any],
        usage: dict[str, Any],
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "key": key,
            "model": model,
            "purpose": purpose,
            "request": request,
            "response": response,
            "usage": usage,
        }
        self._path(key).write_text(
            json.dumps(entry, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
        )


class DbCache:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get(self, key: str) -> CacheEntry | None:
        with Session(self.engine) as session:
            row = session.get(LlmCacheEntry, key)
            if row is None:
                return None
            return {
                "key": row.key,
                "model": row.model,
                "purpose": row.purpose,
                "request": row.request,
                "response": row.response,
                "usage": row.usage,
            }

    def put(
        self,
        key: str,
        *,
        model: str,
        purpose: str,
        request: dict[str, Any],
        response: dict[str, Any],
        usage: dict[str, Any],
    ) -> None:
        with Session(self.engine) as session:
            session.merge(
                LlmCacheEntry(
                    key=key,
                    model=model,
                    purpose=purpose,
                    request=request,
                    response=response,
                    usage=usage,
                )
            )
            session.commit()
