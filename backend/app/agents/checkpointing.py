"""Durable LangGraph checkpoints for the PR analysis pipeline.

Why this exists
---------------
A PR analysis is long, expensive and full of side effects (LLM calls, sandbox
runs, generated files). Without a checkpoint, Celery's retry model is "run the
whole graph again": every completed stage is recomputed and every LLM call is
paid for twice. With a checkpoint keyed by ``(pr_id, head_sha)``, a retry loads
the state after the last completed superstep and continues from the node that
was actually in flight.

Backends
--------
``memory``
    In-process :class:`InMemorySaver`. Default for tests and local development;
    it cannot survive a process restart, so retries start from scratch.
``postgres``
    :class:`~langgraph.checkpoint.postgres.PostgresSaver` using psycopg.
    Required for cross-process resume. The package is imported lazily: when it
    is missing the factory logs a warning and falls back to memory instead of
    breaking the worker. Its schema is created idempotently on first use by
    ``PostgresSaver.setup()`` — LangGraph owns these tables, they are not part
    of the application schema managed by Alembic.
``off``
    No checkpointer at all.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Cached per process: the saver holds a database connection and thread state.
_saver: Any | None = None
_initialised = False


def checkpoint_thread_id(pr_id: str, head_sha: str | None = None) -> str:
    """Stable checkpoint thread for one PR revision.

    Deliberately independent of the run id: a retry must land on the *same*
    thread as the attempt that crashed, while a new commit gets a new thread.
    """
    digest = hashlib.sha256(f"{pr_id}:{head_sha or ''}".encode()).hexdigest()[:16]
    return f"pr-{digest}"


def to_libpq_dsn(database_url: str) -> str:
    """Strip a SQLAlchemy async driver from a URL so psycopg can use it."""
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg://",
        "postgres+psycopg://",
    ):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix) :]
    return database_url


def _memory_saver() -> Any:
    """Build an in-process saver across LangGraph versions."""
    try:  # LangGraph >= 1.0
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    except ImportError:  # LangGraph 0.2.x
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()


def get_checkpointer() -> Any | None:
    """Return the configured checkpointer, or ``None`` when disabled.

    The factory never raises: an unavailable Postgres backend degrades to
    memory with a warning so analysis still runs (just without resume).
    """
    global _saver, _initialised

    if _initialised:
        return _saver

    _initialised = True
    settings = get_settings()
    backend = (settings.checkpoint_backend or "memory").strip().lower()

    if backend in {"off", "none", "disabled"}:
        _saver = None
        return None

    if backend != "postgres":
        _saver = _memory_saver()
        return _saver

    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        dsn = settings.checkpoint_dsn or to_libpq_dsn(str(settings.database_url))
        connection = psycopg.Connection.connect(
            dsn,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            connect_timeout=10,
        )
        saver = PostgresSaver(connection)
        saver.setup()
        _saver = saver
        atexit.register(_close)
        logger.info("Durable LangGraph checkpoints enabled", backend="postgres")
        return saver
    except Exception as exc:  # noqa: BLE001 - checkpointing must never break a run
        logger.warning(
            "Postgres checkpointer unavailable; falling back to in-memory checkpoints "
            "(a retry will restart the graph)",
            error=str(exc),
        )
        _saver = _memory_saver()
        return _saver


def _close() -> None:
    """Best-effort connection cleanup at process exit."""
    connection = getattr(_saver, "conn", None)
    if connection is not None:
        with contextlib.suppress(Exception):
            connection.close()


def discard_thread(thread_id: str) -> None:
    """Drop one thread's checkpoints from an in-process saver.

    The memory backend has no retention policy: without this, a long-running
    worker would keep every completed analysis in RAM. Durable backends are left
    alone so their history can still be inspected.
    """
    delete = getattr(_saver, "delete_thread", None)
    if delete is None:
        return
    try:
        delete(thread_id)
    except Exception as exc:  # noqa: BLE001 - cleanup must not fail a run
        logger.debug("Could not discard checkpoint thread", thread_id=thread_id, error=str(exc))


def reset_checkpointer() -> None:
    """Test hook: forget the cached saver so a new backend can be selected."""
    global _saver, _initialised

    _close()
    _saver = None
    _initialised = False
