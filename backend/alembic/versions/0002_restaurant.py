"""phase 2: restaurant time-slot settings, table open rules, reservation columns

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

NEW_RLS_TABLES = ("time_slot_settings", "table_open_rules")


def upgrade() -> None:
    op.create_table(
        "time_slot_settings",
        sa.Column("booking_mode", sa.String(10), nullable=False),
        sa.Column("slot_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("slot_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("first_slot_time", sa.Time(), nullable=False),
        sa.Column("last_slot_time", sa.Time(), nullable=False),
        sa.Column("buffer_minutes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("policy_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("booking_mode IN ('flexible','fixed')", name=op.f("ck_time_slot_settings_booking_mode")),
        sa.CheckConstraint(
            "slot_duration_minutes > 0 AND slot_interval_minutes > 0 AND buffer_minutes >= 0",
            name=op.f("ck_time_slot_settings_positive"),
        ),
        sa.CheckConstraint("first_slot_time <= last_slot_time", name=op.f("ck_time_slot_settings_slot_order")),
        sa.ForeignKeyConstraint(["policy_id"], ["booking_policies.id"], name=op.f("fk_time_slot_settings_policy_id_booking_policies"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_time_slot_settings_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_time_slot_settings")),
        sa.UniqueConstraint("tenant_id", name="uq_time_slot_settings_tenant"),
    )
    op.create_index(op.f("ix_time_slot_settings_tenant_id"), "time_slot_settings", ["tenant_id"], unique=False)

    op.create_table(
        "table_open_rules",
        sa.Column("weekday", sa.SmallInteger(), nullable=True),
        sa.Column("specific_date", sa.Date(), nullable=True),
        sa.Column("table_type", sa.String(10), server_default="normal", nullable=False),
        sa.Column("open_tables", sa.Integer(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("(weekday IS NULL) <> (specific_date IS NULL)", name=op.f("ck_table_open_rules_weekday_xor_date")),
        sa.CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6", name=op.f("ck_table_open_rules_weekday")),
        sa.CheckConstraint("table_type IN ('normal','vip')", name=op.f("ck_table_open_rules_table_type")),
        sa.CheckConstraint("open_tables >= 0", name=op.f("ck_table_open_rules_open_tables")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_table_open_rules_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_open_rules")),
    )
    op.create_index(op.f("ix_table_open_rules_tenant_id"), "table_open_rules", ["tenant_id"], unique=False)

    op.add_column("bookings", sa.Column("table_type", sa.String(10), nullable=True))
    op.add_column("bookings", sa.Column("allow_separate_seating", sa.Boolean(), nullable=True))
    op.add_column("bookings", sa.Column("minimum_spend_agreed", sa.Numeric(10, 2), nullable=True))
    op.create_check_constraint(
        op.f("ck_bookings_table_type"), "bookings", "table_type IS NULL OR table_type IN ('normal','vip')"
    )

    for table in NEW_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
            f"WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_bookings_table_type"), "bookings", type_="check")
    for col in ("minimum_spend_agreed", "allow_separate_seating", "table_type"):
        op.drop_column("bookings", col)
    op.drop_table("table_open_rules")
    op.drop_table("time_slot_settings")
