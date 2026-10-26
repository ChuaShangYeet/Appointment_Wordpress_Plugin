<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Activator;
use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

/**
 * "Own appointments only" (the engine's _own_job_only): a WordPress user linked to a staff member (resources.wp_user_id) who
 * can work bookings but not manage setup may change only the appointments they are allocated to. Managers, and desk users
 * not linked to any staff member, work every appointment.
 */
final class Access {

	/** @throws EngineError forbidden */
	public static function assert_can_work( int $user_id, int $booking_id ): void {
		global $wpdb;
		if ( user_can( $user_id, Activator::CAP_MANAGE ) ) {
			return;
		}
		$mine = array_map( 'intval', $wpdb->get_col( $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'resources' ) . ' WHERE wp_user_id = %d', $user_id ) ) );
		if ( ! $mine ) {
			return;
		}
		$on_it = $wpdb->get_var(
			$wpdb->prepare(
				'SELECT 1 FROM ' . Schema::table( 'booking_allocations' ) . ' WHERE booking_id = %d AND resource_id IN (' . Db::ints( $mine ) . ') LIMIT 1', // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
				array_merge( array( $booking_id ), $mine )
			)
		);
		if ( ! $on_it ) {
			throw new EngineError( EngineError::FORBIDDEN, 'You can only change your own appointments.' );
		}
	}
}
