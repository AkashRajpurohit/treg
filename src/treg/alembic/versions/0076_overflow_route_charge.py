"""Record the aggregator's in-band charge on an overflow route (expand-only).

`overflow verify` writes the charge its relay call was billed; `overflow sync` prices the route
at no less than it, so a stale or missing listing price cannot under-disclose a paid route.

Revision ID: 0076
Revises: 0075
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: str | Sequence[str] | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("overflowroute", sa.Column("agg_charge_micro", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("overflowroute", "agg_charge_micro")
