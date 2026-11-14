"""Bookkeeping for durable LangGraph checkpoints.

LangGraph stores checkpoints itself (``checkpoints`` / ``checkpoint_writes`` /
``checkpoint_blobs``) and offers no retention policy. This table records the
application-level thread and when it finished, so a periodic task can delete
expired threads without parsing checkpoint internals.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base_class import Base
from app.models.base import TimestampMixin


class AnalysisCheckpoint(Base, TimestampMixin):
    """One checkpoint thread owned by a finished PR analysis."""

    __tablename__ = "analysis_checkpoints"

    thread_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    pull_request_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("pull_requests.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
