"""Add analysis_decisions to pull_requests

Persists the orchestration decisions (why generation was declined, why a loop
stopped, which fallback produced the review) next to the analysis warnings, so
a completed run can be explained without a log search.

Revision ID: 9d4c1a7e6b20
Revises: 8b1f4d2c7a90
Create Date: 2026-11-12 10:15:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d4c1a7e6b20"
down_revision: str | None = "8b1f4d2c7a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pull_requests",
        sa.Column("analysis_decisions", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pull_requests", "analysis_decisions")
