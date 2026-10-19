"""phase 5i: notification templates and the notification log

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
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


def _base() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "notification_templates",
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("channel", sa.String(12), nullable=False),
        sa.Column("subject", sa.String(200), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("external_template", sa.String(100), nullable=True),
        *_base(),
        sa.CheckConstraint("channel IN ('email','sms','whatsapp')", name=op.f("ck_notification_templates_channel")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_notification_templates_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_templates")),
        sa.UniqueConstraint("tenant_id", "event", "channel", name="uq_notification_templates_event_channel"),
    )
    op.create_index(op.f("ix_notification_templates_tenant_id"), "notification_templates", ["tenant_id"])
    _rls("notification_templates")

    op.create_table(
        "notification_log",
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("channel", sa.String(12), nullable=False),
        sa.Column("outbox_event_id", sa.UUID(), nullable=True),
        sa.Column("booking_id", sa.UUID(), nullable=True),
        sa.Column("customer_id", sa.UUID(), nullable=True),
        sa.Column("recipient", sa.String(80), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("provider", sa.String(40), nullable=True),
        sa.Column("provider_message_id", sa.String(160), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_base(),
        sa.CheckConstraint("status IN ('sent','failed','skipped')", name=op.f("ck_notification_log_status")),
        sa.CheckConstraint("channel IN ('email','sms','whatsapp')", name=op.f("ck_notification_log_channel")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_notification_log_tenant_id_tenants"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], name=op.f("fk_notification_log_booking_id_bookings"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name=op.f("fk_notification_log_customer_id_customers"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_log")),
    )
    op.create_index(op.f("ix_notification_log_tenant_id"), "notification_log", ["tenant_id"])
    op.create_index("ix_notification_log_booking", "notification_log", ["booking_id"])
    op.create_index(
        "uq_notification_log_event_channel", "notification_log", ["outbox_event_id", "channel"], unique=True,
        postgresql_where=sa.text("outbox_event_id IS NOT NULL"),
    )
    _rls("notification_log")


def downgrade() -> None:
    op.drop_table("notification_log")
    op.drop_table("notification_templates")
