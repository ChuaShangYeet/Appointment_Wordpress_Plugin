"""plans (free / paid) and tenants.plan

Revision ID: 0023
Revises: 0022

``plans`` is one table for the whole service (no tenant column, no row-level security): the limits of each plan, read by every request and changed only by
the operator (the application's database role may read it but not write it). ``free`` and ``paid`` are seeded; the free numbers are a first proposal, to be
changed with an ordinary UPDATE once pricing is decided. ``NULL`` means no limit, ``0`` none at all.

Every business that exists when this runs is put on ``paid``: nobody is limited by an upgrade of the software. New businesses set up by the operator are
``paid`` unless the request says ``free``.
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

LIMITS = ("max_resources", "max_services", "max_bookings_per_month", "max_whatsapp_per_month", "max_sms_per_month")


def upgrade() -> None:
    plans = op.create_table(
        "plans",
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        *(sa.Column(column, sa.Integer(), nullable=True) for column in LIMITS),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_plans")),
        *(sa.CheckConstraint(f"{column} IS NULL OR {column} >= 0", name=op.f(f"ck_plans_{column}")) for column in LIMITS),
    )
    op.bulk_insert(plans, [
        {"code": "free", "name": "Free", "max_resources": 3, "max_services": 10, "max_bookings_per_month": 50, "max_whatsapp_per_month": 20, "max_sms_per_month": 20},
        {"code": "paid", "name": "Paid", "max_resources": None, "max_services": None, "max_bookings_per_month": None, "max_whatsapp_per_month": None, "max_sms_per_month": None},
    ])
    # the application role may read the limits, never change them (no-op where that role does not exist)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'booking_app') THEN "
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE plans FROM booking_app; END IF; END $$"
    )
    op.add_column("tenants", sa.Column("plan", sa.String(length=20), nullable=False, server_default="paid"))
    op.create_foreign_key(op.f("fk_tenants_plan_plans"), "tenants", "plans", ["plan"], ["code"])


def downgrade() -> None:
    op.drop_constraint(op.f("fk_tenants_plan_plans"), "tenants", type_="foreignkey")
    op.drop_column("tenants", "plan")
    op.drop_table("plans")
