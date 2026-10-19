"""phase 2b: per-tenant switch letting restaurant customers choose their own table

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "time_slot_settings",
        sa.Column("allow_table_selection", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("time_slot_settings", "allow_table_selection")
