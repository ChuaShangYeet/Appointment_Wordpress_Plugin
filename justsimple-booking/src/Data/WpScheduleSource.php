<?php
namespace JustSimple\Booking\Data;

use JustSimple\Booking\Engine\Interval;
use JustSimple\Booking\Engine\ScheduleSource;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/** The engine's view of the jsb_ tables. Reads committed rows only; BookingService calls it inside its lock. */
final class WpScheduleSource implements ScheduleSource {

	public function days_off( int $module_id ): array {
		global $wpdb;
		$rows = $wpdb->get_results(
			$wpdb->prepare( 'SELECT resource_id, start_date, end_date, repeat_yearly FROM ' . Schema::table( 'days_off' ) . ' WHERE module_id = %d', $module_id ),
			ARRAY_A
		);
		foreach ( $rows as &$r ) {
			$r['resource_id'] = null === $r['resource_id'] ? null : (int) $r['resource_id'];
		}
		return $rows;
	}

	public function operating_hours( int $module_id ): array {
		global $wpdb;
		return $wpdb->get_results(
			$wpdb->prepare( 'SELECT weekday, open_time, close_time FROM ' . Schema::table( 'operating_hours' ) . ' WHERE module_id = %d', $module_id ),
			ARRAY_A
		);
	}

	public function qualified_resource_ids( int $module_id, string $type, array $service_ids, ?int $only_id ): array {
		global $wpdb;
		$r   = Schema::table( 'resources' );
		$sql = $wpdb->prepare( "SELECT id FROM $r WHERE module_id = %d AND type = %s AND status = 'active'", $module_id, $type );
		if ( null !== $only_id ) {
			$sql .= $wpdb->prepare( ' AND id = %d', $only_id );
		}
		$candidates = array_map( 'intval', $wpdb->get_col( $sql ) );
		if ( ! $candidates || ! $service_ids ) {
			return $candidates;
		}

		// Links are filtered by type, so a service linked only to a bay never disqualifies every mechanic.
		$links  = $wpdb->get_results(
			$wpdb->prepare(
				'SELECT sr.service_id, sr.resource_id FROM ' . Schema::table( 'service_resources' ) . " sr JOIN $r res ON res.id = sr.resource_id
				 WHERE res.type = %s AND sr.service_id IN (" . Db::ints( $service_ids ) . ')',
				array_merge( array( $type ), $service_ids )
			),
			ARRAY_A
		);
		$linked = array();
		foreach ( $links as $l ) {
			$linked[ (int) $l['service_id'] ][] = (int) $l['resource_id'];
		}
		$common = $candidates;
		foreach ( $service_ids as $sid ) {
			if ( isset( $linked[ $sid ] ) ) {
				$common = array_values( array_intersect( $common, $linked[ $sid ] ) );
			}
		}
		sort( $common );
		return $common;
	}

	public function shifts( array $resource_ids, int $weekday ): array {
		global $wpdb;
		if ( ! $resource_ids ) {
			return array();
		}
		$rows = $wpdb->get_results(
			$wpdb->prepare(
				'SELECT resource_id, start_time, end_time, break_start, break_end FROM ' . Schema::table( 'shifts' ) .
				' WHERE weekday = %d AND resource_id IN (' . Db::ints( $resource_ids ) . ')',
				array_merge( array( $weekday ), $resource_ids )
			),
			ARRAY_A
		);
		foreach ( $rows as &$row ) {
			$row['resource_id'] = (int) $row['resource_id'];
		}
		return $rows;
	}

	public function busy( array $resource_ids, int $lo, int $hi, int $now ): array {
		global $wpdb;
		if ( ! $resource_ids ) {
			return array();
		}
		$rows = $wpdb->get_results(
			$wpdb->prepare(
				'SELECT a.resource_id, a.start_utc, a.end_utc FROM ' . Schema::table( 'booking_allocations' ) . ' a
				 JOIN ' . Schema::table( 'bookings' ) . " b ON b.id = a.booking_id
				 WHERE a.resource_id IN (" . Db::ints( $resource_ids ) . ")
				   AND a.state IN ('held','active') AND a.start_utc < %s AND a.end_utc > %s
				   AND (a.state = 'active' OR (b.hold_expires_utc IS NOT NULL AND b.hold_expires_utc > %s))",
				array_merge( $resource_ids, array( Db::to_sql( $hi ), Db::to_sql( $lo ), Db::to_sql( $now ) ) )
			),
			ARRAY_A
		);
		$out = array();
		foreach ( $rows as $row ) {
			$out[ (int) $row['resource_id'] ][] = new Interval( Db::from_sql( $row['start_utc'] ), Db::from_sql( $row['end_utc'] ) );
		}
		return $out;
	}
}
