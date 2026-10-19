"""tenants.country, and a structured address on the business profile

Revision ID: 0022
Revises: 0021

``tenants.country`` (MY, SG or AU) is the one place a business's country is kept; its time zones, phone code, postcode shape, states and currency
follow from it (``app.core.countries``). Businesses set up before it existed are given the country their phone code names (60, 65 or 61); one
with any other code is not in a country the service supports, so the migration stops and says which, rather than label it wrongly.

``business_profiles`` gains the address in parts (street, second line, city, state, postcode). The free-text ``address`` stays: it is what the
booking page shows, and is written from the parts when they are given.
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

CALLING_CODES = {"60": "MY", "65": "SG", "61": "AU"}


def upgrade() -> None:
    op.add_column("tenants", sa.Column("country", sa.String(length=2), nullable=True))
    conn = op.get_bind()
    for code, country in CALLING_CODES.items():
        conn.execute(sa.text("UPDATE tenants SET country = :country WHERE phone_country_code = :code"), {"country": country, "code": code})
    stray = conn.execute(sa.text("SELECT id, name, phone_country_code FROM tenants WHERE country IS NULL ORDER BY name")).all()
    if stray:
        listing = "; ".join(f"{name} ({tenant_id}, phone code {code})" for tenant_id, name, code in stray)
        raise RuntimeError(
            "These businesses have a phone code that is not Malaysia (60), Singapore (65) or Australia (61), so no country can be set for them: "
            f"{listing}. Change their phone_country_code or remove them, then run the migration again."
        )
    op.alter_column("tenants", "country", nullable=False, server_default="MY")
    op.create_check_constraint(op.f("ck_tenants_country"), "tenants", "country IN ('MY','SG','AU')")

    op.add_column("business_profiles", sa.Column("address_line1", sa.String(length=200), nullable=True))
    op.add_column("business_profiles", sa.Column("address_line2", sa.String(length=200), nullable=True))
    op.add_column("business_profiles", sa.Column("city", sa.String(length=100), nullable=True))
    op.add_column("business_profiles", sa.Column("state", sa.String(length=3), nullable=True))
    op.add_column("business_profiles", sa.Column("postcode", sa.String(length=10), nullable=True))


def downgrade() -> None:
    for column in ("postcode", "state", "city", "address_line2", "address_line1"):
        op.drop_column("business_profiles", column)
    op.drop_constraint(op.f("ck_tenants_country"), "tenants", type_="check")
    op.drop_column("tenants", "country")
