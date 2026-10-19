"""phase 5j: booking reminders - one row per (booking, reminder kind) so each goes out once

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "booking_reminders",
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], name=op.f("fk_booking_reminders_booking_id_bookings"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_booking_reminders_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("booking_id", "kind", name=op.f("pk_booking_reminders")),
    )
    op.create_index(op.f("ix_booking_reminders_tenant_id"), "booking_reminders", ["tenant_id"])
    op.execute("ALTER TABLE booking_reminders ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE booking_reminders FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON booking_reminders "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.drop_table("booking_reminders")
