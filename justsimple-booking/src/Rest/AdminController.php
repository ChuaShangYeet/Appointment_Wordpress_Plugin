<?php
namespace JustSimple\Booking\Rest;

use JustSimple\Booking\Activator;
use JustSimple\Booking\Booking\Access;
use JustSimple\Booking\Booking\BookingService;
use JustSimple\Booking\Booking\FieldService;
use JustSimple\Booking\Booking\Parts;
use JustSimple\Booking\Booking\Upsell;
use JustSimple\Booking\Data\Catalog;
use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Data\WpScheduleSource;
use JustSimple\Booking\Engine\Clock;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/**
 * Setup and booking-desk routes for wp-admin. Cookie auth with the X-WP-Nonce header (the REST API's own check);
 * jsb_manage for setup, jsb_work_bookings for working the bookings.
 */
final class AdminController {

	/** Child tables managed with the same list/create/update/delete pattern. Each row carries module_id. */
	const SPECS = array(
		'services' => array(
			'table'  => 'services',
			'path'   => 'services',
			'fields' => array(
				'name'                   => array( 'type' => 'str', 'required' => true ),
				'description'            => array( 'type' => 'text', 'nullable' => true ),
				'duration_minutes'       => array( 'type' => 'int', 'required' => true, 'min' => 5, 'max' => 1440 ),
				'buffer_minutes'         => array( 'type' => 'int', 'min' => 0, 'max' => 480 ),
				'price'                  => array( 'type' => 'decimal' ),
				'price_after_inspection' => array( 'type' => 'bool' ),
				'policy_id'              => array( 'type' => 'int', 'nullable' => true ),
				'active'                 => array( 'type' => 'bool' ),
				'is_addon'               => array( 'type' => 'bool' ),
				'location_type'          => array( 'type' => 'enum', 'values' => array( 'in_office', 'virtual', 'on_site' ) ),
				'service_category'       => array( 'type' => 'enum', 'nullable' => true, 'values' => array( 'inspection', 'standard' ) ),
				'travel_buffer_minutes'  => array( 'type' => 'int', 'min' => 0, 'max' => 240 ),
				'sort_order'             => array( 'type' => 'int' ),
			),
		),
		'staff'    => array(
			'table'         => 'resources',
			'path'          => 'staff',
			'resource_type' => 'staff',
			'fields' => array(
				'name'          => array( 'type' => 'str', 'required' => true ),
				'status'        => array( 'type' => 'enum', 'values' => array( 'active', 'inactive' ) ),
				'contact_email' => array( 'type' => 'email', 'nullable' => true ),
				'contact_phone' => array( 'type' => 'str', 'nullable' => true, 'max' => 32 ),
				'wp_user_id'    => array( 'type' => 'int', 'nullable' => true ),
				'meeting_link'  => array( 'type' => 'url', 'nullable' => true ),
				'sort_order'    => array( 'type' => 'int' ),
			),
		),
		'policies' => array(
			'table'  => 'policies',
			'path'   => 'policies',
			'fields' => array(
				'name'                       => array( 'type' => 'str', 'required' => true, 'max' => 100 ),
				'default_status'             => array( 'type' => 'enum', 'values' => array( 'confirmed', 'pending' ) ),
				'min_notice_booking_minutes' => array( 'type' => 'int', 'min' => 0, 'max' => 525600 ),
				'min_notice_cancel_minutes'  => array( 'type' => 'int', 'min' => 0, 'max' => 525600 ),
				'booking_window_days'        => array( 'type' => 'int', 'min' => 1, 'max' => 730 ),
				'pending_timeout_minutes'    => array( 'type' => 'int', 'min' => 5, 'max' => 10080 ),
			),
		),
		'days-off' => array(
			'table'      => 'days_off',
			'path'       => 'days-off',
			'timestamps' => false,
			'fields' => array(
				'name'          => array( 'type' => 'str', 'required' => true ),
				'start_date'    => array( 'type' => 'date', 'required' => true ),
				'end_date'      => array( 'type' => 'date', 'required' => true ),
				'resource_id'   => array( 'type' => 'int', 'nullable' => true ),
				'repeat_yearly' => array( 'type' => 'bool' ),
			),
		),
		// Restaurant tables: resources of type 'table'; class, minimum spend and location live in the attributes JSON.
		'tables'   => array(
			'table'         => 'resources',
			'path'          => 'tables',
			'resource_type' => 'table',
			'fields'        => array(
				'name'       => array( 'type' => 'str', 'required' => true ),
				'capacity'   => array( 'type' => 'int', 'required' => true, 'min' => 1, 'max' => 50 ),
				'status'     => array( 'type' => 'enum', 'values' => array( 'active', 'inactive' ) ),
				'sort_order' => array( 'type' => 'int' ),
			),
			'attributes'    => array(
				'is_vip'        => array( 'type' => 'bool' ),
				'minimum_spend' => array( 'type' => 'decimal' ),
				'location'      => array( 'type' => 'str', 'nullable' => true, 'max' => 80 ),
			),
		),
		// Workshop bays: resources of type 'bay'. They follow opening hours and days off; they have no shifts.
		'bays'     => array(
			'table'         => 'resources',
			'path'          => 'bays',
			'resource_type' => 'bay',
			'fields'        => array(
				'name'       => array( 'type' => 'str', 'required' => true ),
				'status'     => array( 'type' => 'enum', 'values' => array( 'active', 'inactive' ) ),
				'sort_order' => array( 'type' => 'int' ),
			),
		),
		// The car models a workshop services, optionally limited to model years.
		'supported-vehicles' => array(
			'table'      => 'supported_vehicles',
			'path'       => 'supported-vehicles',
			'timestamps' => false,
			'fields'     => array(
				'make'      => array( 'type' => 'str', 'required' => true, 'max' => 80 ),
				'model'     => array( 'type' => 'str', 'required' => true, 'max' => 80 ),
				'year_from' => array( 'type' => 'int', 'nullable' => true, 'min' => 1900, 'max' => 2100 ),
				'year_to'   => array( 'type' => 'int', 'nullable' => true, 'min' => 1900, 'max' => 2100 ),
			),
		),
		// Salon retail products and workshop parts.
		'products' => array(
			'table'      => 'products',
			'path'       => 'products',
			'timestamps' => false,
			'fields'     => array(
				'name'                     => array( 'type' => 'str', 'required' => true ),
				'category'                 => array( 'type' => 'str', 'nullable' => true, 'max' => 80 ),
				'stock_status'             => array( 'type' => 'enum', 'values' => array( 'in_stock', 'out_of_stock', 'available_on_order' ) ),
				'estimated_lead_time_days' => array( 'type' => 'int', 'nullable' => true, 'min' => 0, 'max' => 365 ),
				'sku'                      => array( 'type' => 'str', 'nullable' => true, 'max' => 64 ),
				'price'                    => array( 'type' => 'decimal' ),
				'stock_qty'                => array( 'type' => 'int', 'min' => 0, 'max' => 1000000 ),
			),
		),
	);

	const MODULE_FIELDS = array(
		'title'    => array( 'type' => 'str', 'required' => true ),
		'type'     => array( 'type' => 'enum', 'required' => true, 'values' => Schema::MODULE_TYPES ),
		'status'   => array( 'type' => 'enum', 'values' => array( 'draft', 'active' ) ),
		'default_policy_id' => array( 'type' => 'int', 'nullable' => true ),
	);

	const SHIFT_FIELDS = array(
		'weekday'     => array( 'type' => 'int', 'required' => true, 'min' => 0, 'max' => 6 ),
		'start_time'  => array( 'type' => 'time', 'required' => true ),
		'end_time'    => array( 'type' => 'time', 'required' => true ),
		'break_start' => array( 'type' => 'time', 'nullable' => true ),
		'break_end'   => array( 'type' => 'time', 'nullable' => true ),
	);

	const HOURS_FIELDS = array(
		'weekday'    => array( 'type' => 'int', 'required' => true, 'min' => 0, 'max' => 6 ),
		'open_time'  => array( 'type' => 'time', 'required' => true ),
		'close_time' => array( 'type' => 'time', 'required' => true ),
	);

	public static function register() {
		$manage = array( __CLASS__, 'can_manage' );
		$work   = array( __CLASS__, 'can_work' );

		self::route( '/admin/modules', array(
			array( 'GET', array( __CLASS__, 'list_modules' ), $work ),
			array( 'POST', array( __CLASS__, 'create_module' ), $manage ),
		) );
		self::route( '/admin/modules/(?P<id>\d+)', array(
			array( 'GET', array( __CLASS__, 'get_module' ), $work ),
			array( 'PATCH', array( __CLASS__, 'update_module' ), $manage ),
			array( 'DELETE', array( __CLASS__, 'delete_module' ), $manage ),
		) );
		foreach ( self::SPECS as $key => $spec ) {
			self::route( '/admin/modules/(?P<id>\d+)/' . $spec['path'], array(
				array( 'GET', self::bind( 'list_children', $key ), $manage ),
				array( 'POST', self::bind( 'create_child', $key ), $manage ),
			) );
			$item = array(
				array( 'PATCH', self::bind( 'update_child', $key ), $manage ),
				array( 'DELETE', self::bind( 'delete_child', $key ), $manage ),
			);
			self::route( '/admin/' . $spec['path'] . '/(?P<id>\d+)', $item );
		}
		self::route( '/admin/staff/(?P<id>\d+)/shifts', array(
			array( 'GET', array( __CLASS__, 'get_shifts' ), $manage ),
			array( 'PUT', array( __CLASS__, 'put_shifts' ), $manage ),
		) );
		self::route( '/admin/staff/(?P<id>\d+)/services', array(
			array( 'GET', array( __CLASS__, 'get_staff_services' ), $manage ),
			array( 'PUT', array( __CLASS__, 'put_staff_services' ), $manage ),
		) );
		self::route( '/admin/bays/(?P<id>\d+)/services', array(
			array( 'GET', static function ( \WP_REST_Request $r ) {
				return self::get_staff_services( $r, 'bays' );
			}, $manage ),
			array( 'PUT', static function ( \WP_REST_Request $r ) {
				return self::put_staff_services( $r, 'bays' );
			}, $manage ),
		) );
		self::route( '/admin/services/(?P<id>\d+)/parts', array(
			array( 'GET', array( __CLASS__, 'get_service_parts' ), $manage ),
			array( 'PUT', array( __CLASS__, 'put_service_parts' ), $manage ),
		) );
		self::route( '/admin/bookings/(?P<id>\d+)/parts', array(
			array( 'GET', array( __CLASS__, 'get_job_parts' ), $work ),
			array( 'PUT', array( __CLASS__, 'put_job_parts' ), $work ),
		) );
		self::route( '/admin/bookings/(?P<id>\d+)/resume', array(
			array( 'POST', array( __CLASS__, 'resume' ), $work ),
		) );
		self::route( '/admin/bookings/(?P<id>\d+)/meeting-link', array(
			array( 'PATCH', array( __CLASS__, 'meeting_link' ), $work ),
		) );
		self::route( '/admin/bookings/(?P<id>\d+)/follow-up', array(
			array( 'POST', array( __CLASS__, 'follow_up' ), $work ),
		) );
		self::route( '/admin/bookings/(?P<id>\d+)/actual-duration', array(
			array( 'PATCH', array( __CLASS__, 'actual_duration' ), $work ),
		) );
		self::route( '/admin/modules/(?P<id>\d+)/hours', array(
			array( 'GET', array( __CLASS__, 'get_hours' ), $manage ),
			array( 'PUT', array( __CLASS__, 'put_hours' ), $manage ),
		) );
		self::route( '/admin/modules/(?P<id>\d+)/seating', array(
			array( 'GET', array( __CLASS__, 'get_seating' ), $manage ),
			array( 'PUT', array( __CLASS__, 'put_seating' ), $manage ),
		) );
		self::route( '/admin/modules/(?P<id>\d+)/table-rules', array(
			array( 'GET', array( __CLASS__, 'get_table_rules' ), $manage ),
			array( 'PUT', array( __CLASS__, 'put_table_rules' ), $manage ),
		) );
		self::route( '/admin/modules/(?P<id>\d+)/bookings', array(
			array( 'GET', array( __CLASS__, 'list_bookings' ), $work ),
		) );
		self::route( '/admin/bookings/(?P<id>\d+)', array(
			array( 'GET', array( __CLASS__, 'get_booking' ), $work ),
			array( 'PATCH', array( __CLASS__, 'update_booking' ), $work ),
		) );
		self::route( '/admin/bookings/(?P<id>\d+)/items', array(
			array( 'POST', array( __CLASS__, 'add_items' ), $work ),
		) );
		self::route( '/admin/bookings/(?P<id>\d+)/status', array(
			array( 'PATCH', array( __CLASS__, 'set_status' ), $work ),
		) );
		self::route( '/admin/bookings/(?P<id>\d+)/history', array(
			array( 'GET', array( __CLASS__, 'history' ), $work ),
		) );
	}

	public static function can_manage() {
		return current_user_can( Activator::CAP_MANAGE );
	}

	public static function can_work() {
		return current_user_can( Activator::CAP_WORK );
	}

	// ------------------------------------------------------------------ modules

	public static function list_modules() {
		return Rest::run( static function () {
			global $wpdb;
			$ids = $wpdb->get_col( 'SELECT id FROM ' . Schema::table( 'modules' ) . ' ORDER BY id' );
			return array_map( static function ( $id ) {
				return self::module_out( Catalog::module( (int) $id ) );
			}, $ids );
		} );
	}

	public static function get_module( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			return self::module_out( self::module_or_404( (int) $r['id'] ) );
		} );
	}

	/** A new module gets a "Standard" policy (confirmed at once, 60-day window) so it can take bookings straight away. */
	public static function create_module( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$in = Fields::clean( self::MODULE_FIELDS, $r->get_json_params() ?: array(), false );
			unset( $in['default_policy_id'] );
			if ( ! in_array( $in['type'], BookingService::BOOKABLE_TYPES, true ) ) {
				throw new EngineError( EngineError::VALIDATION, 'This release takes ' . implode( ' and ', BookingService::BOOKABLE_TYPES ) . ' modules; the other module types arrive in later releases.', array( 'field' => 'type' ) );
			}
			$settings = self::clean_settings( (array) ( $r->get_json_params()['settings'] ?? array() ), Catalog::SETTINGS_DEFAULTS );
			$now      = Db::now_sql();
			return Db::transaction( static function () use ( $wpdb, $in, $settings, $now ) {
				$wpdb->insert( Schema::table( 'modules' ), $in + array( 'status' => 'draft', 'settings' => wp_json_encode( $settings ), 'created_utc' => $now, 'updated_utc' => $now ) );
				Db::check();
				$id = (int) $wpdb->insert_id;
				$wpdb->insert( Schema::table( 'policies' ), array( 'module_id' => $id, 'name' => 'Standard', 'default_status' => 'confirmed', 'min_notice_booking_minutes' => 60, 'min_notice_cancel_minutes' => 1440, 'booking_window_days' => 60, 'pending_timeout_minutes' => 120, 'created_utc' => $now, 'updated_utc' => $now ) );
				Db::check();
				$wpdb->update( Schema::table( 'modules' ), array( 'default_policy_id' => $wpdb->insert_id ), array( 'id' => $id ) );
				Db::check();
				return self::module_out( Catalog::module( $id ) );
			} );
		}, 201 );
	}

	public static function update_module( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$m    = self::module_or_404( (int) $r['id'] );
			$body = $r->get_json_params() ?: array();
			$in   = Fields::clean( self::MODULE_FIELDS, $body, true );
			if ( isset( $in['type'] ) && $in['type'] !== $m['type'] ) {
				throw new EngineError( EngineError::VALIDATION, 'A module keeps its type; create a new module instead.', array( 'field' => 'type' ) );
			}
			if ( array_key_exists( 'default_policy_id', $in ) ) {
				if ( null === $in['default_policy_id'] ) {
					throw new EngineError( EngineError::VALIDATION, 'A module needs a default policy.', array( 'field' => 'default_policy_id' ) );
				}
				self::owned_or_fail( 'policies', $in['default_policy_id'], $m['id'], 'default_policy_id' );
			}
			if ( isset( $body['settings'] ) ) {
				$in['settings'] = wp_json_encode( self::clean_settings( (array) $body['settings'], $m['settings'] ) );
			}
			$in['updated_utc'] = Db::now_sql();
			$wpdb->update( Schema::table( 'modules' ), $in, array( 'id' => $m['id'] ) );
			Db::check();
			return self::module_out( Catalog::module( $m['id'] ) );
		} );
	}

	/** Refused while the module has bookings still to come; otherwise everything of the module goes, its past bookings too. */
	public static function delete_module( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$m    = self::module_or_404( (int) $r['id'] );
			$b    = Schema::table( 'bookings' );
			$live = (int) $wpdb->get_var( $wpdb->prepare( "SELECT COUNT(*) FROM $b WHERE module_id = %d AND status IN ('pending','confirmed','in-progress','waiting-for-parts') AND end_utc > %s", $m['id'], Db::now_sql() ) );
			if ( $live ) {
				throw new EngineError( EngineError::VALIDATION, sprintf( 'This module still has %d upcoming bookings. Cancel them or set the module to draft instead.', $live ) );
			}
			Db::transaction( static function () use ( $wpdb, $m, $b ) {
				$id         = $m['id'];
				$bookings   = "SELECT id FROM $b WHERE module_id = $id";
				$resources  = 'SELECT id FROM ' . Schema::table( 'resources' ) . " WHERE module_id = $id";
				$services   = 'SELECT id FROM ' . Schema::table( 'services' ) . " WHERE module_id = $id";
				$statements = array(
					'DELETE FROM ' . Schema::table( 'status_history' ) . " WHERE booking_id IN ($bookings)",
					'DELETE FROM ' . Schema::table( 'booking_allocations' ) . " WHERE booking_id IN ($bookings)",
					'DELETE FROM ' . Schema::table( 'booking_items' ) . " WHERE booking_id IN ($bookings)",
					'DELETE FROM ' . Schema::table( 'booking_products' ) . " WHERE booking_id IN ($bookings)",
					"DELETE FROM $b WHERE module_id = $id",
					'DELETE FROM ' . Schema::table( 'shifts' ) . " WHERE resource_id IN ($resources)",
					'DELETE FROM ' . Schema::table( 'service_resources' ) . " WHERE service_id IN ($services)",
				);
				foreach ( array( 'services', 'resources', 'policies', 'operating_hours', 'days_off', 'slot_settings', 'table_open_rules', 'products', 'supported_vehicles' ) as $t ) {
					$statements[] = 'DELETE FROM ' . Schema::table( $t ) . " WHERE module_id = $id";
				}
				$statements[] = 'DELETE FROM ' . Schema::table( 'modules' ) . " WHERE id = $id";
				foreach ( $statements as $sql ) {
					$wpdb->query( $sql ); // $id is an int from the database
					Db::check();
				}
			} );
			return array( 'deleted' => true, 'id' => $m['id'] );
		} );
	}

	// ------------------------------------------------------------------ services, staff, policies, days off

	public static function list_children( string $key, \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $key, $r ) {
			global $wpdb;
			$spec  = self::SPECS[ $key ];
			$m     = self::module_or_404( (int) $r['id'] );
			$where = self::type_where( $spec );
			$order = in_array( $key, array( 'services', 'staff', 'tables', 'bays' ), true ) ? 'sort_order, id' : ( 'days-off' === $key ? 'start_date, id' : ( 'products' === $key ? 'name, id' : ( 'supported-vehicles' === $key ? 'make, model, id' : 'id' ) ) );
			$rows  = $wpdb->get_results( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( $spec['table'] ) . " WHERE module_id = %d$where ORDER BY $order", $m['id'] ), ARRAY_A );
			return array_map( static function ( $row ) use ( $spec ) {
				return self::row_out( $spec, $row );
			}, $rows );
		} );
	}

	public static function create_child( string $key, \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $key, $r ) {
			global $wpdb;
			$spec = self::SPECS[ $key ];
			$m    = self::module_or_404( (int) $r['id'] );
			$body  = $r->get_json_params() ?: array();
			$in    = Fields::clean( $spec['fields'], $body, false );
			$attrs = isset( $spec['attributes'] ) ? Fields::clean( $spec['attributes'], $body, true ) : array();
			self::check_refs( $key, $in + array( 'attributes' => $attrs ), $m['id'] );
			$in['module_id'] = $m['id'];
			if ( isset( $spec['resource_type'] ) ) {
				$in += array( 'type' => $spec['resource_type'], 'attributes' => wp_json_encode( (object) $attrs ) );
			}
			if ( $spec['timestamps'] ?? true ) {
				$in['created_utc'] = Db::now_sql();
				$in['updated_utc'] = $in['created_utc'];
			}
			$wpdb->insert( Schema::table( $spec['table'] ), $in );
			Db::check();
			return self::child_out( $key, (int) $wpdb->insert_id );
		}, 201 );
	}

	public static function update_child( string $key, \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $key, $r ) {
			global $wpdb;
			$spec = self::SPECS[ $key ];
			$row  = self::child_or_404( $key, (int) $r['id'] );
			$body = $r->get_json_params() ?: array();
			$in   = Fields::clean( $spec['fields'], $body, true );
			if ( isset( $spec['attributes'] ) ) {
				$attrs = array_merge( self::attributes( $row ), Fields::clean( $spec['attributes'], $body, true ) );
				self::check_refs( $key, $in + array( 'attributes' => $attrs ) + $row, (int) $row['module_id'] );
				$in['attributes'] = wp_json_encode( (object) $attrs );
			} else {
				self::check_refs( $key, $in + $row, (int) $row['module_id'] );
			}
			if ( $spec['timestamps'] ?? true ) {
				$in['updated_utc'] = Db::now_sql();
			}
			if ( $in ) {
				$wpdb->update( Schema::table( $spec['table'] ), $in, array( 'id' => $row['id'] ) );
				Db::check();
			}
			return self::child_out( $key, (int) $row['id'] );
		} );
	}

	/**
	 * Services, staff and tables with bookings are deactivated rather than deleted, so past bookings keep their names; a module's
	 * default policy cannot be deleted.
	 */
	public static function delete_child( string $key, \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $key, $r ) {
			global $wpdb;
			$spec = self::SPECS[ $key ];
			$row  = self::child_or_404( $key, (int) $r['id'] );
			$id   = (int) $row['id'];
			if ( 'services' === $key || isset( $spec['resource_type'] ) ) {
				$used = 'services' === $key
					? $wpdb->get_var( $wpdb->prepare( 'SELECT 1 FROM ' . Schema::table( 'booking_items' ) . ' WHERE service_id = %d LIMIT 1', $id ) )
					: $wpdb->get_var( $wpdb->prepare( 'SELECT 1 FROM ' . Schema::table( 'booking_allocations' ) . ' WHERE resource_id = %d LIMIT 1', $id ) );
				if ( $used ) {
					$wpdb->update( Schema::table( $spec['table'] ), 'services' === $key ? array( 'active' => 0 ) : array( 'status' => 'inactive' ), array( 'id' => $id ) );
					Db::check();
					return array( 'deleted' => false, 'deactivated' => true, 'id' => $id );
				}
			}
			if ( 'products' === $key && $wpdb->get_var( $wpdb->prepare( 'SELECT 1 FROM ' . Schema::table( 'booking_products' ) . ' WHERE product_id = %d LIMIT 1', $id ) ) ) {
				throw new EngineError( EngineError::VALIDATION, 'This product is on a ticket. Set it to out of stock instead.' );
			}
			if ( 'policies' === $key ) {
				$m = Catalog::module( (int) $row['module_id'] );
				if ( (int) $m['default_policy_id'] === $id ) {
					throw new EngineError( EngineError::VALIDATION, 'This is the module\'s default policy. Choose another default first.' );
				}
				$wpdb->update( Schema::table( 'services' ), array( 'policy_id' => null ), array( 'policy_id' => $id ) );
			}
			Db::transaction( static function () use ( $wpdb, $key, $spec, $id ) {
				if ( isset( $spec['resource_type'] ) ) {
					$wpdb->delete( Schema::table( 'shifts' ), array( 'resource_id' => $id ) );
					$wpdb->delete( Schema::table( 'service_resources' ), array( 'resource_id' => $id ) );
					$wpdb->delete( Schema::table( 'days_off' ), array( 'resource_id' => $id ) );
				} elseif ( 'services' === $key ) {
					$wpdb->delete( Schema::table( 'service_resources' ), array( 'service_id' => $id ) );
				}
				$wpdb->delete( Schema::table( $spec['table'] ), array( 'id' => $id ) );
				Db::check();
			} );
			return array( 'deleted' => true, 'id' => $id );
		} );
	}

	// ------------------------------------------------------------------ shifts, qualifications, opening hours

	public static function get_shifts( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			$staff = self::child_or_404( 'staff', (int) $r['id'] );
			return self::shifts_out( (int) $staff['id'] );
		} );
	}

	/** Replaces the whole week. Body: { "shifts": [ { weekday, start_time, end_time, break_start?, break_end? } ] } */
	public static function put_shifts( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$staff = self::child_or_404( 'staff', (int) $r['id'] );
			$rows  = array();
			foreach ( (array) ( $r->get_json_params()['shifts'] ?? array() ) as $i => $raw ) {
				$s = Fields::clean( self::SHIFT_FIELDS, (array) $raw, false ) + array( 'break_start' => null, 'break_end' => null );
				self::check_shift( $s, "shifts[$i]" );
				$rows[] = $s;
			}
			Db::transaction( static function () use ( $wpdb, $staff, $rows ) {
				$wpdb->delete( Schema::table( 'shifts' ), array( 'resource_id' => $staff['id'] ) );
				foreach ( $rows as $s ) {
					$wpdb->insert( Schema::table( 'shifts' ), $s + array( 'resource_id' => $staff['id'] ) );
					Db::check();
				}
			} );
			return self::shifts_out( (int) $staff['id'] );
		} );
	}

	public static function get_staff_services( \WP_REST_Request $r, string $key = 'staff' ) {
		return Rest::run( static function () use ( $r, $key ) {
			global $wpdb;
			$staff = self::child_or_404( $key, (int) $r['id'] );
			return array( 'service_ids' => array_map( 'intval', $wpdb->get_col( $wpdb->prepare( 'SELECT service_id FROM ' . Schema::table( 'service_resources' ) . ' WHERE resource_id = %d ORDER BY service_id', $staff['id'] ) ) ) );
		} );
	}

	/**
	 * Which services this staff member does. A service linked to nobody can be done by every active staff member;
	 * once any staff member is linked to it, only linked staff are offered for it.
	 */
	public static function put_staff_services( \WP_REST_Request $r, string $key = 'staff' ) {
		return Rest::run( static function () use ( $r, $key ) {
			global $wpdb;
			$staff = self::child_or_404( $key, (int) $r['id'] );
			$ids   = Rest::ids( $r->get_json_params()['service_ids'] ?? array() );
			foreach ( $ids as $sid ) {
				self::owned_or_fail( 'services', $sid, (int) $staff['module_id'], 'service_ids' );
			}
			Db::transaction( static function () use ( $wpdb, $staff, $ids ) {
				$wpdb->delete( Schema::table( 'service_resources' ), array( 'resource_id' => $staff['id'] ) );
				foreach ( array_unique( $ids ) as $sid ) {
					$wpdb->insert( Schema::table( 'service_resources' ), array( 'service_id' => $sid, 'resource_id' => $staff['id'] ) );
					Db::check();
				}
			} );
			return array( 'service_ids' => array_values( array_unique( $ids ) ) );
		} );
	}

	public static function get_hours( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			return self::hours_out( self::module_or_404( (int) $r['id'] )['id'] );
		} );
	}

	/** Replaces the week. An empty list means no opening-hours limit (staff shifts decide). */
	public static function put_hours( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$m    = self::module_or_404( (int) $r['id'] );
			$rows = array();
			foreach ( (array) ( $r->get_json_params()['hours'] ?? array() ) as $i => $raw ) {
				$h = Fields::clean( self::HOURS_FIELDS, (array) $raw, false );
				if ( $h['open_time'] === $h['close_time'] ) {
					Fields::fail( "hours[$i]", 'must not open and close at the same time (a close before the open runs past midnight)' );
				}
				$rows[] = $h;
			}
			Db::transaction( static function () use ( $wpdb, $m, $rows ) {
				$wpdb->delete( Schema::table( 'operating_hours' ), array( 'module_id' => $m['id'] ) );
				foreach ( $rows as $h ) {
					$wpdb->insert( Schema::table( 'operating_hours' ), $h + array( 'module_id' => $m['id'] ) );
					Db::check();
				}
			} );
			return self::hours_out( $m['id'] );
		} );
	}

	// ------------------------------------------------------------------ restaurant: seating times and open-table rules

	const SEATING_FIELDS = array(
		'booking_mode'          => array( 'type' => 'enum', 'required' => true, 'values' => array( 'flexible', 'fixed' ) ),
		'slot_duration_minutes' => array( 'type' => 'int', 'required' => true, 'min' => 15, 'max' => 600 ),
		'slot_interval_minutes' => array( 'type' => 'int', 'required' => true, 'min' => 5, 'max' => 240 ),
		'first_slot_time'       => array( 'type' => 'time', 'required' => true ),
		'last_slot_time'        => array( 'type' => 'time', 'required' => true ),
		'buffer_minutes'        => array( 'type' => 'int', 'min' => 0, 'max' => 240 ),
		'allow_table_selection' => array( 'type' => 'bool' ),
		'policy_id'             => array( 'type' => 'int', 'nullable' => true ),
	);

	const TABLE_RULE_FIELDS = array(
		'weekday'       => array( 'type' => 'int', 'nullable' => true, 'min' => 0, 'max' => 6 ),
		'specific_date' => array( 'type' => 'date', 'nullable' => true ),
		'table_type'    => array( 'type' => 'enum', 'values' => array( 'normal', 'vip' ) ),
		'open_tables'   => array( 'type' => 'int', 'required' => true, 'min' => 0, 'max' => 1000 ),
	);

	public static function get_seating( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			return self::seating_out( self::restaurant_or_fail( (int) $r['id'] )['id'] );
		} );
	}

	/** Creates or replaces the module's seating-time setting (flexible or fixed grid, duration, interval, buffer, table choice). */
	public static function put_seating( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$m  = self::restaurant_or_fail( (int) $r['id'] );
			$in = Fields::clean( self::SEATING_FIELDS, $r->get_json_params() ?: array(), false ) + array( 'buffer_minutes' => 0, 'allow_table_selection' => 0, 'policy_id' => null );
			if ( null !== $in['policy_id'] ) {
				self::owned_or_fail( 'policies', (int) $in['policy_id'], $m['id'], 'policy_id' );
			}
			$t = Schema::table( 'slot_settings' );
			if ( $wpdb->get_var( $wpdb->prepare( "SELECT id FROM $t WHERE module_id = %d", $m['id'] ) ) ) {
				$wpdb->update( $t, $in, array( 'module_id' => $m['id'] ) );
			} else {
				$wpdb->insert( $t, $in + array( 'module_id' => $m['id'] ) );
			}
			Db::check();
			return self::seating_out( $m['id'] );
		} );
	}

	public static function get_table_rules( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			return self::table_rules_out( self::restaurant_or_fail( (int) $r['id'] )['id'] );
		} );
	}

	/**
	 * Replaces all open-table rules. Body: { "rules": [ { weekday | specific_date, table_type, open_tables } ] }.
	 * Each rule has a weekday or a date (not both, not neither), no duplicates, and open_tables at most the active tables of its class.
	 */
	public static function put_table_rules( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$m      = self::restaurant_or_fail( (int) $r['id'] );
			$totals = array( 'normal' => 0, 'vip' => 0 );
			foreach ( ( new WpScheduleSource() )->tables( $m['id'] ) as $t ) {
				if ( 'active' === $t['status'] ) {
					$totals[ $t['is_vip'] ? 'vip' : 'normal' ]++;
				}
			}
			$rows = array();
			$seen = array();
			foreach ( (array) ( $r->get_json_params()['rules'] ?? array() ) as $i => $raw ) {
				$rule = Fields::clean( self::TABLE_RULE_FIELDS, (array) $raw, false ) + array( 'weekday' => null, 'specific_date' => null, 'table_type' => 'normal' );
				if ( ( null === $rule['weekday'] ) === ( null === $rule['specific_date'] ) ) {
					Fields::fail( "rules[$i]", 'needs a weekday or a specific_date, not both' );
				}
				$k = $rule['table_type'] . '|' . $rule['weekday'] . '|' . $rule['specific_date'];
				if ( isset( $seen[ $k ] ) ) {
					Fields::fail( "rules[$i]", 'repeats a rule for the same table type and weekday or date' );
				}
				$seen[ $k ] = true;
				if ( $rule['open_tables'] > $totals[ $rule['table_type'] ] ) {
					Fields::fail( "rules[$i]", sprintf( 'opens %d tables but only %d %s tables are active', $rule['open_tables'], $totals[ $rule['table_type'] ], $rule['table_type'] ) );
				}
				$rows[] = $rule;
			}
			Db::transaction( static function () use ( $wpdb, $m, $rows ) {
				$wpdb->delete( Schema::table( 'table_open_rules' ), array( 'module_id' => $m['id'] ) );
				foreach ( $rows as $rule ) {
					$wpdb->insert( Schema::table( 'table_open_rules' ), $rule + array( 'module_id' => $m['id'] ) );
					Db::check();
				}
			} );
			return self::table_rules_out( $m['id'] );
		} );
	}

	private static function restaurant_or_fail( int $id ): array {
		$m = self::module_or_404( $id );
		if ( 'restaurant' !== $m['type'] ) {
			throw new EngineError( EngineError::VALIDATION, 'Seating times and table rules are only for restaurant modules.' );
		}
		return $m;
	}

	private static function seating_out( int $module_id ): ?array {
		$s = ( new WpScheduleSource() )->slot_settings( $module_id );
		if ( null === $s ) {
			return null;
		}
		$s['first_slot_time'] = substr( $s['first_slot_time'], 0, 5 );
		$s['last_slot_time']  = substr( $s['last_slot_time'], 0, 5 );
		return $s;
	}

	private static function table_rules_out( int $module_id ): array {
		global $wpdb;
		$rows = $wpdb->get_results( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'table_open_rules' ) . ' WHERE module_id = %d ORDER BY table_type, specific_date, weekday, id', $module_id ), ARRAY_A );
		return array_map( static function ( $row ) {
			return Fields::row( self::TABLE_RULE_FIELDS, $row );
		}, $rows );
	}

	// ------------------------------------------------------------------ bookings

	/** ?from=YYYY-MM-DD&to=YYYY-MM-DD (site dates, inclusive; default today and the next 30 days) &status=a,b */
	public static function list_bookings( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$m     = self::module_or_404( (int) $r['id'] );
			$tz    = wp_timezone();
			$today = Clock::local_date( time(), $tz );
			$from  = $r['from'] ? Fields::clean( array( 'from' => array( 'type' => 'date' ) ), array( 'from' => $r['from'] ), false )['from'] : $today;
			$to    = $r['to'] ? Fields::clean( array( 'to' => array( 'type' => 'date' ) ), array( 'to' => $r['to'] ), false )['to'] : Clock::add_days( $today, 30 );
			$lo    = Db::to_sql( Clock::clock_on( $from, '00:00', $tz ) );
			$hi    = Db::to_sql( Clock::clock_on( Clock::add_days( $to, 1 ), '00:00', $tz ) );
			$sql   = $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'bookings' ) . ' WHERE module_id = %d AND start_utc >= %s AND start_utc < %s', $m['id'], $lo, $hi );
			$st    = array_values( array_intersect( array_filter( explode( ',', (string) $r['status'] ) ), Schema::BOOKING_STATUSES ) );
			if ( $st ) {
				$sql .= $wpdb->prepare( ' AND status IN (' . implode( ',', array_fill( 0, count( $st ), '%s' ) ) . ')', $st );
			}
			if ( in_array( $r['table_type'], array( 'normal', 'vip' ), true ) ) {
				$sql .= $wpdb->prepare( ' AND table_type = %s', $r['table_type'] );
			}
			if ( $r['schedule_conflict'] ) {
				$sql .= ' AND schedule_conflict = 1';
			}
			$ids = $wpdb->get_col( $sql . ' ORDER BY start_utc, id LIMIT 500' );
			return array_map( static function ( $id ) {
				return BookingService::view( (int) $id, true );
			}, $ids );
		} );
	}

	public static function get_booking( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			return BookingService::view( (int) $r['id'], true );
		} );
	}

	/** Body: { "status": "confirmed", "note": "optional" } */
	public static function set_status( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			$body = $r->get_json_params() ?: array();
			$note = isset( $body['note'] ) ? sanitize_textarea_field( (string) $body['note'] ) : null;
			Access::assert_can_work( get_current_user_id(), (int) $r['id'] );
			return BookingService::transition( (int) $r['id'], (string) ( $body['status'] ?? '' ), 'staff', get_current_user_id(), $note );
		} );
	}

	/**
	 * Add add-ons and/or products to an in-progress salon appointment.
	 * Body: { "service_ids": [..], "products": [ { "product_id": 1, "quantity": 2 } ] }
	 */
	public static function add_items( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			$body = $r->get_json_params() ?: array();
			Access::assert_can_work( get_current_user_id(), (int) $r['id'] );
			return Upsell::add_to_ticket( (int) $r['id'], Rest::ids( $body['service_ids'] ?? array() ), array_values( array_filter( (array) ( $body['products'] ?? array() ), 'is_array' ) ) );
		} );
	}

	/** Body: { "schedule_conflict": false } clears the flag once someone has sorted the clash out. */
	public static function update_booking( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$id = (int) $r['id'];
			BookingService::view( $id, true ); // 404 when missing
			Access::assert_can_work( get_current_user_id(), $id );
			$in = Fields::clean( array( 'schedule_conflict' => array( 'type' => 'bool', 'required' => true ) ), $r->get_json_params() ?: array(), false );
			$wpdb->update( Schema::table( 'bookings' ), array( 'schedule_conflict' => $in['schedule_conflict'], 'updated_utc' => Db::now_sql() ), array( 'id' => $id ) );
			Db::check();
			return BookingService::view( $id, true );
		} );
	}

	// ------------------------------------------------------------------ workshop: parts and resume

	public static function get_service_parts( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$svc = self::child_or_404( 'services', (int) $r['id'] );
			return array( 'product_ids' => array_map( 'intval', $wpdb->get_col( $wpdb->prepare( 'SELECT product_id FROM ' . Schema::table( 'service_products' ) . ' WHERE service_id = %d ORDER BY product_id', $svc['id'] ) ) ) );
		} );
	}

	/** The parts a service needs (drives the lead-time notice). Body: { "product_ids": [..] } replaces the list. */
	public static function put_service_parts( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$svc = self::child_or_404( 'services', (int) $r['id'] );
			$ids = array_values( array_unique( Rest::ids( $r->get_json_params()['product_ids'] ?? array() ) ) );
			foreach ( $ids as $pid ) {
				if ( ! $wpdb->get_var( $wpdb->prepare( 'SELECT 1 FROM ' . Schema::table( 'products' ) . ' WHERE id = %d AND module_id = %d', $pid, (int) $svc['module_id'] ) ) ) {
					throw new EngineError( EngineError::NOT_FOUND, 'Unknown part.', array( 'field' => 'product_ids' ) );
				}
			}
			Db::transaction( static function () use ( $wpdb, $svc, $ids ) {
				$wpdb->delete( Schema::table( 'service_products' ), array( 'service_id' => $svc['id'] ) );
				foreach ( $ids as $pid ) {
					$wpdb->insert( Schema::table( 'service_products' ), array( 'service_id' => $svc['id'], 'product_id' => $pid ) );
					Db::check();
				}
			} );
			return array( 'product_ids' => $ids );
		} );
	}

	public static function get_job_parts( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			BookingService::view( (int) $r['id'], true ); // 404 when missing
			return array( 'parts' => Parts::lines( (int) $r['id'] ) );
		} );
	}

	/** Replaces the job's parts list. Body: { "parts": [ { "product_id": 1, "quantity": 2 } ] } (an empty list clears it). */
	public static function put_job_parts( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			Access::assert_can_work( get_current_user_id(), (int) $r['id'] );
			$parts = array_values( array_filter( (array) ( $r->get_json_params()['parts'] ?? array() ), 'is_array' ) );
			return array( 'parts' => Parts::set( (int) $r['id'], $parts ) );
		} );
	}

	// ------------------------------------------------------------------ field service

	/** Body: { "url": "https://..." }. Replaces the professional's room for this booking; emailed when the booking is confirmed. */
	public static function meeting_link( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			Access::assert_can_work( get_current_user_id(), (int) $r['id'] );
			return FieldService::set_meeting_link( (int) $r['id'], ( $r->get_json_params() ?: array() )['url'] ?? null );
		} );
	}

	/** Books a standard service after an inspection. Body: { "service_id": int, "start": ISO 8601, "staff_id"?: int } */
	public static function follow_up( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			$body = $r->get_json_params() ?: array();
			Access::assert_can_work( get_current_user_id(), (int) $r['id'] );
			return FieldService::follow_up( (int) $r['id'], (int) ( $body['service_id'] ?? 0 ), (string) ( $body['start'] ?? '' ), empty( $body['staff_id'] ) ? null : (int) $body['staff_id'], get_current_user_id() );
		}, 201 );
	}

	/** How long a running job really took. Body: { "minutes": int } */
	public static function actual_duration( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			Access::assert_can_work( get_current_user_id(), (int) $r['id'] );
			return FieldService::record_actual_duration( (int) $r['id'], ( $r->get_json_params() ?: array() )['minutes'] ?? null );
		} );
	}

	/** Reschedules a job that is waiting for parts. Body: { "start": ISO 8601, "staff_id"?: int, "service_ids"?: [..] } */
	public static function resume( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			$body = $r->get_json_params() ?: array();
			Access::assert_can_work( get_current_user_id(), (int) $r['id'] );
			return BookingService::resume( (int) $r['id'], (string) ( $body['start'] ?? '' ), empty( $body['staff_id'] ) ? null : (int) $body['staff_id'], Rest::ids( $body['service_ids'] ?? array() ), get_current_user_id() );
		} );
	}

	public static function history( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			BookingService::view( (int) $r['id'], true ); // 404 when missing
			$rows = $wpdb->get_results( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'status_history' ) . ' WHERE booking_id = %d ORDER BY id', (int) $r['id'] ), ARRAY_A );
			$tz   = wp_timezone();
			return array_map( static function ( $h ) use ( $tz ) {
				$user = $h['actor_user_id'] ? get_userdata( (int) $h['actor_user_id'] ) : null;
				return array(
					'from'  => $h['from_status'],
					'to'    => $h['to_status'],
					'by'    => $user ? $user->display_name : null,
					'note'  => $h['note'],
					'at'    => Clock::format( Db::from_sql( $h['created_utc'] ), $tz ),
				);
			}, $rows );
		} );
	}

	// ------------------------------------------------------------------ helpers

	private static function route( string $path, array $methods ) {
		$handlers = array();
		foreach ( $methods as $m ) {
			$handlers[] = array(
				'methods'             => $m[0],
				'callback'            => $m[1],
				'permission_callback' => $m[2],
			);
		}
		register_rest_route( Rest::NS, $path, $handlers );
	}

	private static function bind( string $method, string $key ): \Closure {
		return static function ( \WP_REST_Request $r ) use ( $method, $key ) {
			return self::$method( $key, $r );
		};
	}

	private static function module_or_404( int $id ): array {
		$m = Catalog::module( $id );
		if ( ! $m ) {
			throw new EngineError( EngineError::NOT_FOUND, 'Module not found.' );
		}
		return $m;
	}

	private static function module_out( array $m ): array {
		return array(
			'id'                => $m['id'],
			'title'             => $m['title'],
			'type'              => $m['type'],
			'status'            => $m['status'],
			'default_policy_id' => $m['default_policy_id'] ? (int) $m['default_policy_id'] : null,
			'settings'          => $m['settings'],
			'shortcode'         => sprintf( '[justsimple_booking id="%d"]', $m['id'] ),
		);
	}

	private static function clean_settings( array $in, array $current ): array {
		$out = $current;
		if ( array_key_exists( 'slot_step_minutes', $in ) ) {
			$out['slot_step_minutes'] = Fields::clean( array( 'slot_step_minutes' => array( 'type' => 'int', 'min' => 5, 'max' => 120 ) ), $in, true )['slot_step_minutes'];
		}
		if ( array_key_exists( 'notify_email', $in ) ) {
			$out['notify_email'] = '' === $in['notify_email'] ? '' : Fields::clean( array( 'notify_email' => array( 'type' => 'email' ) ), $in, true )['notify_email'];
		}
		// field: the business location and how far it travels; null clears a value
		foreach ( array( 'latitude' => array( -90, 90 ), 'longitude' => array( -180, 180 ), 'service_radius_km' => array( 0, 500 ) ) as $k => $range ) {
			if ( ! array_key_exists( $k, $in ) ) {
				continue;
			}
			$v = $in[ $k ];
			if ( null === $v || '' === $v ) {
				$out[ $k ] = null;
				continue;
			}
			$bad = ! is_numeric( $v ) || is_bool( $v ) || $v < $range[0] || $v > $range[1] || ( 'service_radius_km' === $k && $v <= 0 );
			if ( $bad ) {
				Fields::fail( "settings.$k", 'service_radius_km' === $k ? 'must be more than 0 and at most 500 km' : sprintf( 'must be between %d and %d', $range[0], $range[1] ) );
			}
			$out[ $k ] = (float) $v;
		}
		return $out;
	}

	private static function child_or_404( string $key, int $id ): array {
		global $wpdb;
		$spec  = self::SPECS[ $key ];
		$where = self::type_where( $spec );
		$row   = $wpdb->get_row( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( $spec['table'] ) . " WHERE id = %d$where", $id ), ARRAY_A );
		if ( ! $row ) {
			throw new EngineError( EngineError::NOT_FOUND, 'Not found.' );
		}
		return $row;
	}

	/** " AND type = 'table'" for specs over the resources table; the type is a constant of this class, never input. */
	private static function type_where( array $spec ): string {
		return isset( $spec['resource_type'] ) ? " AND type = '" . $spec['resource_type'] . "'" : '';
	}

	/** @return array<string,mixed> */
	private static function attributes( array $row ): array {
		$a = json_decode( (string) ( $row['attributes'] ?? '' ), true );
		return is_array( $a ) ? $a : array();
	}

	/** A row cast to JSON types, with the spec's attribute fields lifted out of the attributes JSON. */
	private static function row_out( array $spec, array $row ): array {
		$out = Fields::row( $spec['fields'], $row );
		if ( isset( $spec['attributes'] ) ) {
			$a = self::attributes( $row );
			foreach ( $spec['attributes'] as $field => $s ) {
				$v = $a[ $field ] ?? null;
				if ( 'bool' === $s['type'] ) {
					$v = (bool) $v;
				} elseif ( 'decimal' === $s['type'] ) {
					$v = number_format( (float) ( $v ?? 0 ), 2, '.', '' );
				}
				$out[ $field ] = $v;
			}
		}
		return $out;
	}

	private static function child_out( string $key, int $id ): array {
		return self::row_out( self::SPECS[ $key ], self::child_or_404( $key, $id ) );
	}

	/** References inside a row must point at things of the same module. */
	private static function check_refs( string $key, array $in, int $module_id ): void {
		if ( 'services' === $key && ! empty( $in['is_addon'] ) && 'salon' !== Catalog::module( $module_id )['type'] ) {
			Fields::fail( 'is_addon', 'is only for salon modules' );
		}
		if ( 'services' === $key && 'field' !== Catalog::module( $module_id )['type'] ) { // where and how a service happens is a field-service matter
			if ( 'in_office' !== ( $in['location_type'] ?? 'in_office' ) ) {
				Fields::fail( 'location_type', 'is only for field-service modules' );
			}
			if ( null !== ( $in['service_category'] ?? null ) ) {
				Fields::fail( 'service_category', 'is only for field-service modules' );
			}
			if ( (int) ( $in['travel_buffer_minutes'] ?? 0 ) > 0 ) {
				Fields::fail( 'travel_buffer_minutes', 'is only for field-service modules' );
			}
		}
		if ( 'services' === $key && ! empty( $in['policy_id'] ) ) {
			self::owned_or_fail( 'policies', (int) $in['policy_id'], $module_id, 'policy_id' );
		}
		if ( 'days-off' === $key ) {
			if ( ! empty( $in['resource_id'] ) ) {
				self::owned_or_fail( 'resources', (int) $in['resource_id'], $module_id, 'resource_id' );
			}
			if ( isset( $in['start_date'], $in['end_date'] ) && $in['end_date'] < $in['start_date'] ) {
				Fields::fail( 'end_date', 'must be on or after start_date' );
			}
		}
		if ( 'tables' === $key ) {
			if ( 'restaurant' !== Catalog::module( $module_id )['type'] ) {
				Fields::fail( 'tables', 'are only for restaurant modules' );
			}
			if ( (float) ( $in['attributes']['minimum_spend'] ?? 0 ) > 0 && empty( $in['attributes']['is_vip'] ) ) {
				Fields::fail( 'minimum_spend', 'is only for VIP tables' );
			}
		}
		if ( in_array( $key, array( 'bays', 'supported-vehicles' ), true ) && 'workshop' !== Catalog::module( $module_id )['type'] ) {
			Fields::fail( $key, 'are only for workshop modules' );
		}
		if ( 'supported-vehicles' === $key && isset( $in['year_from'], $in['year_to'] ) && (int) $in['year_to'] < (int) $in['year_from'] ) {
			Fields::fail( 'year_to', 'must not be before year_from' );
		}
		if ( 'products' === $key && 'available_on_order' === ( $in['stock_status'] ?? null ) && empty( $in['estimated_lead_time_days'] ) ) {
			Fields::fail( 'estimated_lead_time_days', 'is needed for a part that is available on order' );
		}
		if ( 'staff' === $key && ! empty( $in['wp_user_id'] ) && ! get_userdata( (int) $in['wp_user_id'] ) ) {
			Fields::fail( 'wp_user_id', 'is not a user of this site' );
		}
	}

	private static function owned_or_fail( string $table, int $id, int $module_id, string $field ): void {
		global $wpdb;
		if ( ! $wpdb->get_var( $wpdb->prepare( 'SELECT 1 FROM ' . Schema::table( $table ) . ' WHERE id = %d AND module_id = %d', $id, $module_id ) ) ) {
			Fields::fail( $field, 'does not belong to this module' );
		}
	}

	/** Start and end differ (an end before the start runs past midnight); a break is both-or-neither and lies inside the shift. */
	private static function check_shift( array $s, string $field ): void {
		if ( $s['start_time'] === $s['end_time'] ) {
			Fields::fail( $field, 'must not start and end at the same time' );
		}
		if ( ( null === $s['break_start'] ) !== ( null === $s['break_end'] ) ) {
			Fields::fail( $field, 'needs both a break start and a break end, or neither' );
		}
		if ( null === $s['break_start'] ) {
			return;
		}
		// measured forward from the shift's start, so overnight shifts and breaks after midnight work (BREAK_WITHIN_SHIFT in the engine)
		$after = static function ( $t ) use ( $s ) {
			$secs = strtotime( '1970-01-01 ' . $t . ' UTC' ) - strtotime( '1970-01-01 ' . $s['start_time'] . ' UTC' );
			return ( $secs % 86400 + 86400 ) % 86400;
		};
		if ( ! ( $after( $s['break_start'] ) < $after( $s['break_end'] ) && $after( $s['break_end'] ) <= $after( $s['end_time'] ) ) ) {
			Fields::fail( $field, 'has a break outside the shift' );
		}
	}

	private static function shifts_out( int $staff_id ): array {
		global $wpdb;
		$rows = $wpdb->get_results( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'shifts' ) . ' WHERE resource_id = %d ORDER BY weekday, start_time', $staff_id ), ARRAY_A );
		return array_map( static function ( $row ) {
			return Fields::row( self::SHIFT_FIELDS, $row );
		}, $rows );
	}

	private static function hours_out( int $module_id ): array {
		global $wpdb;
		$rows = $wpdb->get_results( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'operating_hours' ) . ' WHERE module_id = %d ORDER BY weekday, open_time', $module_id ), ARRAY_A );
		return array_map( static function ( $row ) {
			return Fields::row( self::HOURS_FIELDS, $row );
		}, $rows );
	}
}
