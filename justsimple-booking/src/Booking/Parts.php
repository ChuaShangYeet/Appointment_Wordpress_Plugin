<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/**
 * Parts on workshop jobs. Ported from backend/app/services/parts.py and automotive.lead_time_notice.
 *
 * - A service may need parts (service_products). Parts that are only available on order are shown to the customer, who must
 *   acknowledge the lead time before booking.
 * - A job's parts list is replaced as a whole while the job is confirmed, running or waiting for parts. A line is priced when it is
 *   first added and keeps that price when its quantity changes. Nothing leaves the shelf until the job is completed (Inventory).
 * - A line wanting more than the shelf holds is marked needs_ordering: the cue to put the job on "waiting for parts".
 */
final class Parts {

	const EDITABLE  = array( 'confirmed', 'in-progress', 'waiting-for-parts' );
	const MAX_LINES = 50;

	/**
	 * Parts the chosen services need that are only available on order.
	 *
	 * @param int[] $service_ids
	 * @return array<int,array{name:string,estimated_lead_time_days:?int}>
	 */
	public static function lead_time_notice( array $service_ids ): array {
		global $wpdb;
		if ( ! $service_ids ) {
			return array();
		}
		$rows = $wpdb->get_results(
			$wpdb->prepare(
				'SELECT DISTINCT p.name, p.estimated_lead_time_days FROM ' . Schema::table( 'products' ) . ' p JOIN ' . Schema::table( 'service_products' ) . " sp ON sp.product_id = p.id
				 WHERE p.stock_status = 'available_on_order' AND sp.service_id IN (" . Db::ints( $service_ids ) . ') ORDER BY p.name',
				$service_ids
			),
			ARRAY_A
		);
		return array_map( static function ( $r ) {
			return array( 'name' => $r['name'], 'estimated_lead_time_days' => null === $r['estimated_lead_time_days'] ? null : (int) $r['estimated_lead_time_days'] );
		}, $rows );
	}

	/** The job's parts list, with what the shelf holds now. */
	public static function lines( int $booking_id ): array {
		global $wpdb;
		$rows = $wpdb->get_results(
			$wpdb->prepare(
				'SELECT l.product_id, p.name, l.quantity, l.unit_price, p.stock_qty, p.estimated_lead_time_days FROM ' . Schema::table( 'booking_products' ) . ' l
				 JOIN ' . Schema::table( 'products' ) . ' p ON p.id = l.product_id WHERE l.booking_id = %d ORDER BY p.name, p.id',
				$booking_id
			),
			ARRAY_A
		);
		return array_map( static function ( $r ) {
			return array(
				'product_id'               => (int) $r['product_id'],
				'name'                     => $r['name'],
				'quantity'                 => (int) $r['quantity'],
				'unit_price'               => $r['unit_price'],
				'in_stock'                 => (int) $r['stock_qty'],
				'needs_ordering'           => (int) $r['quantity'] > (int) $r['stock_qty'],
				'estimated_lead_time_days' => null === $r['estimated_lead_time_days'] ? null : (int) $r['estimated_lead_time_days'],
			);
		}, $rows );
	}

	/**
	 * Makes the job's parts exactly `$wanted` (an empty list clears them). Everything is checked before anything is written, and
	 * the booking row is locked, so two edits (or an edit and a completion) apply one after the other.
	 *
	 * @param array<int,array{product_id:int,quantity:int}> $wanted
	 */
	public static function set( int $booking_id, array $wanted ): array {
		global $wpdb;
		if ( count( $wanted ) > self::MAX_LINES ) {
			throw new EngineError( EngineError::VALIDATION, sprintf( 'At most %d different parts on one job.', self::MAX_LINES ), array( 'field' => 'parts' ) );
		}
		$clean = array();
		foreach ( $wanted as $i => $w ) {
			$pid = (int) ( $w['product_id'] ?? 0 );
			$qty = $w['quantity'] ?? 1;
			if ( $pid <= 0 || ! is_numeric( $qty ) || (int) $qty != $qty || $qty < 1 || $qty > 1000 ) { // phpcs:ignore Universal.Operators.StrictComparisons -- "2" and 2 are both fine
				throw new EngineError( EngineError::VALIDATION, 'Each part needs a product_id and a quantity from 1 to 1000.', array( 'field' => "parts[$i]" ) );
			}
			if ( isset( $clean[ $pid ] ) ) {
				throw new EngineError( EngineError::VALIDATION, 'Each part can appear once; give the total quantity.', array( 'field' => "parts[$i]" ) );
			}
			$clean[ $pid ] = (int) $qty;
		}

		Db::transaction(
			static function () use ( $wpdb, $booking_id, $clean ) {
				$b = $wpdb->get_row( $wpdb->prepare( 'SELECT b.status, b.module_id, m.type FROM ' . Schema::table( 'bookings' ) . ' b JOIN ' . Schema::table( 'modules' ) . ' m ON m.id = b.module_id WHERE b.id = %d FOR UPDATE', $booking_id ), ARRAY_A );
				if ( ! $b ) {
					throw new EngineError( EngineError::NOT_FOUND, 'Booking not found.' );
				}
				if ( 'workshop' !== $b['type'] ) {
					throw new EngineError( EngineError::VALIDATION, 'A parts list belongs to workshop jobs.' );
				}
				if ( ! in_array( $b['status'], self::EDITABLE, true ) ) {
					throw new EngineError( EngineError::INVALID_TRANSITION, sprintf( "Parts can be changed on a confirmed, running or waiting-for-parts job (status is '%s').", $b['status'] ) );
				}
				$ids    = array_keys( $clean );
				$prices = array();
				if ( $ids ) {
					foreach ( $wpdb->get_results( $wpdb->prepare( 'SELECT id, price FROM ' . Schema::table( 'products' ) . ' WHERE module_id = %d AND id IN (' . Db::ints( $ids ) . ')', array_merge( array( (int) $b['module_id'] ), $ids ) ), ARRAY_A ) as $p ) {
						$prices[ (int) $p['id'] ] = $p['price'];
					}
					if ( count( $prices ) !== count( $ids ) ) {
						throw new EngineError( EngineError::NOT_FOUND, 'Unknown part.', array( 'field' => 'parts' ) );
					}
				}
				$t = Schema::table( 'booking_products' );
				$wpdb->query( $wpdb->prepare( "DELETE FROM $t WHERE booking_id = %d" . ( $ids ? ' AND product_id NOT IN (' . Db::ints( $ids ) . ')' : '' ), array_merge( array( $booking_id ), $ids ) ) );
				Db::check();
				foreach ( $clean as $pid => $qty ) { // a line already on the job keeps the price it was added at
					$wpdb->query( $wpdb->prepare( "INSERT INTO $t (booking_id, product_id, quantity, unit_price) VALUES (%d, %d, %d, %s) ON DUPLICATE KEY UPDATE quantity = VALUES(quantity)", $booking_id, $pid, $qty, $prices[ $pid ] ) );
					Db::check();
				}
			}
		);
		return self::lines( $booking_id );
	}
}
