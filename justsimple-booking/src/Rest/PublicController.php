<?php
namespace JustSimple\Booking\Rest;

use JustSimple\Booking\Booking\BookingService;
use JustSimple\Booking\Data\Catalog;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\EngineError;

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
			$services = $wpdb->get_results( $wpdb->prepare( 'SELECT id, name, description, duration_minutes, price, price_after_inspection FROM ' . Schema::table( 'services' ) . ' WHERE module_id = %d AND active = 1 AND is_addon = 0 ORDER BY sort_order, id', $m['id'] ), ARRAY_A );
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
					);
				}, $services ),
				'staff'    => array_map( static function ( $s ) {
					return array( 'id' => (int) $s['id'], 'name' => $s['name'] );
				}, $staff ),
				'policy'   => $policy ? array(
					'needs_approval'            => 'pending' === $policy['default_status'],
					'min_notice_booking_minutes' => (int) $policy['min_notice_booking_minutes'],
					'min_notice_cancel_minutes' => (int) $policy['min_notice_cancel_minutes'],
					'booking_window_days'       => (int) $policy['booking_window_days'],
				) : null,
			);
		} );
	}

	public static function availability( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			return BookingService::availability( (int) $r['id'], (string) $r['date'], Rest::ids( $r['services'] ), $r['staff'] ? (int) $r['staff'] : null );
		} );
	}

	public static function month( \WP_REST_Request $r ) {
		return Rest::run( static function () use ( $r ) {
			$month = (string) $r['month'];
			if ( ! preg_match( '/^\d{4}-(0[1-9]|1[0-2])$/', $month ) ) {
				throw new EngineError( EngineError::VALIDATION, 'Give the month as YYYY-MM.', array( 'field' => 'month' ) );
			}
			return BookingService::month_availability( (int) $r['id'], $month, Rest::ids( $r['services'] ), $r['staff'] ? (int) $r['staff'] : null );
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
