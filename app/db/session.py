"""SQLAlchemy engine, connection pool, and session management."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.db.base import CMIR_SCHEMA, PENALTIES_SCHEMA, PROCESS_SCHEMA, Base
from app.utils.sanitize import strip_nul_bytes


def apply_sqlite_schema_translation(engine: Engine) -> Engine:
    """Redirect the application's Postgres schemas onto SQLite's single namespace.

    LANGGRAPH_SCHEMA is absent by design: no ORM model binds to it, so nothing on
    Base.metadata needs translating. LangGraph's PostgresSaver populates it at runtime.
    """
    if engine.dialect.name == "sqlite":
        return engine.execution_options(
            schema_translate_map={
                PROCESS_SCHEMA: None,
                CMIR_SCHEMA: None,
                PENALTIES_SCHEMA: None,
            },
        )
    return engine


def checkpoint_dsn(database_url: str, schema: str) -> str:
    """Convert a SQLAlchemy PostgreSQL URL into a psycopg DSN for LangGraph's saver.

    `PostgresSaver` connects with psycopg directly and rejects SQLAlchemy's
    `+psycopg`/`+psycopg2` driver suffix. The DSN also pins `search_path` so
    checkpoint tables land in the given schema without any database-level change.
    """
    parsed = urlsplit(database_url)
    scheme = parsed.scheme.replace("+psycopg2", "").replace("+psycopg", "")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("options", f"-csearch_path={schema},public"))

    return urlunsplit((scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


class Database:
    """Owns the application's SQLAlchemy engine and session factory."""

    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int | None = None,
        max_overflow: int | None = None,
        pool_timeout: int | None = None,
        **engine_kwargs: Any,
    ) -> None:
        # Validate pooled connections before handing them to the application.
        # This is especially useful for long-idle connections that may have been
        # closed by the network or database while still present in the pool.
        engine_kwargs.setdefault("pool_pre_ping", True)
        if database_url.startswith("postgresql"):
            # Keep PostgreSQL connections active during idle periods. This helps
            # prevent network infrastructure (notably WSL2/NAT) from silently
            # expiring otherwise-idle TCP connections between database operations.
            engine_kwargs.setdefault(
                "connect_args",
                {
                    "keepalives": 1,
                    "keepalives_idle": 2,
                    "keepalives_interval": 2,
                    "keepalives_count": 3,
                },
            )
        # Serialize arbitrary application objects as JSON, falling back to str()
        # for values such as UUIDs that the standard encoder does not handle.
        # Remove embedded NUL bytes first because PostgreSQL JSON/JSONB rejects them.
        engine_kwargs.setdefault(
            "json_serializer",
            lambda obj: json.dumps(strip_nul_bytes(obj), default=str),
        )

        if pool_size is not None:
            engine_kwargs.setdefault("pool_size", pool_size)

        if max_overflow is not None:
            engine_kwargs.setdefault("max_overflow", max_overflow)

        if pool_timeout is not None:
            engine_kwargs.setdefault("pool_timeout", pool_timeout)

        self._engine = apply_sqlite_schema_translation(create_engine(database_url, **engine_kwargs))
        self._session_factory = sessionmaker(
            bind=self._engine,
            autoflush=False,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> Engine:
        """Return the underlying SQLAlchemy engine."""
        return self._engine

    def create_all_tables(self) -> None:
        """Create ORM tables for tests and local bootstrapping."""
        Base.metadata.create_all(self._engine)

    def dispose(self) -> None:
        """Dispose the engine and its connection pool."""
        self._engine.dispose()

    def new_session(self) -> Session:
        """Create a session for a caller-managed lifecycle."""
        return self._session_factory()

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """Provide a transactional session for non-request callers."""
        session = self._session_factory()

        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
