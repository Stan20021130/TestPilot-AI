"""Periodic maintenance tasks.

LangGraph keeps checkpoint rows forever and exposes no retention policy. The
application records every finished thread in ``analysis_checkpoints``; this task
deletes threads older than ``CHECKPOINT_RETENTION_DAYS`` through the saver's
own API, then drops the bookkeeping rows.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app.agents.checkpointing import get_checkpointer
from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.session import get_session
from app.models.analysis_checkpoint import AnalysisCheckpoint
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def _expired_thread_ids(cutoff: datetime) -> list[str]:
    async with get_session() as db:
        result = await db.execute(
            select(AnalysisCheckpoint.thread_id).where(AnalysisCheckpoint.completed_at < cutoff)
        )
        return list(result.scalars().all())


async def _forget_threads(thread_ids: list[str]) -> None:
    if not thread_ids:
        return
    async with get_session() as db:
        await db.execute(
            delete(AnalysisCheckpoint).where(AnalysisCheckpoint.thread_id.in_(thread_ids))
        )


async def prune_expired(retention_days: int | None, saver: Any) -> dict[str, Any]:
    """Delete checkpoint threads older than the retention window.

    Split from the Celery entry point so it can be tested against a session and
    a fake saver without going through a task.
    """
    days = retention_days or get_settings().checkpoint_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    expired = await _expired_thread_ids(cutoff)

    delete_thread = getattr(saver, "delete_thread", None)
    if delete_thread is None:
        return {"deleted": 0, "reason": "checkpointer has no delete_thread"}

    deleted: list[str] = []
    for thread_id in expired:
        try:
            delete_thread(thread_id)
            deleted.append(thread_id)
        except Exception as exc:  # noqa: BLE001 - one bad thread must not stop pruning
            logger.warning(
                "Could not delete checkpoint thread",
                thread_id=thread_id,
                error=str(exc),
            )

    await _forget_threads(deleted)
    logger.info(
        "Checkpoint retention pass finished",
        expired=len(expired),
        deleted=len(deleted),
        retention_days=days,
    )
    return {"deleted": len(deleted), "expired": len(expired), "retention_days": days}


@celery_app.task(name="app.tasks.maintenance.prune_checkpoints")
def prune_checkpoints(retention_days: int | None = None) -> dict[str, Any]:
    """Delete finished checkpoint threads older than the retention window."""
    settings = get_settings()
    backend = (settings.checkpoint_backend or "memory").strip().lower()
    if backend != "postgres":
        return {"deleted": 0, "reason": f"checkpoint backend is {backend}"}

    saver = get_checkpointer()
    if type(saver).__name__ != "PostgresSaver":
        # The factory degrades to memory when the driver/database is missing.
        # Deleting bookkeeping rows then would orphan the real checkpoint rows.
        return {"deleted": 0, "reason": "postgres checkpointer unavailable"}
    return asyncio.run(prune_expired(retention_days, saver))
