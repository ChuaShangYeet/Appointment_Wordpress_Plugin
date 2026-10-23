"""a business can be in any country

Revision ID: 0034
Revises: 0033

``tenants.country`` was held to Malaysia, Singapore or Australia; it may now be any ISO 3166 code. The database checks only that it is two
capital letters: which codes are countries is ``app.core.countries``' to say (it lists every country the time-zone database knows).

``business_profiles.state`` held a state's three-letter code (``SGR``, ``NSW``). Outside Malaysia and Australia the state, province or region
is kept as written (``Ontario``, ``Île-de-France``), so the column grows to 100 characters.

Downgrading stops, and says which, while any business is outside the three countries or has a state longer than three characters.
"""
import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_tenants_country"), "tenants", type_="check")
    op.create_check_constraint(op.f("ck_tenants_country"), "tenants", "country ~ '^[A-Z]{2}$'")
    op.alter_column("business_profiles", "state", type_=sa.String(length=100), existing_type=sa.String(length=3), existing_nullable=True)


def downgrade() -> None:
    conn = op.get_bind()
    stray = conn.execute(sa.text(
        "SELECT t.name, t.country, p.state FROM tenants t LEFT JOIN business_profiles p ON p.tenant_id = t.id "
        "WHERE t.country NOT IN ('MY','SG','AU') OR length(p.state) > 3 ORDER BY t.name"
    )).all()
    if stray:
        listing = "; ".join(f"{name} ({country}, state {state!r})" for name, country, state in stray)
        raise RuntimeError(f"These businesses do not fit the rules before migration 0034 (Malaysia, Singapore or Australia, a state code of 3 letters): {listing}")
    op.alter_column("business_profiles", "state", type_=sa.String(length=3), existing_type=sa.String(length=100), existing_nullable=True)
    op.drop_constraint(op.f("ck_tenants_country"), "tenants", type_="check")
    op.create_check_constraint(op.f("ck_tenants_country"), "tenants", "country IN ('MY','SG','AU')")
