"""Add analysis_warnings to pull_requests

Persists the non-fatal context degradations raised by the context-collection
agents (empty retrieval, missing checkout, unknown blast radius, ...) so a
silently degraded analysis can be audited after the fact.

Revision ID: 8b1f4d2c7a90
Revises: 2c5589a68ebf
Create Date: 2026-11-11 14:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b1f4d2c7a90"
down_revision: str | None = "2c5589a68ebf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pull_requests",
        sa.Column("analysis_warnings", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pull_requests", "analysis_warnings")
