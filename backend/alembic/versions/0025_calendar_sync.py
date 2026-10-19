"""Google Calendar sync: calendar_connections and calendar_events

Revision ID: 0025
Revises: 0024

A business connects its Google calendar (or a staff member theirs); each booking then has one calendar event per connected calendar it belongs to.
Tokens are stored encrypted. Both tables are tenant tables with the usual Row-Level Security.
"""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
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
    op.create_table(
        "calendar_connections",
        sa.Column("resource_id", sa.UUID(), nullable=True),
        sa.Column("calendar_id", sa.String(254), server_default="primary", nullable=False),
        sa.Column("account_label", sa.String(200), nullable=True),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("share_customer_details", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('active','needs_reconnect')", name=op.f("ck_calendar_connections_status")),
        sa.ForeignKeyConstraint(["resource_id"], ["resources.id"], name=op.f("fk_calendar_connections_resource_id_resources"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_calendar_connections_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_connections")),
    )
    op.create_index(op.f("ix_calendar_connections_tenant_id"), "calendar_connections", ["tenant_id"])
    op.create_index("uq_calendar_connections_business", "calendar_connections", ["tenant_id"], unique=True, postgresql_where=sa.text("resource_id IS NULL"))
    op.create_index("uq_calendar_connections_staff", "calendar_connections", ["tenant_id", "resource_id"], unique=True, postgresql_where=sa.text("resource_id IS NOT NULL"))
    _rls("calendar_connections")

    op.create_table(
        "calendar_events",
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("external_event_id", sa.String(100), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("state", sa.String(10), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("state IN ('pending','synced','failed')", name=op.f("ck_calendar_events_state")),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], name=op.f("fk_calendar_events_booking_id_bookings"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["calendar_connections.id"], name=op.f("fk_calendar_events_connection_id_calendar_connections"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_calendar_events_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_events")),
        sa.UniqueConstraint("connection_id", "booking_id", name="uq_calendar_events_connection_booking"),
    )
    op.create_index(op.f("ix_calendar_events_tenant_id"), "calendar_events", ["tenant_id"])
    op.create_index(op.f("ix_calendar_events_booking_id"), "calendar_events", ["booking_id"])
    op.create_index("ix_calendar_events_due", "calendar_events", ["next_attempt_at"], postgresql_where=sa.text("state = 'pending'"))
    _rls("calendar_events")


def downgrade() -> None:
    op.drop_table("calendar_events")
    op.drop_table("calendar_connections")
