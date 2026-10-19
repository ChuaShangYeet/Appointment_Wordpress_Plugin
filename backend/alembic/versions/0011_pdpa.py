"""phase 5f: PDPA - audit log (append-only), data access/erasure requests, erased marker on customers

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.add_column("customers", sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "audit_log",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.Column("actor_type", sa.String(10), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=True),
        sa.Column("role", sa.String(10), nullable=True),
        sa.Column("action", sa.String(160), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("detail", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("actor_type IN ('user','system')", name=op.f("ck_audit_log_actor_type")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_audit_log_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_tenant_seq", "audit_log", ["tenant_id", "seq"])
    op.create_index(op.f("ix_audit_log_tenant_id"), "audit_log", ["tenant_id"])
    _rls("audit_log")
    # Append-only. A row deleted *because its tenant was deleted* (a cascade) runs at trigger depth 1 and is allowed.
    op.execute(
        "CREATE FUNCTION audit_log_is_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'audit_log is append-only'; END $$"
    )
    op.execute(
        "CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW "
        "WHEN (pg_trigger_depth() = 0) EXECUTE FUNCTION audit_log_is_append_only()"
    )

    op.create_table(
        "data_requests",
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("status", sa.String(10), server_default="pending", nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_by", sa.String(64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("kind IN ('access','erasure')", name=op.f("ck_data_requests_kind")),
        sa.CheckConstraint("status IN ('pending','completed','rejected')", name=op.f("ck_data_requests_status")),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name=op.f("fk_data_requests_customer_id_customers")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_data_requests_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_requests")),
    )
    op.create_index(op.f("ix_data_requests_customer_id"), "data_requests", ["customer_id"])
    op.create_index(op.f("ix_data_requests_tenant_id"), "data_requests", ["tenant_id"])
    op.create_index("uq_data_requests_open", "data_requests", ["customer_id", "kind"], unique=True, postgresql_where=sa.text("status = 'pending'"))
    _rls("data_requests")


def downgrade() -> None:
    op.drop_table("data_requests")
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.drop_table("audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_is_append_only()")
    op.drop_column("customers", "erased_at")
