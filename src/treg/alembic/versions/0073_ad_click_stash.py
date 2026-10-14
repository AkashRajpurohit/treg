"""Add first-party email-to-click attribution stash (expand-only).

Revision ID: 0073
Revises: 0072
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: str | Sequence[str] | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "adclickstash",
        sa.Column("email", sa.String(), primary_key=True),
        sa.Column("raw_cookie", sa.String(length=4096), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("adclickstash")
