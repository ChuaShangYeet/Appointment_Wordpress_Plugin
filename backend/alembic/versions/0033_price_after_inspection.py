"""a service's price can be told after an inspection

Revision ID: 0033
Revises: 0032

``services.price_after_inspection``: the business tells the customer the price once it has seen the job (a car nobody knows what is wrong
with, a house's wiring). The service's ``price`` is then what is known up front: 0 when nothing is, or a starting price (an inspection fee).
Existing services keep a fixed price. Downgrading drops the column.
"""
import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("services", sa.Column("price_after_inspection", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("services", "price_after_inspection")
