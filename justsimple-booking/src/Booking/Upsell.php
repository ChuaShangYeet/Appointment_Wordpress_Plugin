<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/**
 * On-the-spot upsell for salon appointments. Ported from backend/app/services/upsell.py; the stretch-or-flag itself is Extend (shared with field overruns).
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
					list( $extended, $clash_ids ) = $added ? Extend::try_extend( $booking_id, 60 * $added, $now ) : array( false, array() );
					if ( $clash_ids ) {
						Extend::flag( $booking_id, $clash_ids, $now );
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
			'conflicts'     => Extend::clash_details( $clash_ids ),
		);
	}
}
