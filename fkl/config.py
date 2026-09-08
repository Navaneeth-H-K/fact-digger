"""Environment-driven settings. The only place configuration is read; never secrets in code."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./fkl.db"

    storage_backend: Literal["local", "supabase"] = "local"
    local_storage_dir: str = "./local_storage"
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_bucket: str = "pdfs"

    llm_provider: Literal["anthropic", "openai_compat"] = "anthropic"
    llm_mode: Literal["off", "live", "record", "replay"] = "live"
    agentrouter_api_key: str | None = None
    agentrouter_base_url: str = "https://agentrouter.org"
    openai_compat_base_url: str | None = None
    openai_compat_api_key: str | None = None
    groq_api_key: str | None = None  # shortcut: sets the openai_compat endpoint to Groq
    extract_model: str = "claude-sonnet-4-5-20250929"
    adjudicate_model: str = "claude-opus-4-8"
    llm_cache_dir: str | None = None

    fiscal_year_start_month: int = 4
    page_concurrency: int = 4
    batch_budget_s: float = 45.0
    image_width: int = 1200
    extract_max_tokens: int = 6000
    max_facts_per_page: int = 25
    upload_max_bytes: int = 50 * 1024 * 1024
    diagnostics_token: str | None = None
