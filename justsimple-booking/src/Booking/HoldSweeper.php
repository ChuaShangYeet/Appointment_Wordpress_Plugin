<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/**
 * Rejects pending bookings whose hold has run out and frees their slots (release_expired_holds in the engine).
 *
 * Runs from WP-Cron and also before every availability read and booking: on a quiet free host WP-Cron may not run for hours,
 * and the availability query already ignores expired holds, so this only makes the stored status catch up.
 */
final class HoldSweeper {

	/** WP-Cron entry point. */
	public static function cron() {
		self::run( time() );
	}

	/** @return int how many bookings were rejected */
	public static function run( int $now ): int {
		global $wpdb;
		$b = Schema::table( 'bookings' );
		// Look before writing: this runs on every request, and most of the time there is nothing to do.
		$any = $wpdb->get_var( $wpdb->prepare( "SELECT id FROM $b WHERE status = 'pending' AND hold_expires_utc <= %s LIMIT 1", Db::to_sql( $now ) ) );
		if ( ! $any ) {
			return 0;
		}

		$ids = Db::transaction(
			static function () use ( $wpdb, $b, $now ) {
				$ids = array_map(
					'intval',
					$wpdb->get_col( $wpdb->prepare( "SELECT id FROM $b WHERE status = 'pending' AND hold_expires_utc <= %s LIMIT 200 FOR UPDATE", Db::to_sql( $now ) ) )
				);
				if ( ! $ids ) {
					return array();
				}
				$in  = Db::ints( $ids );
				$sql = Db::to_sql( $now );
				$wpdb->query( $wpdb->prepare( "UPDATE $b SET status = 'rejected', hold_expires_utc = NULL, updated_utc = %s WHERE id IN ($in)", array_merge( array( $sql ), $ids ) ) );
				Db::check();
				$wpdb->query( $wpdb->prepare( 'UPDATE ' . Schema::table( 'booking_allocations' ) . " SET state = 'released' WHERE state = 'held' AND booking_id IN ($in)", $ids ) );
				Db::check();
				foreach ( $ids as $id ) {
					$wpdb->insert(
						Schema::table( 'status_history' ),
						array(
							'booking_id'  => $id,
							'from_status' => 'pending',
							'to_status'   => 'rejected',
							'note'        => 'The business did not answer in time; the hold expired.',
							'created_utc' => $sql,
						)
					);
					Db::check();
				}
				return $ids;
			}
		);

		foreach ( $ids as $id ) {
			do_action( 'jsb_booking_status_changed', $id, 'pending', 'rejected', 'system' );
		}
		return count( $ids );
	}
}
