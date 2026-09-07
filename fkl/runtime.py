"""Wire settings into concrete collaborators: engine, storage, LLM client, pipeline deps.

Tests build a Runtime by hand (SQLite, a temp directory, a fake model); production builds one
from the environment at application start-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from fkl.config import Settings
from fkl.db import make_engine
from fkl.llm.cache import DbCache, JsonDirCache, LLMCache
from fkl.llm.client import LLMClient, Provider
from fkl.llm.providers import AnthropicProvider, OpenAICompatProvider
from fkl.pipeline import PipelineDeps
from fkl.storage import LocalDirStorage, Storage, SupabaseStorage


@dataclass
class Runtime:
    settings: Settings
    engine: Engine
    storage: Storage
    deps: PipelineDeps


def build_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "supabase":
        if not settings.supabase_url or not settings.supabase_service_role_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
        from supabase import create_client

        client = create_client(settings.supabase_url, settings.supabase_service_role_key)
        return SupabaseStorage(client.storage, bucket=settings.supabase_bucket)
    return LocalDirStorage(Path(settings.local_storage_dir))


def build_provider(settings: Settings) -> Provider:
    if settings.llm_provider == "openai_compat":
        if not settings.openai_compat_base_url:
            raise ValueError("OPENAI_COMPAT_BASE_URL is required for the openai_compat provider")
        return OpenAICompatProvider(
            api_key=settings.openai_compat_api_key or "none",
            base_url=settings.openai_compat_base_url,
        )
    if not settings.agentrouter_api_key:
        raise ValueError("AGENTROUTER_API_KEY is required for live or record mode")
    return AnthropicProvider(
        api_key=settings.agentrouter_api_key, base_url=settings.agentrouter_base_url
    )


def build_cache(settings: Settings, engine: Engine) -> LLMCache:
    if settings.llm_cache_dir:
        return JsonDirCache(Path(settings.llm_cache_dir))
    return DbCache(engine)


def build_llm_client(settings: Settings, engine: Engine) -> LLMClient:
    if settings.llm_mode == "off":
        raise ValueError("LLM_MODE=off is only for tests; use live, record or replay")
    provider = build_provider(settings) if settings.llm_mode in ("live", "record") else None
    cache = build_cache(settings, engine) if settings.llm_mode in ("record", "replay") else None
    return LLMClient(mode=settings.llm_mode, provider=provider, cache=cache)


def build_deps(settings: Settings, client: LLMClient) -> PipelineDeps:
    return PipelineDeps(
        client=client,
        extract_model=settings.extract_model,
        adjudicate_model=settings.adjudicate_model,
        fiscal_year_start_month=settings.fiscal_year_start_month,
        page_concurrency=settings.page_concurrency,
        budget_s=settings.batch_budget_s,
        image_width=settings.image_width,
    )


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    return Runtime(
        settings=settings,
        engine=engine,
        storage=build_storage(settings),
        deps=build_deps(settings, build_llm_client(settings, engine)),
    )
