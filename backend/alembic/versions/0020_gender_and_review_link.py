"""customers.gender (spa and salon) and business_profiles.review_url (review invitations)

Revision ID: 0020
Revises: 0019

``customers.gender``: the blueprint's "Customer Extensions: customer_gender" for spas and salons. Optional, one of female / male / other,
personal data under PDPA (part of the access export, cleared by erasure). ``business_profiles.review_url``: where the business's own
website shows the review page; the completed-visit email links to it with a signed token in place of ``{token}``.
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("gender", sa.String(length=10), nullable=True))
    op.create_check_constraint(op.f("ck_customers_gender"), "customers", "gender IS NULL OR gender IN ('female','male','other')")
    op.add_column("business_profiles", sa.Column("review_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("business_profiles", "review_url")
    op.drop_constraint(op.f("ck_customers_gender"), "customers", type_="check")
    op.drop_column("customers", "gender")
