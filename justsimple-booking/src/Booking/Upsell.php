<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\Clock;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/**
 * On-the-spot upsell for salon appointments. Ported from backend/app/services/upsell.py and booking.try_extend.
 *
 * Once the appointment is in progress the stylist can add add-ons and retail products to the ticket. An add-on stretches the
 * stylist's reservation if nothing else of theirs is live in the extra time; otherwise nothing is forced into the other
 * customer's time: the add-on still goes on the ticket, both appointments are flagged schedule_conflict, and the clash is
 * reported so the stylist knows whose appointment it is.
 *
 * Extending competes with new bookings for the stylist's time, so it takes the same per-module lock as BookingService::create
 * and runs in one transaction with the booking row locked: concurrent upsells apply one after the other, and no booking can
 * slip in between the clash check and the extension.
 */
final class Upsell {

	/**
	 * @param int[]                                      $service_ids add-ons
	 * @param array<int,array{product_id:int,quantity:int}> $products
	 * @return array{booking:array,extended:bool,added_minutes:int,conflicts:array}
	 */
	public static function add_to_ticket( int $booking_id, array $service_ids, array $products, ?int $now = null ): array {
		global $wpdb;
		$now = $now ?? time();
		$row = $wpdb->get_row( $wpdb->prepare( 'SELECT b.module_id, m.type FROM ' . Schema::table( 'bookings' ) . ' b JOIN ' . Schema::table( 'modules' ) . ' m ON m.id = b.module_id WHERE b.id = %d', $booking_id ), ARRAY_A );
		if ( ! $row ) {
			throw new EngineError( EngineError::NOT_FOUND, 'Booking not found.' );
		}
		if ( 'salon' !== $row['type'] ) {
			throw new EngineError( EngineError::INVALID_TRANSITION, 'On-the-spot upsell applies to salon appointments only.' );
		}
		$service_ids = array_values( array_unique( array_map( 'intval', $service_ids ) ) );
		$wanted      = array();
		foreach ( $products as $p ) {
			$pid = (int) ( $p['product_id'] ?? 0 );
			$qty = (int) ( $p['quantity'] ?? 1 );
			if ( $pid <= 0 || $qty < 1 || $qty > 1000 ) {
				throw new EngineError( EngineError::VALIDATION, 'Each product needs a product_id and a quantity from 1 to 1000.', array( 'field' => 'products' ) );
			}
			$wanted[ $pid ] = ( $wanted[ $pid ] ?? 0 ) + $qty;
		}
		if ( ! $service_ids && ! $wanted ) {
			throw new EngineError( EngineError::VALIDATION, 'Add at least one add-on or product.' );
		}

		HoldSweeper::run( $now );
		$module_id = (int) $row['module_id'];
		$lock      = 'jsb:module:' . $module_id;
		if ( '1' !== (string) $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, %d)', $lock, BookingService::LOCK_WAIT ) ) ) {
			throw new EngineError( EngineError::BUSY, 'The schedule is busy. Please try again in a moment.' );
		}
		try {
			$result = Db::transaction(
				static function () use ( $wpdb, $booking_id, $module_id, $service_ids, $wanted, $now ) {
					$b      = Schema::table( 'bookings' );
					$status = $wpdb->get_var( $wpdb->prepare( "SELECT status FROM $b WHERE id = %d FOR UPDATE", $booking_id ) );
					if ( 'in-progress' !== $status ) {
						throw new EngineError( EngineError::INVALID_TRANSITION, sprintf( "Items can be added once the appointment is in progress (status is '%s').", $status ) );
					}

					// validate everything before writing anything
					$addons = array();
					if ( $service_ids ) {
						$rows = $wpdb->get_results( $wpdb->prepare( 'SELECT id, name, duration_minutes, buffer_minutes, is_addon FROM ' . Schema::table( 'services' ) . ' WHERE module_id = %d AND active = 1 AND id IN (' . Db::ints( $service_ids ) . ')', array_merge( array( $module_id ), $service_ids ) ), ARRAY_A );
						if ( count( $rows ) !== count( $service_ids ) ) {
							throw new EngineError( EngineError::NOT_FOUND, 'Unknown or unavailable service.' );
						}
						foreach ( $rows as $s ) {
							if ( ! (int) $s['is_addon'] ) {
								throw new EngineError( EngineError::VALIDATION, sprintf( "'%s' is a base service; only add-ons can be added to a running appointment.", $s['name'] ), array( 'field' => 'service_ids' ) );
							}
						}
						$addons = $rows;
					}
					$bp       = Schema::table( 'booking_products' );
					$on_ticket = array();
					foreach ( $wpdb->get_results( $wpdb->prepare( "SELECT product_id, quantity FROM $bp WHERE booking_id = %d", $booking_id ), ARRAY_A ) as $line ) {
						$on_ticket[ (int) $line['product_id'] ] = (int) $line['quantity'];
					}
					$stock = array();
					foreach ( $wanted as $pid => $qty ) {
						$p = $wpdb->get_row( $wpdb->prepare( 'SELECT id, name, price, stock_qty, stock_status FROM ' . Schema::table( 'products' ) . ' WHERE id = %d AND module_id = %d', $pid, $module_id ), ARRAY_A );
						if ( ! $p ) {
							throw new EngineError( EngineError::NOT_FOUND, 'Unknown product.' );
						}
						if ( 'out_of_stock' === $p['stock_status'] || ( $on_ticket[ $pid ] ?? 0 ) + $qty > (int) $p['stock_qty'] ) {
							throw new EngineError( EngineError::INSUFFICIENT_STOCK, sprintf( "Only %d of '%s' in stock.", (int) $p['stock_qty'], $p['name'] ), array( 'product_id' => $pid, 'in_stock' => (int) $p['stock_qty'] ) );
						}
						$stock[ $pid ] = $p;
					}

					// the ticket: products at today's price (kept when the quantity grows), add-ons marked as upsells
					foreach ( $wanted as $pid => $qty ) {
						$wpdb->query( $wpdb->prepare( "INSERT INTO $bp (booking_id, product_id, quantity, unit_price) VALUES (%d, %d, %d, %s) ON DUPLICATE KEY UPDATE quantity = quantity + VALUES(quantity)", $booking_id, $pid, $qty, $stock[ $pid ]['price'] ) );
						Db::check();
					}
					$seq = (int) $wpdb->get_var( $wpdb->prepare( 'SELECT COALESCE(MAX(seq), -1) FROM ' . Schema::table( 'booking_items' ) . ' WHERE booking_id = %d', $booking_id ) );
					foreach ( $addons as $s ) {
						$wpdb->insert(
							Schema::table( 'booking_items' ),
							array(
								'booking_id'       => $booking_id,
								'service_id'       => (int) $s['id'],
								'duration_minutes' => (int) $s['duration_minutes'],
								'buffer_minutes'   => (int) $s['buffer_minutes'],
								'seq'              => ++$seq,
								'is_upsell'        => 1,
							)
						);
						Db::check();
					}

					$added = array_sum( array_map( static function ( $s ) {
						return (int) $s['duration_minutes'] + (int) $s['buffer_minutes'];
					}, $addons ) );
					list( $extended, $clash_ids ) = $added ? self::try_extend( $booking_id, 60 * $added, $now ) : array( false, array() );
					if ( $clash_ids ) {
						$ids = array_merge( array( $booking_id ), $clash_ids );
						$wpdb->query( $wpdb->prepare( "UPDATE $b SET schedule_conflict = 1, updated_utc = %s WHERE id IN (" . Db::ints( $ids ) . ')', array_merge( array( Db::to_sql( $now ) ), $ids ) ) );
						Db::check();
					}
					return array( $extended, $added, $clash_ids );
				}
			);
		} finally {
			$wpdb->query( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) );
		}

		list( $extended, $added, $clash_ids ) = $result;
		if ( $clash_ids ) {
			do_action( 'jsb_schedule_conflict', $booking_id, $clash_ids );
		}
		return array(
			'booking'       => BookingService::view( $booking_id, true, $now ),
			'extended'      => $extended,
			'added_minutes' => $added,
			'conflicts'     => self::clash_details( $clash_ids ),
		);
	}

	/**
	 * Stretch every live allocation of the booking by `$delta` seconds if no other live allocation of the same resource lies in
	 * that time. Returns [extended, clashing booking ids]. Called inside the module lock, so the check and the write cannot be split.
	 */
	private static function try_extend( int $booking_id, int $delta, int $now ): array {
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

	/** The appointments an upsell ran into, for the stylist's alert. */
	private static function clash_details( array $ids ): array {
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
