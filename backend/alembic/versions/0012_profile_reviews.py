"""phase 5g: business profile and reviews

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
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
        "business_profiles",
        sa.Column("business_name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("contact_no", sa.String(32), nullable=True),
        sa.Column("contact_email", sa.String(254), nullable=True),
        sa.Column("logo_image", sa.Text(), nullable=True),
        sa.Column("gallery_images", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("social_media_links", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("google_maps_url", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_business_profiles_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_business_profiles")),
        sa.UniqueConstraint("tenant_id", name="uq_business_profiles_tenant"),
    )
    op.create_index(op.f("ix_business_profiles_tenant_id"), "business_profiles", ["tenant_id"])
    _rls("business_profiles")

    op.create_table(
        "reviews",
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("resource_id", sa.UUID(), nullable=True),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", sa.String(10), server_default="published", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name=op.f("ck_reviews_rating")),
        sa.CheckConstraint("status IN ('published','hidden')", name=op.f("ck_reviews_status")),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], name=op.f("fk_reviews_booking_id_bookings"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name=op.f("fk_reviews_customer_id_customers")),
        sa.ForeignKeyConstraint(["resource_id"], ["resources.id"], name=op.f("fk_reviews_resource_id_resources"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_reviews_tenant_id_tenants"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
        sa.UniqueConstraint("booking_id", name="uq_reviews_booking"),
    )
    op.create_index(op.f("ix_reviews_tenant_id"), "reviews", ["tenant_id"])
    op.create_index("ix_reviews_resource", "reviews", ["tenant_id", "resource_id", "created_at"])
    _rls("reviews")


def downgrade() -> None:
    op.drop_table("reviews")
    op.drop_table("business_profiles")
