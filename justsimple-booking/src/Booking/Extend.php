<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\Clock;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/**
 * Making a running job longer without forcing it into anyone else's time (booking.try_extend in the engine).
 * Used by salon upsell (an add-on) and field-service overruns (actual duration). Callers hold the per-module lock and run inside
 * a transaction, so the clash check and the write cannot be split by a new booking.
 */
final class Extend {

	/**
	 * Stretch every live allocation of the booking, and its end, by `$delta` seconds if no other live allocation of the same resource
	 * lies in that time. Returns [extended, clashing booking ids]; with clashes nothing is changed.
	 */
	public static function try_extend( int $booking_id, int $delta, int $now ): array {
		global $wpdb;
		$a    = Schema::table( 'booking_allocations' );
		$live = $wpdb->get_results( $wpdb->prepare( "SELECT id, resource_id, end_utc FROM $a WHERE booking_id = %d AND state IN ('held','active') FOR UPDATE", $booking_id ), ARRAY_A );
		if ( ! $live ) {
			return array( false, array() );
		}
		$clashes = array();
		foreach ( $live as $l ) {
			$end   = Db::from_sql( $l['end_utc'] );
			$found = $wpdb->get_col(
				$wpdb->prepare(
					"SELECT DISTINCT o.booking_id FROM $a o JOIN " . Schema::table( 'bookings' ) . " ob ON ob.id = o.booking_id
					 WHERE o.resource_id = %d AND o.booking_id <> %d AND o.state IN ('held','active') AND o.start_utc < %s AND o.end_utc > %s
					   AND (o.state = 'active' OR ob.hold_expires_utc > %s)",
					(int) $l['resource_id'],
					$booking_id,
					Db::to_sql( $end + $delta ),
					Db::to_sql( $end ),
					Db::to_sql( $now )
				)
			);
			$clashes = array_merge( $clashes, array_map( 'intval', $found ) );
		}
		$clashes = array_values( array_unique( $clashes ) );
		sort( $clashes );
		if ( $clashes ) {
			return array( false, $clashes );
		}
		foreach ( $live as $l ) {
			$wpdb->update( $a, array( 'end_utc' => Db::to_sql( Db::from_sql( $l['end_utc'] ) + $delta ) ), array( 'id' => (int) $l['id'] ) );
			Db::check();
		}
		$b   = Schema::table( 'bookings' );
		$end = Db::from_sql( (string) $wpdb->get_var( $wpdb->prepare( "SELECT end_utc FROM $b WHERE id = %d", $booking_id ) ) );
		$wpdb->update( $b, array( 'end_utc' => Db::to_sql( $end + $delta ), 'updated_utc' => Db::to_sql( $now ) ), array( 'id' => $booking_id ) );
		Db::check();
		return array( true, array() );
	}

	/** Flag the booking and the ones it ran into for a human to sort out; nothing is moved. */
	public static function flag( int $booking_id, array $clash_ids, int $now ): void {
		global $wpdb;
		$ids = array_merge( array( $booking_id ), $clash_ids );
		$wpdb->query( $wpdb->prepare( 'UPDATE ' . Schema::table( 'bookings' ) . ' SET schedule_conflict = 1, updated_utc = %s WHERE id IN (' . Db::ints( $ids ) . ')', array_merge( array( Db::to_sql( $now ) ), $ids ) ) );
		Db::check();
	}

	/** The appointments a longer job ran into, for the alert. */
	public static function clash_details( array $ids ): array {
		global $wpdb;
		if ( ! $ids ) {
			return array();
		}
		$tz   = wp_timezone();
		$rows = $wpdb->get_results( $wpdb->prepare( 'SELECT b.id, b.status, b.start_utc, b.end_utc, c.name FROM ' . Schema::table( 'bookings' ) . ' b JOIN ' . Schema::table( 'customers' ) . ' c ON c.id = b.customer_id WHERE b.id IN (' . Db::ints( $ids ) . ') ORDER BY b.start_utc', $ids ), ARRAY_A );
		return array_map( static function ( $r ) use ( $wpdb, $tz ) {
			$names = $wpdb->get_col( $wpdb->prepare( 'SELECT s.name FROM ' . Schema::table( 'booking_items' ) . ' i JOIN ' . Schema::table( 'services' ) . ' s ON s.id = i.service_id WHERE i.booking_id = %d ORDER BY s.name', (int) $r['id'] ) );
			return array(
				'booking_id'    => (int) $r['id'],
				'status'        => $r['status'],
				'start'         => Clock::format( Db::from_sql( $r['start_utc'] ), $tz ),
				'end'           => Clock::format( Db::from_sql( $r['end_utc'] ), $tz ),
				'customer_name' => $r['name'],
				'service_names' => $names,
			);
		}, $rows );
	}
}
