"""segment-by-segment qualification: booking_allocations.service_id

Revision ID: 0019
Revises: 0018

A workshop job order is made of segments, one per service, each with its own mechanic and bay. Recording which service a reservation is for
lets the review flags judge every segment against its own service. Existing rows: those of a workshop booking with exactly one service
are filled in (there is nothing to be ambiguous about); rows of multi-service job orders stay ``NULL`` and keep the lenient rule.
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("booking_allocations", sa.Column("service_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_booking_allocations_service_id_services"), "booking_allocations", "services", ["service_id"], ["id"], ondelete="SET NULL"
    )
    conn = op.get_bind()
    for (tenant_id,) in conn.execute(sa.text("SELECT id FROM tenants WHERE business_type = 'automotive'")).all():
        # the allocations table is row-level secured (and its owner is subject to it): one business at a time
        conn.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(sa.text(
            "UPDATE booking_allocations a SET service_id = one.service_id "
            "FROM (SELECT booking_id, min(service_id::text)::uuid AS service_id FROM booking_items WHERE NOT is_upsell "
            "      GROUP BY booking_id HAVING count(*) = 1) one "
            "WHERE one.booking_id = a.booking_id"
        ))
    conn.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))


def downgrade() -> None:
    op.drop_constraint(op.f("fk_booking_allocations_service_id_services"), "booking_allocations", type_="foreignkey")
    op.drop_column("booking_allocations", "service_id")
