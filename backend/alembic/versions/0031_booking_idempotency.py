"""bookings remember the Idempotency-Key they were made with

Revision ID: 0031
Revises: 0030

``bookings.idempotency_key`` and ``bookings.idempotency_hash`` (a hash of the request) are filled when ``POST /public/bookings`` is sent with an ``Idempotency-Key`` header; the same request
sent again then returns that booking instead of making another. The partial unique index allows one booking per business and key (bookings without a key are not affected). Existing
bookings have neither. Downgrading drops the index and both columns.
"""
import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("idempotency_key", sa.String(length=100), nullable=True))
    op.add_column("bookings", sa.Column("idempotency_hash", sa.String(length=64), nullable=True))
    op.create_index("uq_bookings_idempotency_key", "bookings", ["tenant_id", "idempotency_key"], unique=True, postgresql_where=sa.text("idempotency_key IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_bookings_idempotency_key", table_name="bookings")
    op.drop_column("bookings", "idempotency_hash")
    op.drop_column("bookings", "idempotency_key")
