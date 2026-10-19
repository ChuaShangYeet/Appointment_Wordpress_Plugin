"""phase 3: automotive - vehicle compatibility, parts lead time, service parts

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vehicles", sa.Column("supported_vehicle_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_vehicles_supported_vehicle_id_supported_vehicles"),
        "vehicles", "supported_vehicles", ["supported_vehicle_id"], ["id"], ondelete="SET NULL",
    )
    op.create_unique_constraint("uq_vehicles_customer_plate", "vehicles", ["tenant_id", "customer_id", "plate_no"])

    op.add_column("products", sa.Column("category", sa.String(80), nullable=True))
    op.add_column("products", sa.Column("stock_status", sa.String(20), server_default="in_stock", nullable=False))
    op.add_column("products", sa.Column("estimated_lead_time_days", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_products_stock_status"), "products",
        "stock_status IN ('in_stock','out_of_stock','available_on_order')",
    )

    op.create_table(
        "service_products",
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_service_products_product_id_products"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], name=op.f("fk_service_products_service_id_services"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_service_products_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("service_id", "product_id", name=op.f("pk_service_products")),
    )
    op.create_index(op.f("ix_service_products_tenant_id"), "service_products", ["tenant_id"], unique=False)

    op.add_column("bookings", sa.Column("lead_time_acknowledged_at", sa.DateTime(timezone=True), nullable=True))

    op.execute("ALTER TABLE service_products ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE service_products FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON service_products "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.drop_column("bookings", "lead_time_acknowledged_at")
    op.drop_table("service_products")
    op.drop_constraint(op.f("ck_products_stock_status"), "products", type_="check")
    for col in ("estimated_lead_time_days", "stock_status", "category"):
        op.drop_column("products", col)
    op.drop_constraint("uq_vehicles_customer_plate", "vehicles", type_="unique")
    op.drop_constraint(op.f("fk_vehicles_supported_vehicle_id_supported_vehicles"), "vehicles", type_="foreignkey")
    op.drop_column("vehicles", "supported_vehicle_id")
