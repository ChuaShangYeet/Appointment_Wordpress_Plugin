<?php
namespace JustSimple\Booking\Data;

defined( 'ABSPATH' ) || exit;

/**
 * The plugin's tables, ported from the engine's models (backend/app/models) with three changes:
 *
 * - No tenant_id: one WordPress site is one business. Its booking modules take the tenant's place (module_id).
 * - Integer ids instead of UUIDs, as WordPress does; bookings carry a random public `code` for links and messages.
 * - No exclusion constraint on booking_allocations (MySQL has none). Double booking is stopped in BookingService
 *   with GET_LOCK per module and day plus an overlap re-check inside a transaction, which needs InnoDB.
 *
 * All instants are stored in UTC (`*_utc` columns); times of day (shifts, hours) are wall-clock in the site's time zone.
 * dbDelta rules apply to every statement: one column per line, two spaces after PRIMARY KEY, KEY not INDEX, no foreign keys.
 */
final class Schema {

	const VERSION = 1;
	const OPTION  = 'jsb_db_version';

	/** Every table, without prefix. Order matters only for readability; there are no foreign keys. */
	const TABLES = array(
		'modules',
		'policies',
		'operating_hours',
		'slot_settings',
		'table_open_rules',
		'resources',
		'shifts',
		'days_off',
		'services',
		'service_resources',
		'products',
		'service_products',
		'supported_vehicles',
		'customers',
		'customer_addresses',
		'vehicles',
		'bookings',
		'booking_items',
		'booking_products',
		'booking_allocations',
		'status_history',
	);

	/** Module types: one per engine allocator (backend/app/services/allocators). */
	const MODULE_TYPES = array( 'staff', 'salon', 'restaurant', 'workshop', 'field' );

	const BOOKING_STATUSES = array( 'pending', 'confirmed', 'in-progress', 'waiting-for-parts', 'completed', 'cancelled', 'rejected', 'no-show' );

	public static function table( $name ) {
		global $wpdb;
		return $wpdb->prefix . 'jsb_' . $name;
	}

	public static function maybe_upgrade() {
		if ( (int) get_option( self::OPTION, 0 ) < self::VERSION ) {
			self::install();
		}
	}

	public static function install() {
		require_once ABSPATH . 'wp-admin/includes/upgrade.php';
		foreach ( self::statements() as $sql ) {
			dbDelta( $sql );
		}
		update_option( self::OPTION, self::VERSION, false );
	}

	/** Used by uninstall.php only, and only when the owner asked for their data to be deleted. */
	public static function drop_all() {
		global $wpdb;
		foreach ( self::TABLES as $name ) {
			$wpdb->query( 'DROP TABLE IF EXISTS ' . self::table( $name ) ); // phpcs:ignore WordPress.DB.PreparedSQL.NotPrepared -- table names are constants
		}
		delete_option( self::OPTION );
	}

	/** @return array<string,string> table name (without prefix) => CREATE TABLE statement */
	public static function statements() {
		global $wpdb;
		$c = $wpdb->get_charset_collate();
		$t = array();
		foreach ( self::TABLES as $name ) {
			$t[ $name ] = self::table( $name );
		}

		return array(
			// A booking page an owner sets up: one type, its own services, resources, policy and look (settings JSON).
			'modules'             => "CREATE TABLE {$t['modules']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  title varchar(150) NOT NULL,
  type varchar(20) NOT NULL,
  status varchar(20) NOT NULL DEFAULT 'draft',
  default_policy_id bigint(20) unsigned DEFAULT NULL,
  settings longtext NOT NULL,
  created_utc datetime NOT NULL,
  updated_utc datetime NOT NULL,
  PRIMARY KEY  (id),
  KEY status (status)
) $c;",

			'policies'            => "CREATE TABLE {$t['policies']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  module_id bigint(20) unsigned NOT NULL,
  name varchar(100) NOT NULL,
  default_status varchar(20) NOT NULL DEFAULT 'confirmed',
  min_notice_booking_minutes int(11) NOT NULL DEFAULT 0,
  min_notice_cancel_minutes int(11) NOT NULL DEFAULT 0,
  booking_window_days int(11) NOT NULL DEFAULT 60,
  pending_timeout_minutes int(11) NOT NULL DEFAULT 30,
  created_utc datetime NOT NULL,
  updated_utc datetime NOT NULL,
  PRIMARY KEY  (id),
  KEY module_id (module_id)
) $c;",

			// No rows for a module = no opening-hours limit; shifts decide. close_time before open_time runs past midnight.
			'operating_hours'     => "CREATE TABLE {$t['operating_hours']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  module_id bigint(20) unsigned NOT NULL,
  weekday tinyint(1) unsigned NOT NULL,
  open_time time NOT NULL,
  close_time time NOT NULL,
  PRIMARY KEY  (id),
  KEY module_weekday (module_id,weekday)
) $c;",

			// Restaurant slot rules, one row per restaurant module.
			'slot_settings'       => "CREATE TABLE {$t['slot_settings']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  module_id bigint(20) unsigned NOT NULL,
  booking_mode varchar(10) NOT NULL DEFAULT 'flexible',
  slot_duration_minutes int(11) NOT NULL,
  slot_interval_minutes int(11) NOT NULL,
  first_slot_time time NOT NULL,
  last_slot_time time NOT NULL,
  buffer_minutes int(11) NOT NULL DEFAULT 0,
  allow_table_selection tinyint(1) NOT NULL DEFAULT 0,
  policy_id bigint(20) unsigned DEFAULT NULL,
  PRIMARY KEY  (id),
  UNIQUE KEY module_id (module_id)
) $c;",

			// Exactly one of weekday / specific_date is set; a date rule overrides a weekday rule.
			'table_open_rules'    => "CREATE TABLE {$t['table_open_rules']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  module_id bigint(20) unsigned NOT NULL,
  weekday tinyint(1) unsigned DEFAULT NULL,
  specific_date date DEFAULT NULL,
  table_type varchar(10) NOT NULL DEFAULT 'normal',
  open_tables int(11) NOT NULL,
  PRIMARY KEY  (id),
  KEY module_id (module_id)
) $c;",

			// Staff member, table or bay. wp_user_id links a staff member to a WordPress login so they see their own schedule.
			'resources'           => "CREATE TABLE {$t['resources']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  module_id bigint(20) unsigned NOT NULL,
  type varchar(20) NOT NULL,
  name varchar(150) NOT NULL,
  status varchar(20) NOT NULL DEFAULT 'active',
  capacity int(11) DEFAULT NULL,
  attributes longtext NOT NULL,
  contact_email varchar(254) DEFAULT NULL,
  contact_phone varchar(32) DEFAULT NULL,
  meeting_link text DEFAULT NULL,
  wp_user_id bigint(20) unsigned DEFAULT NULL,
  sort_order int(11) NOT NULL DEFAULT 0,
  created_utc datetime NOT NULL,
  updated_utc datetime NOT NULL,
  PRIMARY KEY  (id),
  KEY module_type (module_id,type,status),
  KEY wp_user_id (wp_user_id)
) $c;",

			// weekday 0 = Sunday .. 6 = Saturday, as in the engine. end_time before start_time is an overnight shift.
			'shifts'              => "CREATE TABLE {$t['shifts']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  resource_id bigint(20) unsigned NOT NULL,
  weekday tinyint(1) unsigned NOT NULL,
  start_time time NOT NULL,
  end_time time NOT NULL,
  break_start time DEFAULT NULL,
  break_end time DEFAULT NULL,
  PRIMARY KEY  (id),
  KEY resource_weekday (resource_id,weekday)
) $c;",

			// resource_id NULL = the whole module is closed (holiday); otherwise personal leave.
			'days_off'            => "CREATE TABLE {$t['days_off']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  module_id bigint(20) unsigned NOT NULL,
  resource_id bigint(20) unsigned DEFAULT NULL,
  name varchar(150) NOT NULL,
  start_date date NOT NULL,
  end_date date NOT NULL,
  repeat_yearly tinyint(1) NOT NULL DEFAULT 0,
  PRIMARY KEY  (id),
  KEY module_dates (module_id,start_date,end_date),
  KEY resource_id (resource_id)
) $c;",

			'services'            => "CREATE TABLE {$t['services']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  module_id bigint(20) unsigned NOT NULL,
  name varchar(150) NOT NULL,
  description text DEFAULT NULL,
  duration_minutes int(11) NOT NULL,
  buffer_minutes int(11) NOT NULL DEFAULT 0,
  price decimal(10,2) NOT NULL DEFAULT 0.00,
  price_after_inspection tinyint(1) NOT NULL DEFAULT 0,
  policy_id bigint(20) unsigned DEFAULT NULL,
  active tinyint(1) NOT NULL DEFAULT 1,
  is_addon tinyint(1) NOT NULL DEFAULT 0,
  service_category varchar(20) DEFAULT NULL,
  location_type varchar(20) NOT NULL DEFAULT 'in_office',
  travel_buffer_minutes int(11) NOT NULL DEFAULT 0,
  sort_order int(11) NOT NULL DEFAULT 0,
  created_utc datetime NOT NULL,
  updated_utc datetime NOT NULL,
  PRIMARY KEY  (id),
  KEY module_active (module_id,active)
) $c;",

			// Who may do a service. No rows for a service = any active staff of its module.
			'service_resources'   => "CREATE TABLE {$t['service_resources']} (
  service_id bigint(20) unsigned NOT NULL,
  resource_id bigint(20) unsigned NOT NULL,
  PRIMARY KEY  (service_id,resource_id),
  KEY resource_id (resource_id)
) $c;",

			// Salon retail products and workshop parts.
			'products'            => "CREATE TABLE {$t['products']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  module_id bigint(20) unsigned NOT NULL,
  name varchar(150) NOT NULL,
  category varchar(80) DEFAULT NULL,
  stock_status varchar(20) NOT NULL DEFAULT 'in_stock',
  estimated_lead_time_days int(11) DEFAULT NULL,
  sku varchar(64) DEFAULT NULL,
  price decimal(10,2) NOT NULL DEFAULT 0.00,
  stock_qty int(11) NOT NULL DEFAULT 0,
  PRIMARY KEY  (id),
  KEY module_id (module_id)
) $c;",

			'service_products'    => "CREATE TABLE {$t['service_products']} (
  service_id bigint(20) unsigned NOT NULL,
  product_id bigint(20) unsigned NOT NULL,
  PRIMARY KEY  (service_id,product_id),
  KEY product_id (product_id)
) $c;",

			'supported_vehicles'  => "CREATE TABLE {$t['supported_vehicles']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  module_id bigint(20) unsigned NOT NULL,
  make varchar(80) NOT NULL,
  model varchar(80) NOT NULL,
  year_from smallint(4) unsigned DEFAULT NULL,
  year_to smallint(4) unsigned DEFAULT NULL,
  PRIMARY KEY  (id),
  KEY module_id (module_id)
) $c;",

			// Site-wide: the same person may book the salon and the restaurant on one site.
			'customers'           => "CREATE TABLE {$t['customers']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  name varchar(150) NOT NULL,
  phone varchar(32) NOT NULL,
  email varchar(254) DEFAULT NULL,
  email_verified_utc datetime DEFAULT NULL,
  gender varchar(10) DEFAULT NULL,
  erased_utc datetime DEFAULT NULL,
  created_utc datetime NOT NULL,
  updated_utc datetime NOT NULL,
  PRIMARY KEY  (id),
  UNIQUE KEY phone (phone),
  KEY email (email(191))
) $c;",

			'customer_addresses'  => "CREATE TABLE {$t['customer_addresses']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  customer_id bigint(20) unsigned NOT NULL,
  line1 text NOT NULL,
  city varchar(100) DEFAULT NULL,
  postcode varchar(16) DEFAULT NULL,
  latitude decimal(9,6) DEFAULT NULL,
  longitude decimal(9,6) DEFAULT NULL,
  created_utc datetime NOT NULL,
  PRIMARY KEY  (id),
  KEY customer_id (customer_id)
) $c;",

			'vehicles'            => "CREATE TABLE {$t['vehicles']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  customer_id bigint(20) unsigned NOT NULL,
  supported_vehicle_id bigint(20) unsigned DEFAULT NULL,
  plate_no varchar(20) NOT NULL,
  make varchar(80) DEFAULT NULL,
  model varchar(80) DEFAULT NULL,
  year smallint(4) unsigned DEFAULT NULL,
  created_utc datetime NOT NULL,
  PRIMARY KEY  (id),
  UNIQUE KEY customer_plate (customer_id,plate_no)
) $c;",

			// code: the public reference shown to the customer. manage_token_hash: sha256 of the secret in the manage link.
			'bookings'            => "CREATE TABLE {$t['bookings']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  code varchar(12) NOT NULL,
  manage_token_hash char(64) NOT NULL,
  module_id bigint(20) unsigned NOT NULL,
  customer_id bigint(20) unsigned NOT NULL,
  contact_email varchar(254) DEFAULT NULL,
  idempotency_key varchar(100) DEFAULT NULL,
  idempotency_hash char(64) DEFAULT NULL,
  status varchar(30) NOT NULL,
  start_utc datetime NOT NULL,
  end_utc datetime NOT NULL,
  policy_snapshot longtext NOT NULL,
  party_size int(11) DEFAULT NULL,
  vehicle_id bigint(20) unsigned DEFAULT NULL,
  address_id bigint(20) unsigned DEFAULT NULL,
  meeting_link text DEFAULT NULL,
  special_request text DEFAULT NULL,
  health_notes_enc blob DEFAULT NULL,
  hold_expires_utc datetime DEFAULT NULL,
  source varchar(20) NOT NULL DEFAULT 'widget',
  table_type varchar(10) DEFAULT NULL,
  allow_separate_seating tinyint(1) DEFAULT NULL,
  minimum_spend_agreed decimal(10,2) DEFAULT NULL,
  lead_time_acknowledged_utc datetime DEFAULT NULL,
  parent_booking_id bigint(20) unsigned DEFAULT NULL,
  actual_duration_minutes int(11) DEFAULT NULL,
  schedule_conflict tinyint(1) NOT NULL DEFAULT 0,
  flags longtext DEFAULT NULL,
  created_utc datetime NOT NULL,
  updated_utc datetime NOT NULL,
  PRIMARY KEY  (id),
  UNIQUE KEY code (code),
  UNIQUE KEY module_idempotency (module_id,idempotency_key),
  KEY module_start (module_id,start_utc),
  KEY status_hold (status,hold_expires_utc),
  KEY customer_id (customer_id),
  KEY contact_email (contact_email(191)),
  KEY parent_booking_id (parent_booking_id)
) $c;",

			// A service on a booking, with its duration frozen at booking time; seq orders a workshop's stacked segments.
			'booking_items'       => "CREATE TABLE {$t['booking_items']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  booking_id bigint(20) unsigned NOT NULL,
  service_id bigint(20) unsigned NOT NULL,
  duration_minutes int(11) NOT NULL,
  buffer_minutes int(11) NOT NULL DEFAULT 0,
  seq int(11) NOT NULL DEFAULT 0,
  is_upsell tinyint(1) NOT NULL DEFAULT 0,
  PRIMARY KEY  (id),
  KEY booking_id (booking_id)
) $c;",

			'booking_products'    => "CREATE TABLE {$t['booking_products']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  booking_id bigint(20) unsigned NOT NULL,
  product_id bigint(20) unsigned NOT NULL,
  quantity int(11) NOT NULL,
  unit_price decimal(10,2) NOT NULL,
  PRIMARY KEY  (id),
  UNIQUE KEY booking_product (booking_id,product_id)
) $c;",

			// One resource reserved over [start_utc, end_utc). Rows in state held/active for one resource must never overlap.
			'booking_allocations' => "CREATE TABLE {$t['booking_allocations']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  booking_id bigint(20) unsigned NOT NULL,
  resource_id bigint(20) unsigned NOT NULL,
  service_id bigint(20) unsigned DEFAULT NULL,
  start_utc datetime NOT NULL,
  end_utc datetime NOT NULL,
  state varchar(20) NOT NULL,
  PRIMARY KEY  (id),
  KEY resource_period (resource_id,state,start_utc,end_utc),
  KEY booking_id (booking_id)
) $c;",

			'status_history'      => "CREATE TABLE {$t['status_history']} (
  id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  booking_id bigint(20) unsigned NOT NULL,
  from_status varchar(30) DEFAULT NULL,
  to_status varchar(30) NOT NULL,
  actor_user_id bigint(20) unsigned DEFAULT NULL,
  note text DEFAULT NULL,
  created_utc datetime NOT NULL,
  PRIMARY KEY  (id),
  KEY booking_id (booking_id)
) $c;",
		);
	}
}
