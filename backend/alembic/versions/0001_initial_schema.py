"""initial schema: Phase 1 scheduling core with GiST exclusion + RLS

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

#: Tables isolated per tenant by Row-Level Security. tenants/sites/api_keys are
#: excluded because they are read while resolving *which* tenant a request belongs to.
RLS_TABLES = ['booking_policies', 'customers', 'operating_hours', 'products', 'resources', 'supported_vehicles', 'customer_addresses', 'days_off', 'resource_shifts', 'services', 'vehicles', 'bookings', 'service_resources', 'booking_allocations', 'booking_items']


def upgrade() -> None:
    # Must run first: the exclusion constraint on booking_allocations mixes
    # equality on a UUID (btree) with range overlap (gist).
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    # gen_random_uuid() is core on PG13+; pgcrypto not required.

    op.create_table('tenants',
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('business_type', sa.String(length=20), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('default_policy_id', sa.UUID(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tenants'))
    )
    op.create_table('booking_policies',
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('default_status', sa.String(length=20), nullable=False),
    sa.Column('min_notice_booking', sa.Interval(), nullable=False),
    sa.Column('min_notice_cancel', sa.Interval(), nullable=False),
    sa.Column('booking_window', sa.Interval(), nullable=False),
    sa.Column('pending_timeout', sa.Interval(), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("default_status IN ('pending','confirmed')", name=op.f('ck_booking_policies_default_status')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_booking_policies_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_booking_policies'))
    )
    op.create_index(op.f('ix_booking_policies_tenant_id'), 'booking_policies', ['tenant_id'], unique=False)
    op.create_table('customers',
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('phone', sa.String(length=32), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_customers_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customers')),
    sa.UniqueConstraint('tenant_id', 'phone', name='uq_customers_tenant_phone')
    )
    op.create_index(op.f('ix_customers_tenant_id'), 'customers', ['tenant_id'], unique=False)
    op.create_table('operating_hours',
    sa.Column('weekday', sa.SmallInteger(), nullable=False),
    sa.Column('open_time', sa.Time(), nullable=False),
    sa.Column('close_time', sa.Time(), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.CheckConstraint('weekday BETWEEN 0 AND 6', name=op.f('ck_operating_hours_weekday')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_operating_hours_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_operating_hours'))
    )
    op.create_index(op.f('ix_operating_hours_tenant_id'), 'operating_hours', ['tenant_id'], unique=False)
    op.create_table('products',
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=True),
    sa.Column('price', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('stock_qty', sa.Integer(), server_default='0', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_products_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_products'))
    )
    op.create_index(op.f('ix_products_tenant_id'), 'products', ['tenant_id'], unique=False)
    op.create_table('resources',
    sa.Column('type', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('capacity', sa.Integer(), nullable=True),
    sa.Column('attributes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("status IN ('active','inactive')", name=op.f('ck_resources_status')),
    sa.CheckConstraint("type IN ('staff', 'table', 'bay')", name=op.f('ck_resources_type')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_resources_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resources'))
    )
    op.create_index(op.f('ix_resources_tenant_id'), 'resources', ['tenant_id'], unique=False)
    op.create_table('sites',
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('domain', sa.String(length=255), nullable=True),
    sa.Column('allowed_origins', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('webhook_secret_enc', sa.LargeBinary(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_sites_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sites'))
    )
    op.create_index(op.f('ix_sites_tenant_id'), 'sites', ['tenant_id'], unique=False)
    op.create_table('supported_vehicles',
    sa.Column('make', sa.String(length=80), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('year_from', sa.Integer(), nullable=True),
    sa.Column('year_to', sa.Integer(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_supported_vehicles_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_supported_vehicles'))
    )
    op.create_index(op.f('ix_supported_vehicles_tenant_id'), 'supported_vehicles', ['tenant_id'], unique=False)
    op.create_table('api_keys',
    sa.Column('site_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('prefix', sa.String(length=8), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_api_keys_site_id_sites'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_api_keys_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_keys'))
    )
    op.create_index(op.f('ix_api_keys_key_hash'), 'api_keys', ['key_hash'], unique=True)
    op.create_index(op.f('ix_api_keys_tenant_id'), 'api_keys', ['tenant_id'], unique=False)
    op.create_table('customer_addresses',
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('line1', sa.Text(), nullable=False),
    sa.Column('city', sa.String(length=100), nullable=True),
    sa.Column('postcode', sa.String(length=16), nullable=True),
    sa.Column('latitude', sa.Double(), nullable=True),
    sa.Column('longitude', sa.Double(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_customer_addresses_customer_id_customers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_customer_addresses_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customer_addresses'))
    )
    op.create_index(op.f('ix_customer_addresses_tenant_id'), 'customer_addresses', ['tenant_id'], unique=False)
    op.create_table('days_off',
    sa.Column('resource_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=False),
    sa.Column('repeat_yearly', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.CheckConstraint('end_date >= start_date', name=op.f('ck_days_off_span')),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], name=op.f('fk_days_off_resource_id_resources'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_days_off_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_days_off'))
    )
    op.create_index(op.f('ix_days_off_resource_id'), 'days_off', ['resource_id'], unique=False)
    op.create_index(op.f('ix_days_off_tenant_id'), 'days_off', ['tenant_id'], unique=False)
    op.create_table('resource_shifts',
    sa.Column('resource_id', sa.UUID(), nullable=False),
    sa.Column('weekday', sa.SmallInteger(), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=False),
    sa.Column('end_time', sa.Time(), nullable=False),
    sa.Column('break_start', sa.Time(), nullable=True),
    sa.Column('break_end', sa.Time(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.CheckConstraint('(break_start IS NULL AND break_end IS NULL) OR (break_start IS NOT NULL AND break_end IS NOT NULL AND break_start < break_end AND break_start >= start_time AND break_end <= end_time)', name=op.f('ck_resource_shifts_break_within_shift')),
    sa.CheckConstraint('end_time > start_time', name=op.f('ck_resource_shifts_span')),
    sa.CheckConstraint('weekday BETWEEN 0 AND 6', name=op.f('ck_resource_shifts_weekday')),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], name=op.f('fk_resource_shifts_resource_id_resources'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_resource_shifts_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resource_shifts'))
    )
    op.create_index(op.f('ix_resource_shifts_resource_id'), 'resource_shifts', ['resource_id'], unique=False)
    op.create_index(op.f('ix_resource_shifts_tenant_id'), 'resource_shifts', ['tenant_id'], unique=False)
    op.create_table('services',
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('duration_minutes', sa.Integer(), nullable=False),
    sa.Column('buffer_minutes', sa.Integer(), server_default='0', nullable=False),
    sa.Column('price', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('policy_id', sa.UUID(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['policy_id'], ['booking_policies.id'], name=op.f('fk_services_policy_id_booking_policies'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_services_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_services'))
    )
    op.create_index(op.f('ix_services_tenant_id'), 'services', ['tenant_id'], unique=False)
    op.create_table('vehicles',
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('plate_no', sa.String(length=20), nullable=False),
    sa.Column('make', sa.String(length=80), nullable=True),
    sa.Column('model', sa.String(length=80), nullable=True),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_vehicles_customer_id_customers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_vehicles_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vehicles'))
    )
    op.create_index(op.f('ix_vehicles_tenant_id'), 'vehicles', ['tenant_id'], unique=False)
    op.create_table('bookings',
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('policy_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('party_size', sa.Integer(), nullable=True),
    sa.Column('vehicle_id', sa.UUID(), nullable=True),
    sa.Column('address_id', sa.UUID(), nullable=True),
    sa.Column('meeting_link', sa.Text(), nullable=True),
    sa.Column('special_request', sa.Text(), nullable=True),
    sa.Column('health_notes_enc', sa.LargeBinary(), nullable=True),
    sa.Column('hold_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source', sa.String(length=20), server_default='headless', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'confirmed', 'in-progress', 'waiting-for-parts', 'completed', 'cancelled', 'rejected', 'no-show')", name=op.f('ck_bookings_status')),
    sa.CheckConstraint('end_at > start_at', name=op.f('ck_bookings_span')),
    sa.ForeignKeyConstraint(['address_id'], ['customer_addresses.id'], name=op.f('fk_bookings_address_id_customer_addresses')),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_bookings_customer_id_customers')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_bookings_tenant_id_tenants'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], name=op.f('fk_bookings_vehicle_id_vehicles')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bookings'))
    )
    op.create_index(op.f('ix_bookings_customer_id'), 'bookings', ['customer_id'], unique=False)
    op.create_index(op.f('ix_bookings_start_at'), 'bookings', ['start_at'], unique=False)
    op.create_index(op.f('ix_bookings_tenant_id'), 'bookings', ['tenant_id'], unique=False)
    op.create_table('service_resources',
    sa.Column('service_id', sa.UUID(), nullable=False),
    sa.Column('resource_id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], name=op.f('fk_service_resources_resource_id_resources'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], name=op.f('fk_service_resources_service_id_services'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_service_resources_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('service_id', 'resource_id', name=op.f('pk_service_resources'))
    )
    op.create_index(op.f('ix_service_resources_tenant_id'), 'service_resources', ['tenant_id'], unique=False)
    op.create_table('booking_allocations',
    sa.Column('booking_id', sa.UUID(), nullable=False),
    sa.Column('resource_id', sa.UUID(), nullable=False),
    sa.Column('period', postgresql.TSTZRANGE(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    postgresql.ExcludeConstraint((sa.column('resource_id'), '='), (sa.column('period'), '&&'), where=sa.text("state IN ('held', 'active')"), using='gist', name='booking_allocations_no_overlap'),
    sa.CheckConstraint("state IN ('held', 'active', 'released')", name=op.f('ck_booking_allocations_state')),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], name=op.f('fk_booking_allocations_booking_id_bookings'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], name=op.f('fk_booking_allocations_resource_id_resources')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_booking_allocations_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_booking_allocations'))
    )
    op.create_index(op.f('ix_booking_allocations_booking_id'), 'booking_allocations', ['booking_id'], unique=False)
    op.create_index(op.f('ix_booking_allocations_resource_id'), 'booking_allocations', ['resource_id'], unique=False)
    op.create_index(op.f('ix_booking_allocations_tenant_id'), 'booking_allocations', ['tenant_id'], unique=False)
    op.create_table('booking_items',
    sa.Column('booking_id', sa.UUID(), nullable=False),
    sa.Column('service_id', sa.UUID(), nullable=False),
    sa.Column('duration_minutes', sa.Integer(), nullable=False),
    sa.Column('buffer_minutes', sa.Integer(), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], name=op.f('fk_booking_items_booking_id_bookings'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], name=op.f('fk_booking_items_service_id_services')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_booking_items_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_booking_items'))
    )
    op.create_index(op.f('ix_booking_items_booking_id'), 'booking_items', ['booking_id'], unique=False)
    op.create_index(op.f('ix_booking_items_tenant_id'), 'booking_items', ['tenant_id'], unique=False)

    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so the policy also binds the table owner (the app's DB role).
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
            f"WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        )


def downgrade() -> None:
    for table in ['booking_items', 'booking_allocations', 'service_resources', 'bookings', 'vehicles', 'services', 'resource_shifts', 'days_off', 'customer_addresses', 'api_keys', 'supported_vehicles', 'sites', 'resources', 'products', 'operating_hours', 'customers', 'booking_policies', 'tenants']:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
