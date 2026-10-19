"""resources.contact_email / contact_phone: where a staff member can be reached when their schedule collides

Revision ID: 0021
Revises: 0020

Optional, set by a manager on a *staff* resource (never public: the public staff list does not carry them). The backend alerts the stylist (or
mechanic, professional) on these when an add-on or an overrun runs into their next appointment.
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resources", sa.Column("contact_email", sa.String(length=254), nullable=True))
    op.add_column("resources", sa.Column("contact_phone", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("resources", "contact_phone")
    op.drop_column("resources", "contact_email")
