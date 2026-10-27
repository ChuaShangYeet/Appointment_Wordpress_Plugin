<?php
namespace JustSimple\Booking\Data;

use JustSimple\Booking\Engine\Interval;
use JustSimple\Booking\Engine\MinimumSpend;
use JustSimple\Booking\Engine\RestaurantSource;
use JustSimple\Booking\Engine\WorkshopSource;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/** The engine's view of the jsb_ tables. Reads committed rows only; BookingService calls it inside its lock. */
final class WpScheduleSource implements RestaurantSource, WorkshopSource {

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

	public function resources( array $ids ): array {
		global $wpdb;
		if ( ! $ids ) {
			return array();
		}
		$out = array();
		foreach ( $wpdb->get_results( $wpdb->prepare( 'SELECT id, name, type FROM ' . Schema::table( 'resources' ) . ' WHERE id IN (' . Db::ints( $ids ) . ')', $ids ), ARRAY_A ) as $r ) {
			$out[ (int) $r['id'] ] = array( 'name' => $r['name'], 'type' => $r['type'] );
		}
		return $out;
	}

	public function tables( int $module_id ): array {
		global $wpdb;
		$rows = $wpdb->get_results(
			$wpdb->prepare( 'SELECT id, name, capacity, status, attributes FROM ' . Schema::table( 'resources' ) . " WHERE module_id = %d AND type = 'table' ORDER BY id", $module_id ),
			ARRAY_A
		);
		return array_map( static function ( $r ) {
			$attrs = json_decode( (string) $r['attributes'], true );
			$attrs = is_array( $attrs ) ? $attrs : array();
			return array(
				'id'            => (int) $r['id'],
				'name'          => $r['name'],
				'capacity'      => (int) $r['capacity'],
				'status'        => $r['status'],
				'is_vip'        => ! empty( $attrs['is_vip'] ),
				'minimum_spend' => MinimumSpend::to_cents( $attrs['minimum_spend'] ?? 0 ) ?? 0,
			);
		}, $rows );
	}

	public function slot_settings( int $module_id ): ?array {
		global $wpdb;
		$r = $wpdb->get_row( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'slot_settings' ) . ' WHERE module_id = %d', $module_id ), ARRAY_A );
		if ( ! $r ) {
			return null;
		}
		return array(
			'booking_mode'          => $r['booking_mode'],
			'slot_duration_minutes' => (int) $r['slot_duration_minutes'],
			'slot_interval_minutes' => (int) $r['slot_interval_minutes'],
			'first_slot_time'       => $r['first_slot_time'],
			'last_slot_time'        => $r['last_slot_time'],
			'buffer_minutes'        => (int) $r['buffer_minutes'],
			'allow_table_selection' => (bool) (int) $r['allow_table_selection'],
			'policy_id'             => null === $r['policy_id'] ? null : (int) $r['policy_id'],
		);
	}

	public function open_rules( int $module_id, string $table_type ): array {
		global $wpdb;
		$rows = $wpdb->get_results(
			$wpdb->prepare( 'SELECT weekday, specific_date, open_tables FROM ' . Schema::table( 'table_open_rules' ) . ' WHERE module_id = %d AND table_type = %s', $module_id, $table_type ),
			ARRAY_A
		);
		return array_map( static function ( $r ) {
			return array(
				'weekday'       => null === $r['weekday'] ? null : (int) $r['weekday'],
				'specific_date' => $r['specific_date'],
				'open_tables'   => (int) $r['open_tables'],
			);
		}, $rows );
	}
}
