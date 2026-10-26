<?php
namespace JustSimple\Booking\Rest;

use JustSimple\Booking\Activator;
use JustSimple\Booking\Booking\BookingService;
use JustSimple\Booking\Data\Catalog;
use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
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
				'sort_order'             => array( 'type' => 'int' ),
			),
		),
		'staff'    => array(
			'table'  => 'resources',
			'path'   => 'staff',
			'fields' => array(
				'name'          => array( 'type' => 'str', 'required' => true ),
				'status'        => array( 'type' => 'enum', 'values' => array( 'active', 'inactive' ) ),
				'contact_email' => array( 'type' => 'email', 'nullable' => true ),
				'contact_phone' => array( 'type' => 'str', 'nullable' => true, 'max' => 32 ),
				'wp_user_id'    => array( 'type' => 'int', 'nullable' => true ),
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
			'table'  => 'days_off',
			'path'   => 'days-off',
			'fields' => array(
				'name'          => array( 'type' => 'str', 'required' => true ),
				'start_date'    => array( 'type' => 'date', 'required' => true ),
				'end_date'      => array( 'type' => 'date', 'required' => true ),
				'resource_id'   => array( 'type' => 'int', 'nullable' => true ),
				'repeat_yearly' => array( 'type' => 'bool' ),
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
		self::route( '/admin/modules/(?P<id>\d+)/hours', array(
			array( 'GET', array( __CLASS__, 'get_hours' ), $manage ),
			array( 'PUT', array( __CLASS__, 'put_hours' ), $manage ),
		) );
		self::route( '/admin/modules/(?P<id>\d+)/bookings', array(
			array( 'GET', array( __CLASS__, 'list_bookings' ), $work ),
		) );
		self::route( '/admin/bookings/(?P<id>\d+)', array(
			array( 'GET', array( __CLASS__, 'get_booking' ), $work ),
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
				throw new EngineError( EngineError::VALIDATION, 'This release takes staff appointments only; the other module types arrive in later releases.', array( 'field' => 'type' ) );
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
			$where = 'staff' === $key ? " AND type = 'staff'" : '';
			$order = in_array( $key, array( 'services', 'staff' ), true ) ? 'sort_order, id' : ( 'days-off' === $key ? 'start_date, id' : 'id' );
			$rows  = $wpdb->get_results( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( $spec['table'] ) . " WHERE module_id = %d$where ORDER BY $order", $m['id'] ), ARRAY_A );
			return array_map( static function ( $row ) use ( $spec ) {
				return Fields::row( $spec['fields'], $row );
			}, $rows );
		} );
	}

	public static function create_child( string $key, \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $key, $r ) {
			global $wpdb;
			$spec = self::SPECS[ $key ];
			$m    = self::module_or_404( (int) $r['id'] );
			$in   = Fields::clean( $spec['fields'], $r->get_json_params() ?: array(), false );
			self::check_refs( $key, $in, $m['id'] );
			$in['module_id'] = $m['id'];
			if ( 'staff' === $key ) {
				$in += array( 'type' => 'staff', 'attributes' => '{}' );
			}
			if ( 'days-off' !== $key ) {
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
			$in   = Fields::clean( $spec['fields'], $r->get_json_params() ?: array(), true );
			self::check_refs( $key, $in + $row, (int) $row['module_id'] );
			if ( 'days-off' !== $key ) {
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
	 * Services and staff with bookings are deactivated rather than deleted, so past bookings keep their names; a module's
	 * default policy cannot be deleted.
	 */
	public static function delete_child( string $key, \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $key, $r ) {
			global $wpdb;
			$spec = self::SPECS[ $key ];
			$row  = self::child_or_404( $key, (int) $r['id'] );
			$id   = (int) $row['id'];
			if ( 'services' === $key || 'staff' === $key ) {
				$used = 'services' === $key
					? $wpdb->get_var( $wpdb->prepare( 'SELECT 1 FROM ' . Schema::table( 'booking_items' ) . ' WHERE service_id = %d LIMIT 1', $id ) )
					: $wpdb->get_var( $wpdb->prepare( 'SELECT 1 FROM ' . Schema::table( 'booking_allocations' ) . ' WHERE resource_id = %d LIMIT 1', $id ) );
				if ( $used ) {
					$wpdb->update( Schema::table( $spec['table'] ), 'services' === $key ? array( 'active' => 0 ) : array( 'status' => 'inactive' ), array( 'id' => $id ) );
					Db::check();
					return array( 'deleted' => false, 'deactivated' => true, 'id' => $id );
				}
			}
			if ( 'policies' === $key ) {
				$m = Catalog::module( (int) $row['module_id'] );
				if ( (int) $m['default_policy_id'] === $id ) {
					throw new EngineError( EngineError::VALIDATION, 'This is the module\'s default policy. Choose another default first.' );
				}
				$wpdb->update( Schema::table( 'services' ), array( 'policy_id' => null ), array( 'policy_id' => $id ) );
			}
			Db::transaction( static function () use ( $wpdb, $key, $spec, $id ) {
				if ( 'staff' === $key ) {
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

	public static function get_staff_services( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$staff = self::child_or_404( 'staff', (int) $r['id'] );
			return array( 'service_ids' => array_map( 'intval', $wpdb->get_col( $wpdb->prepare( 'SELECT service_id FROM ' . Schema::table( 'service_resources' ) . ' WHERE resource_id = %d ORDER BY service_id', $staff['id'] ) ) ) );
		} );
	}

	/**
	 * Which services this staff member does. A service linked to nobody can be done by every active staff member;
	 * once any staff member is linked to it, only linked staff are offered for it.
	 */
	public static function put_staff_services( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$staff = self::child_or_404( 'staff', (int) $r['id'] );
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
			return BookingService::transition( (int) $r['id'], (string) ( $body['status'] ?? '' ), 'staff', get_current_user_id(), $note );
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
		return $out;
	}

	private static function child_or_404( string $key, int $id ): array {
		global $wpdb;
		$spec  = self::SPECS[ $key ];
		$where = 'staff' === $key ? " AND type = 'staff'" : '';
		$row   = $wpdb->get_row( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( $spec['table'] ) . " WHERE id = %d$where", $id ), ARRAY_A );
		if ( ! $row ) {
			throw new EngineError( EngineError::NOT_FOUND, 'Not found.' );
		}
		return $row;
	}

	private static function child_out( string $key, int $id ): array {
		return Fields::row( self::SPECS[ $key ]['fields'], self::child_or_404( $key, $id ) );
	}

	/** References inside a row must point at things of the same module. */
	private static function check_refs( string $key, array $in, int $module_id ): void {
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
