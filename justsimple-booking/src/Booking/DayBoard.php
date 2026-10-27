<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Data\WpScheduleSource;
use JustSimple\Booking\Engine\Clock;
use JustSimple\Booking\Engine\Interval;
use JustSimple\Booking\Engine\IntervalMath;

defined( 'ABSPATH' ) || exit;

/**
 * One module's day as the admin's day board draws it: the things being booked down the side (stylists, tables, mechanics and
 * bays, professionals), each with the hours it works that day, and one bar per allocation. Working hours come from the same
 * rules the engine books by (Clock::shift_windows, days off, opening hours), so the board and the free times never disagree.
 *
 * A field visit's bar is wider than its visit: the travel time either side is the allocation outside the booking's own window.
 */
final class DayBoard {

	const RESOURCE_TYPES = array(
		'staff'      => array( 'staff' ),
		'salon'      => array( 'staff' ),
		'field'      => array( 'staff' ),
		'restaurant' => array( 'table' ),
		'workshop'   => array( 'staff', 'bay' ),
	);

	/** Bookings that no longer hold anyone are left off the board. */
	const GONE = array( 'cancelled', 'rejected' );

	public static function build( array $module, string $day, int $user_id, ?int $now = null ): array {
		global $wpdb;
		$now    = $now ?? time();
		$tz     = wp_timezone();
		$source = new WpScheduleSource();
		$mine   = Access::own_resources( $user_id );
		$lo     = Clock::clock_on( $day, '00:00', $tz );
		$hi     = Clock::clock_on( Clock::add_days( $day, 1 ), '00:00', $tz );

		// opening hours; null = no hours set, so the day is not limited by them
		$open = null;
		$rows = $source->operating_hours( $module['id'] );
		if ( $rows ) {
			$open = array();
			foreach ( $rows as $r ) {
				if ( (int) $r['weekday'] === Clock::weekday_index( $day ) && Clock::hms( $r['open_time'] ) !== Clock::hms( $r['close_time'] ) ) {
					$open[] = Clock::window_on( $day, $r['open_time'], $r['close_time'], $tz );
				}
			}
		}
		$closed   = false;
		$on_leave = array();
		foreach ( $source->days_off( $module['id'] ) as $o ) {
			if ( Clock::covers_date( $o, $day ) ) {
				if ( null === $o['resource_id'] ) {
					$closed = true;
				} else {
					$on_leave[ $o['resource_id'] ] = true;
				}
			}
		}

		$types = self::RESOURCE_TYPES[ $module['type'] ];
		$res   = $wpdb->get_results(
			$wpdb->prepare(
				'SELECT id, type, name FROM ' . Schema::table( 'resources' ) . " WHERE module_id = %d AND status = 'active' AND type IN (" . implode( ',', array_fill( 0, count( $types ), '%s' ) ) . ') ORDER BY FIELD(type, ' . implode( ',', array_fill( 0, count( $types ), '%s' ) ) . '), sort_order, id', // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
				array_merge( array( $module['id'] ), $types, $types )
			),
			ARRAY_A
		);
		$shifts = array();
		foreach ( $source->shifts( array_map( 'intval', array_column( $res, 'id' ) ), Clock::weekday_index( $day ) ) as $s ) {
			foreach ( Clock::shift_windows( $s, $day, $tz ) as $w ) {
				$shifts[ (int) $s['resource_id'] ][] = $w;
			}
		}
		$whole     = array( new Interval( $lo, $hi ) );
		$resources = array();
		foreach ( $res as $r ) {
			$id = (int) $r['id'];
			if ( $closed || isset( $on_leave[ $id ] ) ) {
				$working = array();
			} elseif ( 'staff' === $r['type'] ) {
				$working = null === $open ? ( $shifts[ $id ] ?? array() ) : IntervalMath::intersect( $shifts[ $id ] ?? array(), $open );
			} else {
				$working = $open ?? $whole; // tables and bays have no shifts: they are there whenever the business is open
			}
			$resources[] = array(
				'id'      => $id,
				'kind'    => $r['type'],
				'name'    => $r['name'],
				'mine'    => null !== $mine && in_array( $id, $mine, true ),
				'off'     => $closed ? 'closed' : ( isset( $on_leave[ $id ] ) ? 'leave' : null ),
				'working' => array_map( static function ( Interval $w ) use ( $tz ) {
					return array( Clock::format( $w->start, $tz ), Clock::format( $w->end, $tz ) );
				}, $working ),
			);
		}
		// their own rows first, in the order the owner set
		usort( $resources, static function ( $a, $b ) {
			return (int) $b['mine'] - (int) $a['mine'];
		} );

		// allocations that touch the day; a completed or missed booking keeps its released rows, so the day stays as it happened
		$ids = array_column( $resources, 'id' );
		$out = array();
		if ( $ids ) {
			$rows = $wpdb->get_results(
				$wpdb->prepare(
					'SELECT a.booking_id, a.resource_id, a.start_utc, a.end_utc, b.start_utc AS b_start, b.end_utc AS b_end, b.status, b.schedule_conflict, b.party_size, c.name AS customer
					 FROM ' . Schema::table( 'booking_allocations' ) . ' a JOIN ' . Schema::table( 'bookings' ) . ' b ON b.id = a.booking_id JOIN ' . Schema::table( 'customers' ) . ' c ON c.id = b.customer_id
					 WHERE a.resource_id IN (' . Db::ints( $ids ) . ") AND a.start_utc < %s AND a.end_utc > %s
					   AND b.status NOT IN ('cancelled','rejected') AND (a.state <> 'released' OR b.status IN ('completed','no-show'))
					 ORDER BY a.start_utc, a.booking_id", // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
					array_merge( $ids, array( Db::to_sql( $hi ), Db::to_sql( $lo ) ) )
				),
				ARRAY_A
			);
			$services = self::service_names( array_unique( array_map( 'intval', array_column( $rows, 'booking_id' ) ) ) );
			$own      = self::own_bookings( array_unique( array_map( 'intval', array_column( $rows, 'booking_id' ) ) ), $mine );
			foreach ( $rows as $r ) {
				$bid   = (int) $r['booking_id'];
				$start = Db::from_sql( $r['start_utc'] );
				$end   = Db::from_sql( $r['end_utc'] );
				$mine_ = isset( $own[ $bid ] );
				$who   = $mine_ ? $r['customer'] : Access::first_name( $r['customer'] );
				$out[] = array(
					'booking_id'    => $bid,
					'resource_id'   => (int) $r['resource_id'],
					'start'         => Clock::format( $start, $tz ),
					'end'           => Clock::format( $end, $tz ),
					'service_start' => Clock::format( max( $start, Db::from_sql( $r['b_start'] ) ), $tz ),
					'service_end'   => Clock::format( min( $end, Db::from_sql( $r['b_end'] ) ), $tz ),
					'status'        => $r['status'],
					'customer'      => $who,
					'what'          => null === $r['party_size'] ? ( $services[ $bid ] ?? '' ) : sprintf( _n( '%d person', '%d people', (int) $r['party_size'], 'justsimple-booking' ), (int) $r['party_size'] ),
					'clash'         => (bool) (int) $r['schedule_conflict'],
					'mine'          => $mine_,
				);
			}
		}

		return array(
			'date'      => $day,
			'now'       => Clock::format( $now, $tz ),
			'closed'    => $closed,
			'open'      => null === $open ? null : array_map( static function ( Interval $w ) use ( $tz ) {
				return array( Clock::format( $w->start, $tz ), Clock::format( $w->end, $tz ) );
			}, $open ),
			'resources' => $resources,
			'bars'      => $out,
			'counts'    => self::counts( $module['id'], $lo, $hi ),
			'needs_you' => self::needs_you( $module['id'], $mine, $now, $tz ),
		);
	}

	/** Today's bookings by status, for the finding line. */
	private static function counts( int $module_id, int $lo, int $hi ): array {
		global $wpdb;
		$rows = $wpdb->get_results( $wpdb->prepare( 'SELECT status, COUNT(*) AS n FROM ' . Schema::table( 'bookings' ) . ' WHERE module_id = %d AND start_utc >= %s AND start_utc < %s GROUP BY status', $module_id, Db::to_sql( $lo ), Db::to_sql( $hi ) ), ARRAY_A );
		$out  = array_fill_keys( Schema::BOOKING_STATUSES, 0 );
		foreach ( $rows as $r ) {
			$out[ $r['status'] ] = (int) $r['n'];
		}
		$out['total'] = array_sum( array_diff_key( $out, array_flip( self::GONE ) ) );
		return $out;
	}

	/**
	 * What waits on a person, from now on: requests to confirm (soonest hold expiry first), bookings that run into the next one,
	 * jobs waiting for parts. A colleague's items are left out: they could not act on them.
	 */
	private static function needs_you( int $module_id, ?array $mine, int $now, \DateTimeZone $tz ): array {
		global $wpdb;
		$b    = Schema::table( 'bookings' );
		$rows = $wpdb->get_results(
			$wpdb->prepare(
				"SELECT b.id, b.status, b.start_utc, b.hold_expires_utc, b.schedule_conflict, c.name AS customer FROM $b b JOIN " . Schema::table( 'customers' ) . " c ON c.id = b.customer_id
				 WHERE b.module_id = %d AND (
					(b.status = 'pending' AND b.hold_expires_utc > %s)
					OR (b.schedule_conflict = 1 AND b.status IN ('confirmed','in-progress') AND b.end_utc > %s)
					OR b.status = 'waiting-for-parts')
				 ORDER BY b.status = 'pending' DESC, COALESCE(b.hold_expires_utc, b.start_utc), b.id LIMIT 50", // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
				$module_id,
				Db::to_sql( $now ),
				Db::to_sql( $now )
			),
			ARRAY_A
		);
		$own = self::own_bookings( array_map( 'intval', array_column( $rows, 'id' ) ), $mine );
		$out = array();
		foreach ( $rows as $r ) {
			if ( ! isset( $own[ (int) $r['id'] ] ) ) {
				continue;
			}
			$out[] = array(
				'booking_id'   => (int) $r['id'],
				'why'          => 'pending' === $r['status'] ? 'request' : ( 'waiting-for-parts' === $r['status'] ? 'parts' : 'clash' ),
				'start'        => Clock::format( Db::from_sql( $r['start_utc'] ), $tz ),
				'hold_expires' => $r['hold_expires_utc'] ? Clock::format( Db::from_sql( $r['hold_expires_utc'] ), $tz ) : null,
				'customer'     => $r['customer'],
			);
		}
		return $out;
	}

	/** @return array<int,string> booking id => "Haircut + Gel finish" */
	private static function service_names( array $booking_ids ): array {
		global $wpdb;
		if ( ! $booking_ids ) {
			return array();
		}
		$rows = $wpdb->get_results( $wpdb->prepare( 'SELECT i.booking_id, s.name FROM ' . Schema::table( 'booking_items' ) . ' i JOIN ' . Schema::table( 'services' ) . ' s ON s.id = i.service_id WHERE i.booking_id IN (' . Db::ints( $booking_ids ) . ') ORDER BY i.booking_id, i.seq', $booking_ids ), ARRAY_A ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		$out  = array();
		foreach ( $rows as $r ) {
			$out[ (int) $r['booking_id'] ][] = $r['name'];
		}
		return array_map( static function ( $names ) {
			return implode( ' + ', $names );
		}, $out );
	}

	/** @return array<int,true> the bookings among `$booking_ids` that `$mine` may see in full (all of them when null) */
	private static function own_bookings( array $booking_ids, ?array $mine ): array {
		global $wpdb;
		if ( ! $booking_ids ) {
			return array();
		}
		if ( null === $mine ) {
			return array_fill_keys( $booking_ids, true );
		}
		$ids = $wpdb->get_col( $wpdb->prepare( 'SELECT DISTINCT booking_id FROM ' . Schema::table( 'booking_allocations' ) . ' WHERE booking_id IN (' . Db::ints( $booking_ids ) . ') AND resource_id IN (' . Db::ints( $mine ) . ')', array_merge( $booking_ids, $mine ) ) ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		return array_fill_keys( array_map( 'intval', $ids ), true );
	}
}
