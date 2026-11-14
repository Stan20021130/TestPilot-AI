"""Add verification/context outcomes to pull_requests

Denormalises the verify loop's exit contract and the context plan summary so
EvalOps can aggregate green/budget-exhausted/declined rates without parsing
the full decision log.

Revision ID: b7e2f4a91c30
Revises: 9d4c1a7e6b20
Create Date: 2026-11-12 11:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2f4a91c30"
down_revision: str | None = "9d4c1a7e6b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pull_requests",
        sa.Column("verify_exit_reason", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "pull_requests",
        sa.Column("verify_rounds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "pull_requests",
        sa.Column("context_mode", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "pull_requests",
        sa.Column("generation_plan_summary", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_pull_requests_verify_exit_reason",
        "pull_requests",
        ["verify_exit_reason"],
    )


def downgrade() -> None:
    op.drop_index("ix_pull_requests_verify_exit_reason", table_name="pull_requests")
    op.drop_column("pull_requests", "generation_plan_summary")
    op.drop_column("pull_requests", "context_mode")
    op.drop_column("pull_requests", "verify_rounds")
    op.drop_column("pull_requests", "verify_exit_reason")
