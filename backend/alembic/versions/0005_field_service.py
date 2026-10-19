"""phase 4: field service - business location, service location types, follow-ups, overruns

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostGIS powers the on-site service-radius check. It is not a "trusted" extension, so a
    # non-superuser migration role cannot create it; in that case (or when it is not installed)
    # we continue and the radius check reports "postgis_unavailable" at runtime instead of the
    # whole migration failing. The postgis/postgis Docker image runs migrations as superuser.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'postgis') THEN
                BEGIN
                    CREATE EXTENSION IF NOT EXISTS postgis;
                EXCEPTION WHEN insufficient_privilege THEN
                    RAISE NOTICE 'postgis is available but this role cannot create it; enable it as a superuser';
                END;
            ELSE
                RAISE NOTICE 'postgis is not installed; on-site radius checks will be unavailable';
            END IF;
        END $$;
        """
    )

    op.add_column("tenants", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("tenants", sa.Column("longitude", sa.Float(), nullable=True))
    op.add_column("tenants", sa.Column("service_radius_km", sa.Float(), nullable=True))
    op.create_check_constraint(op.f("ck_tenants_latitude"), "tenants", "latitude IS NULL OR latitude BETWEEN -90 AND 90")
    op.create_check_constraint(op.f("ck_tenants_longitude"), "tenants", "longitude IS NULL OR longitude BETWEEN -180 AND 180")
    op.create_check_constraint(op.f("ck_tenants_service_radius_km"), "tenants", "service_radius_km IS NULL OR service_radius_km > 0")

    op.add_column("services", sa.Column("service_category", sa.String(20), nullable=True))
    op.add_column("services", sa.Column("location_type", sa.String(20), server_default="in_office", nullable=False))
    op.add_column("services", sa.Column("travel_buffer_minutes", sa.Integer(), server_default="0", nullable=False))
    op.create_check_constraint(
        op.f("ck_services_service_category"), "services", "service_category IS NULL OR service_category IN ('inspection','standard')"
    )
    op.create_check_constraint(op.f("ck_services_location_type"), "services", "location_type IN ('in_office','virtual','on_site')")
    op.create_check_constraint(op.f("ck_services_travel_buffer_minutes"), "services", "travel_buffer_minutes >= 0")

    op.add_column("bookings", sa.Column("parent_booking_id", sa.UUID(), nullable=True))
    op.add_column("bookings", sa.Column("actual_duration_minutes", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("schedule_conflict", sa.Boolean(), server_default="false", nullable=False))
    op.create_foreign_key(
        op.f("fk_bookings_parent_booking_id_bookings"), "bookings", "bookings", ["parent_booking_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(op.f("ix_bookings_parent_booking_id"), "bookings", ["parent_booking_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_bookings_parent_booking_id"), table_name="bookings")
    op.drop_constraint(op.f("fk_bookings_parent_booking_id_bookings"), "bookings", type_="foreignkey")
    for col in ("schedule_conflict", "actual_duration_minutes", "parent_booking_id"):
        op.drop_column("bookings", col)
    for name in ("ck_services_travel_buffer_minutes", "ck_services_location_type", "ck_services_service_category"):
        op.drop_constraint(op.f(name), "services", type_="check")
    for col in ("travel_buffer_minutes", "location_type", "service_category"):
        op.drop_column("services", col)
    for name in ("ck_tenants_service_radius_km", "ck_tenants_longitude", "ck_tenants_latitude"):
        op.drop_constraint(op.f(name), "tenants", type_="check")
    for col in ("service_radius_km", "longitude", "latitude"):
        op.drop_column("tenants", col)
    # The postgis extension is left in place: other objects may depend on it.
