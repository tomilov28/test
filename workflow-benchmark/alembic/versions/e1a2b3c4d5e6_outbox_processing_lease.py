"""add processing_started_at lease to workflow_commands

Audit A03: stale PROCESSING detection used `created_at`, which is wrong - a
command created long ago but claimed just now must NOT be re-armed as stale.
The lease timestamp is written at claim time (PENDING -> PROCESSING) and stale
rearm is computed from it.

Revision ID: e1a2b3c4d5e6
Revises: 298bb5dfa5ff
Create Date: 2026-08-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1a2b3c4d5e6"
down_revision: Union[str, None] = "298bb5dfa5ff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflow_commands",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflow_commands", "processing_started_at")
