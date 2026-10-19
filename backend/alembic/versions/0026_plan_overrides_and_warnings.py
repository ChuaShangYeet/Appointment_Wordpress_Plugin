"""per-business plan overrides, and the record of usage warnings already sent

Revision ID: 0026
Revises: 0025

``tenants.plan_overrides``: limits set for one business that differ from its plan (``{"resources": 10, "sms_per_month": null}``; ``null`` = no limit, a missing key = the
plan's own number). ``{}`` for everyone to begin with, so nothing changes.

``plan_warnings``: one row each time a business is told it is near (or at) a limit, so that it is told once per limit, period, level and limit value - not at every booking.
A tenant table with the usual Row-Level Security.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("plan_overrides", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.create_table(
        "plan_warnings",
        sa.Column("limit_name", sa.String(40), nullable=False),
        sa.Column("period", sa.String(10), nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("allowed", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("level IN ('near','reached')", name=op.f("ck_plan_warnings_level")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_plan_warnings_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_warnings")),
        sa.UniqueConstraint("tenant_id", "limit_name", "period", "level", "allowed", name="uq_plan_warnings_once"),
    )
    op.create_index(op.f("ix_plan_warnings_tenant_id"), "plan_warnings", ["tenant_id"])
    op.execute("ALTER TABLE plan_warnings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE plan_warnings FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON plan_warnings "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.drop_table("plan_warnings")
    op.drop_column("tenants", "plan_overrides")
