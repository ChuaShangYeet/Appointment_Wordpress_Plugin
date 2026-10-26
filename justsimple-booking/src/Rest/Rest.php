<?php
namespace JustSimple\Booking\Rest;

use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

/** Shared REST plumbing: namespace, error translation and rate limiting. */
final class Rest {

	const NS = 'justsimple/v1';

	/**
	 * Run a handler and turn engine errors into WP_Error with the right HTTP status.
	 * Anything else is a bug: it is logged and answered with a generic 500, never with the exception text.
	 */
	public static function run( callable $fn, int $status = 200 ) {
		try {
			return new \WP_REST_Response( $fn(), $status );
		} catch ( EngineError $e ) {
			return new \WP_Error( $e->error_code, $e->getMessage(), array_merge( array( 'status' => $e->http_status() ), $e->data ) );
		} catch ( \Throwable $e ) {
			error_log( 'JustSimple Booking: ' . $e->getMessage() . ' in ' . $e->getFile() . ':' . $e->getLine() ); // phpcs:ignore WordPress.PHP.DevelopmentFunctions.error_log_error_log
			return new \WP_Error( 'internal', __( 'Something went wrong. Please try again.', 'justsimple-booking' ), array( 'status' => 500 ) );
		}
	}

	/**
	 * Count a request against a per-IP budget for `$bucket`. Transients are not atomic, so a burst can overshoot by a few;
	 * this is a brake on scripts, not an exact quota. Limits can be changed with the `jsb_rate_limit` filter.
	 *
	 * @return true|\WP_Error
	 */
	public static function rate_limit( string $bucket, int $per_ten_minutes ) {
		$limit = (int) apply_filters( 'jsb_rate_limit', $per_ten_minutes, $bucket );
		if ( $limit <= 0 ) {
			return true;
		}
		$ip    = isset( $_SERVER['REMOTE_ADDR'] ) ? sanitize_text_field( wp_unslash( $_SERVER['REMOTE_ADDR'] ) ) : 'unknown';
		$key   = 'jsb_rl_' . md5( $bucket . '|' . $ip );
		$count = (int) get_transient( $key );
		if ( $count >= $limit ) {
			return new \WP_Error( 'rate_limited', __( 'Too many requests. Please wait a few minutes and try again.', 'justsimple-booking' ), array( 'status' => 429 ) );
		}
		set_transient( $key, $count + 1, 10 * MINUTE_IN_SECONDS );
		return true;
	}

	/** int[] from a query parameter given as 1,2,3 or services[]=1&services[]=2. */
	public static function ids( $v ): array {
		if ( is_string( $v ) ) {
			$v = explode( ',', $v );
		}
		return array_values( array_filter( array_map( 'intval', (array) $v ) ) );
	}
}
