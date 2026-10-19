"""tenants.owner_email, terms_version, terms_accepted_at (self-service sign-up)

Revision ID: 0024
Revises: 0023

A business that signed itself up has an owner's email address (confirmed by a link before the business exists, and unique: one free business per
address) and the version of the terms it accepted, with when. Businesses set up by the operator, and every one that exists today, have none of these
(``NULL``).
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("owner_email", sa.String(length=254), nullable=True))
    op.add_column("tenants", sa.Column("terms_version", sa.String(length=40), nullable=True))
    op.add_column("tenants", sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("uq_tenants_owner_email", "tenants", ["owner_email"], unique=True, postgresql_where=sa.text("owner_email IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_tenants_owner_email", table_name="tenants")
    op.drop_column("tenants", "terms_accepted_at")
    op.drop_column("tenants", "terms_version")
    op.drop_column("tenants", "owner_email")
