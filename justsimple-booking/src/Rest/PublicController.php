<?php
namespace JustSimple\Booking\Rest;

use JustSimple\Booking\Booking\BookingService;
use JustSimple\Booking\Data\Catalog;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Data\WpScheduleSource;
use JustSimple\Booking\Engine\EngineError;
use JustSimple\Booking\Engine\MinimumSpend;
use JustSimple\Booking\Engine\RestaurantAllocator;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/**
 * Routes the booking widget calls. They need no login and no nonce: caching plugins serve pages with stale nonces to
 * logged-out visitors, which would break booking. Abuse is slowed by a per-IP rate limit and a honeypot field instead.
 */
final class PublicController {

	public static function register() {
		register_rest_route( Rest::NS, '/modules/(?P<id>\d+)', array(
			'methods'             => 'GET',
			'callback'            => array( __CLASS__, 'module' ),
			'permission_callback' => '__return_true',
		) );
		register_rest_route( Rest::NS, '/modules/(?P<id>\d+)/availability', array(
			'methods'             => 'GET',
			'callback'            => self::limited( 'read', 'availability' ),
			'permission_callback' => '__return_true',
		) );
		register_rest_route( Rest::NS, '/modules/(?P<id>\d+)/availability/month', array(
			'methods'             => 'GET',
			'callback'            => self::limited( 'read', 'month' ),
			'permission_callback' => '__return_true',
		) );
		register_rest_route( Rest::NS, '/modules/(?P<id>\d+)/bookings', array(
			'methods'             => 'POST',
			'callback'            => self::limited( 'write', 'book' ),
			'permission_callback' => '__return_true',
		) );
		register_rest_route( Rest::NS, '/modules/(?P<id>\d+)/products', array(
			'methods'             => 'GET',
			'callback'            => self::limited( 'read', 'products' ),
			'permission_callback' => '__return_true',
		) );
		register_rest_route( Rest::NS, '/modules/(?P<id>\d+)/supported-vehicles', array(
			'methods'             => 'GET',
			'callback'            => self::limited( 'read', 'supported_vehicles' ),
			'permission_callback' => '__return_true',
		) );
		register_rest_route( Rest::NS, '/bookings/(?P<code>[A-Za-z0-9]{12})', array(
			'methods'             => 'GET',
			'callback'            => self::limited( 'read', 'show' ),
			'permission_callback' => '__return_true',
		) );
		register_rest_route( Rest::NS, '/bookings/(?P<code>[A-Za-z0-9]{12})/cancel', array(
			'methods'             => 'POST',
			'callback'            => self::limited( 'write', 'cancel' ),
			'permission_callback' => '__return_true',
		) );
	}

	/** Requests per IP per 10 minutes. */
	const BUDGETS = array( 'read' => 300, 'write' => 20 );

	/**
	 * Counts the request against its budget inside the handler. Not in permission_callback: WordPress calls that again to
	 * build the Allow header, which counted every request twice.
	 */
	private static function limited( string $bucket, string $handler ): \Closure {
		return static function ( \WP_REST_Request $r ) use ( $bucket, $handler ) {
			$ok = Rest::rate_limit( $bucket, self::BUDGETS[ $bucket ] );
			return true === $ok ? self::$handler( $r ) : $ok;
		};
	}

	public static function module( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$m        = Catalog::bookable_module( (int) $r['id'] );
			$services = $wpdb->get_results( $wpdb->prepare( 'SELECT id, name, description, duration_minutes, price, price_after_inspection, is_addon, location_type, service_category, travel_buffer_minutes FROM ' . Schema::table( 'services' ) . ' WHERE module_id = %d AND active = 1 ORDER BY is_addon, sort_order, id', $m['id'] ), ARRAY_A );
			$staff    = $wpdb->get_results( $wpdb->prepare( 'SELECT id, name FROM ' . Schema::table( 'resources' ) . " WHERE module_id = %d AND type = 'staff' AND status = 'active' ORDER BY sort_order, id", $m['id'] ), ARRAY_A );
			$policy   = $m['default_policy_id'] ? $wpdb->get_row( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'policies' ) . ' WHERE id = %d', $m['default_policy_id'] ), ARRAY_A ) : null;
			return array(
				'id'       => $m['id'],
				'title'    => $m['title'],
				'type'     => $m['type'],
				'timezone' => wp_timezone_string(),
				'services' => array_map( static function ( $s ) {
					return array(
						'id'                     => (int) $s['id'],
						'name'                   => $s['name'],
						'description'            => $s['description'],
						'duration_minutes'       => (int) $s['duration_minutes'],
						'price'                  => $s['price'],
						'price_after_inspection' => (bool) (int) $s['price_after_inspection'],
						'is_addon'               => (bool) (int) $s['is_addon'],
						'location_type'          => $s['location_type'],
						'service_category'       => $s['service_category'],
						'travel_buffer_minutes'  => (int) $s['travel_buffer_minutes'],
					);
				}, $services ),
				'staff'    => array_map( static function ( $s ) {
					return array( 'id' => (int) $s['id'], 'name' => $s['name'] );
				}, $staff ),
				'restaurant' => 'restaurant' === $m['type'] ? self::restaurant_info( $m['id'] ) : null,
				'look'       => array(
					'accent'   => $m['settings']['accent'],
					'font'     => $m['settings']['font'],
					'currency' => $m['settings']['currency'],
					'phone'    => $m['settings']['phone'],
					'country'  => $m['settings']['country'], // the code in front of the customer's number until they choose another
				),
				// field: the optional map picker starts near the business (rounded to about 1 km; the exact point stays private)
				'map'        => 'field' === $m['type'] ? array(
					'enabled' => (bool) $m['settings']['map_picker'],
					'center'  => null === $m['settings']['latitude'] ? null : array( round( (float) $m['settings']['latitude'], 2 ), round( (float) $m['settings']['longitude'], 2 ) ),
				) : null,
				'policy'   => $policy ? array(
					'needs_approval'            => 'pending' === $policy['default_status'],
					'min_notice_booking_minutes' => (int) $policy['min_notice_booking_minutes'],
					'min_notice_cancel_minutes' => (int) $policy['min_notice_cancel_minutes'],
					'booking_window_days'       => (int) $policy['booking_window_days'],
				) : null,
			);
		} );
	}

	/**
	 * What a restaurant can seat, per table class: the biggest party one table takes and the biggest with separate seating
	 * (its 6 largest tables together). The tables themselves are listed only when customers may choose one.
	 */
	private static function restaurant_info( int $module_id ): array {
		$source  = new WpScheduleSource();
		$setting = $source->slot_settings( $module_id );
		$choose  = $setting && $setting['allow_table_selection'];
		$active  = array_values( array_filter( $source->tables( $module_id ), static function ( $t ) {
			return 'active' === $t['status'];
		} ) );
		$out = array( 'table_selection' => $choose );
		foreach ( array( 'normal' => false, 'vip' => true ) as $class => $vip ) {
			$caps = array_column( array_filter( $active, static function ( $t ) use ( $vip ) {
				return $t['is_vip'] === $vip;
			} ), 'capacity' );
			rsort( $caps );
			$out[ $class ] = array(
				'largest_table'          => $caps ? $caps[0] : 0,
				'largest_separate_party' => array_sum( array_slice( $caps, 0, RestaurantAllocator::MAX_COMBINED_TABLES ) ),
			);
		}
		if ( $choose ) {
			$out['tables'] = array_map( static function ( $t ) {
				return array(
					'id'            => $t['id'],
					'name'          => $t['name'],
					'capacity'      => $t['capacity'],
					'table_type'    => $t['is_vip'] ? 'vip' : 'normal',
					'minimum_spend' => $t['minimum_spend'] > 0 ? MinimumSpend::format( $t['minimum_spend'] ) : null,
				);
			}, $active );
		}
		return $out;
	}

	/** Party parameters of an availability query: party_size, table_type, separate (the engine checks them). */
	private static function party_query( \WP_REST_Request $r ): array {
		return array(
			'party_size'             => null === $r['party_size'] || '' === $r['party_size'] ? null : (int) $r['party_size'],
			'table_type'             => $r['table_type'] ? (string) $r['table_type'] : 'normal',
			'allow_separate_seating' => in_array( $r['separate'], array( true, 1, '1', 'true' ), true ),
			// field: the customer's map pin for an on-site visit
			'latitude'               => $r['latitude'],
			'longitude'              => $r['longitude'],
		);
	}

	/** The staff member or table a query names. */
	private static function named( \WP_REST_Request $r ): ?int {
		$id = $r['staff'] ?: $r['table'];
		return $id ? (int) $id : null;
	}

	/** The car models a workshop services, with any year limits. */
	public static function supported_vehicles( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$m = Catalog::bookable_module( (int) $r['id'] );
			return array_map( static function ( $v ) {
				return array(
					'id'        => (int) $v['id'],
					'make'      => $v['make'],
					'model'     => $v['model'],
					'year_from' => null === $v['year_from'] ? null : (int) $v['year_from'],
					'year_to'   => null === $v['year_to'] ? null : (int) $v['year_to'],
				);
			}, $wpdb->get_results( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'supported_vehicles' ) . ' WHERE module_id = %d ORDER BY make, model, id', $m['id'] ), ARRAY_A ) );
		} );
	}

	/** Retail products: name, price and stock status only, never the count or SKU. */
	public static function products( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			global $wpdb;
			$m = Catalog::bookable_module( (int) $r['id'] );
			return array_map( static function ( $p ) {
				return array(
					'id'           => (int) $p['id'],
					'name'         => $p['name'],
					'category'     => $p['category'],
					'price'        => $p['price'],
					'stock_status' => $p['stock_status'],
				);
			}, $wpdb->get_results( $wpdb->prepare( 'SELECT id, name, category, price, stock_status FROM ' . Schema::table( 'products' ) . ' WHERE module_id = %d ORDER BY name, id', $m['id'] ), ARRAY_A ) );
		} );
	}

	public static function availability( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			return BookingService::availability( (int) $r['id'], (string) $r['date'], Rest::ids( $r['services'] ), self::named( $r ), self::party_query( $r ) );
		} );
	}

	public static function month( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			$month = (string) $r['month'];
			if ( ! preg_match( '/^\d{4}-(0[1-9]|1[0-2])$/', $month ) ) {
				throw new EngineError( EngineError::VALIDATION, 'Give the month as YYYY-MM.', array( 'field' => 'month' ) );
			}
			return BookingService::month_availability( (int) $r['id'], $month, Rest::ids( $r['services'] ), self::named( $r ), self::party_query( $r ) );
		} );
	}

	public static function book( \WP_REST_Request $r ) {
		// A field people never see: bots fill every field. Answer as a validation error, saying nothing about why.
		if ( '' !== (string) $r['website'] ) {
			return new \WP_Error( EngineError::VALIDATION, __( 'The booking could not be made.', 'justsimple-booking' ), array( 'status' => 422 ) );
		}
		$in = array(
			'service_ids'     => Rest::ids( $r['service_ids'] ),
			'staff_id'        => $r['staff_id'] ? (int) $r['staff_id'] : null,
			'start'           => (string) $r['start'],
			'customer'        => is_array( $r['customer'] ) ? $r['customer'] : array(),
			'special_request' => $r['special_request'],
			'idempotency_key' => $r['idempotency_key'] ?? $r->get_header( 'idempotency_key' ),
			'health_notes'    => $r['health_notes'],
			'source'          => 'widget',
			// restaurant
			'party_size'             => $r['party_size'],
			'table_type'             => $r['table_type'] ?? 'normal',
			'allow_separate_seating' => $r['allow_separate_seating'],
			'table_id'               => $r['table_id'] ? (int) $r['table_id'] : null,
			'accept_minimum_spend'   => $r['accept_minimum_spend'],
			'accepted_minimum_spend' => $r['accepted_minimum_spend'],
			// workshop
			'vehicle'                => $r['vehicle'],
			'vehicle_id'             => $r['vehicle_id'],
			'acknowledge_lead_time'  => $r['acknowledge_lead_time'],
			// field
			'address'                => $r['address'],
			'address_id'             => $r['address_id'],
		);
		$created = null;
		$resp    = Rest::run( static function () use ( $r, $in, &$created ) {
			$created = BookingService::create( (int) $r['id'], $in );
			return $created;
		}, 201 );
		if ( $created && $created['replayed'] ) {
			$resp->set_status( 200 );
		}
		return $resp;
	}

	public static function show( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			$row = BookingService::find_by_code( (string) $r['code'], (string) $r['token'] );
			return BookingService::view( (int) $row['id'], false );
		} );
	}

	public static function cancel( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			return BookingService::cancel_as_customer( (string) $r['code'], (string) $r['token'] );
		} );
	}
}
