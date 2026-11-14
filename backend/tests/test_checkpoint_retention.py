"""Checkpoint bookkeeping and retention tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.analysis_checkpoint import AnalysisCheckpoint
from app.tasks import maintenance, pr_pipeline

pytestmark = pytest.mark.unit


class _SharedSession:
    """Context manager yielding an existing session (no commit/close)."""

    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakeSaver:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


async def test_prune_expired_deletes_only_old_threads(
    db_session, test_pull_request, monkeypatch
) -> None:
    monkeypatch.setattr(maintenance, "get_session", lambda: _SharedSession(db_session))
    monkeypatch.setattr(
        maintenance,
        "get_settings",
        lambda: SimpleNamespace(checkpoint_retention_days=7),
    )

    old = AnalysisCheckpoint(
        thread_id="old-thread",
        pull_request_id=test_pull_request.id,
        completed_at=datetime.now(UTC) - timedelta(days=30),
    )
    fresh = AnalysisCheckpoint(
        thread_id="fresh-thread",
        pull_request_id=test_pull_request.id,
        completed_at=datetime.now(UTC),
    )
    db_session.add_all([old, fresh])
    await db_session.flush()

    saver = _FakeSaver()
    result = await maintenance.prune_expired(7, saver)

    assert result == {"deleted": 1, "expired": 1, "retention_days": 7}
    assert saver.deleted == ["old-thread"]

    remaining = (await db_session.execute(select(AnalysisCheckpoint.thread_id))).scalars().all()
    assert remaining == ["fresh-thread"]


async def test_register_checkpoint_is_idempotent(
    db_session, test_pull_request, monkeypatch
) -> None:
    monkeypatch.setattr(pr_pipeline, "get_session", lambda: _SharedSession(db_session))

    await pr_pipeline._register_checkpoint("thread-1", test_pull_request.id)
    await pr_pipeline._register_checkpoint("thread-1", test_pull_request.id)
    await db_session.flush()

    rows = (await db_session.execute(select(AnalysisCheckpoint))).scalars().all()
    assert [row.thread_id for row in rows] == ["thread-1"]
    assert rows[0].completed_at is not None


def test_prune_task_refuses_memory_fallback(monkeypatch) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    monkeypatch.setattr(
        maintenance,
        "get_settings",
        lambda: SimpleNamespace(checkpoint_backend="postgres", checkpoint_retention_days=7),
    )
    monkeypatch.setattr(maintenance, "get_checkpointer", lambda: InMemorySaver())

    result = maintenance.prune_checkpoints.run(None)

    assert result["deleted"] == 0
    assert result["reason"] == "postgres checkpointer unavailable"


def test_prune_task_skips_non_postgres_backend(monkeypatch) -> None:
    monkeypatch.setattr(
        maintenance,
        "get_settings",
        lambda: SimpleNamespace(checkpoint_backend="memory", checkpoint_retention_days=7),
    )

    result = maintenance.prune_checkpoints.run(None)

    assert result["deleted"] == 0
    assert "memory" in result["reason"]
