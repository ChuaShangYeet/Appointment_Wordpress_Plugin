"""phase 5c: periodic flag sweep - remember when each booking's review flags were last checked

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL = not swept yet: existing upcoming bookings get one full check on the sweep's first runs
    op.add_column("bookings", sa.Column("flags_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_bookings_unchecked", "bookings", ["created_at"], unique=False,
        postgresql_where=sa.text("flags_checked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_bookings_unchecked", table_name="bookings", postgresql_where=sa.text("flags_checked_at IS NULL"))
    op.drop_column("bookings", "flags_checked_at")
