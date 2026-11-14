"""Add analysis_checkpoints bookkeeping table

LangGraph owns its checkpoint tables and has no retention policy. This table
records finished threads so a periodic task can delete them via
``PostgresSaver.delete_thread`` without inspecting LangGraph internals.

Revision ID: c4a8e1d72f90
Revises: b7e2f4a91c30
Create Date: 2026-11-12 11:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4a8e1d72f90"
down_revision: str | None = "b7e2f4a91c30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_checkpoints",
        sa.Column("thread_id", sa.String(length=100), primary_key=True),
        sa.Column("pull_request_id", sa.String(length=36), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["pull_request_id"],
            ["pull_requests.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_analysis_checkpoints_pull_request_id",
        "analysis_checkpoints",
        ["pull_request_id"],
    )
    op.create_index(
        "ix_analysis_checkpoints_completed_at",
        "analysis_checkpoints",
        ["completed_at"],
    )
    op.create_index(
        "ix_analysis_checkpoints_created_at",
        "analysis_checkpoints",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_checkpoints_created_at",
        table_name="analysis_checkpoints",
    )
    op.drop_index(
        "ix_analysis_checkpoints_completed_at",
        table_name="analysis_checkpoints",
    )
    op.drop_index(
        "ix_analysis_checkpoints_pull_request_id",
        table_name="analysis_checkpoints",
    )
    op.drop_table("analysis_checkpoints")
