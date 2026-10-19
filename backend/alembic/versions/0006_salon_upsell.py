"""phase 5a: salon - add-on services, upsell items, retail products on the ticket

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("services", sa.Column("is_addon", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("booking_items", sa.Column("is_upsell", sa.Boolean(), server_default="false", nullable=False))

    op.create_table(
        "booking_products",
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_booking_products_quantity")),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], name=op.f("fk_booking_products_booking_id_bookings"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_booking_products_product_id_products")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_booking_products_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_booking_products")),
        sa.UniqueConstraint("booking_id", "product_id", name="uq_booking_products_booking_product"),
    )
    op.create_index(op.f("ix_booking_products_booking_id"), "booking_products", ["booking_id"], unique=False)
    op.create_index(op.f("ix_booking_products_tenant_id"), "booking_products", ["tenant_id"], unique=False)

    op.execute("ALTER TABLE booking_products ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE booking_products FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON booking_products "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.drop_table("booking_products")
    op.drop_column("booking_items", "is_upsell")
    op.drop_column("services", "is_addon")
