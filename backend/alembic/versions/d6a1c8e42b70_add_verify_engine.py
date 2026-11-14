"""Add verify_engine to pull_requests

Records which verify implementation produced the outcome (legacy or next), so
canary/differential data can be split by engine without parsing decisions.

Revision ID: d6a1c8e42b70
Revises: c4a8e1d72f90
Create Date: 2026-11-12 14:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6a1c8e42b70"
down_revision: str | None = "c4a8e1d72f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pull_requests",
        sa.Column("verify_engine", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pull_requests", "verify_engine")
