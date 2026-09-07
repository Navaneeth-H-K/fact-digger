"""Engine and session factory. SQLite in tests and local runs; Postgres (Supabase) in production.

Production connects through Supabase's transaction pooler, which does not support prepared
statements and is shared by many short-lived serverless invocations, hence psycopg's
prepare_threshold=None and NullPool.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool, StaticPool

from fkl.models import Base


def normalize_database_url(url: str) -> str:
    """Rewrite Postgres URLs (as issued by Supabase/Heroku) to use the psycopg 3 driver."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def make_engine(url: str) -> Engine:
    """Create an engine for the URL and make sure the schema exists."""
    url = normalize_database_url(url)
    options: dict[str, Any] = {}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            options["poolclass"] = StaticPool
    else:
        options["poolclass"] = NullPool
        options["pool_pre_ping"] = True
        options["connect_args"] = {"prepare_threshold": None}
    engine = create_engine(url, future=True, **options)
    if url.startswith("sqlite"):
        _enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    return engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """A unit of work: commits on success, rolls back on error, always closes."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
