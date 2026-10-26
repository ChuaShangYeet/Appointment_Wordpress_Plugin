<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/** Retail stock on salon tickets. Ported from backend/app/services/inventory.py. Runs inside the caller's transaction. */
final class Inventory {

	/**
	 * Takes every ticket line off the shelf: one atomic UPDATE per product, so concurrent completions cannot lose updates.
	 * Stock never goes below zero (two tickets that both claimed the last unit leave 0). A product that reaches zero turns
	 * from in_stock to out_of_stock; available_on_order keeps its status.
	 *
	 * MySQL applies SET assignments left to right, each seeing the ones before it, so stock_status is set first, from the
	 * quantity as it was, and only then is stock_qty lowered.
	 */
	public static function deduct_stock( int $booking_id ): void {
		global $wpdb;
		$p     = Schema::table( 'products' );
		$lines = $wpdb->get_results( $wpdb->prepare( 'SELECT product_id, quantity FROM ' . Schema::table( 'booking_products' ) . ' WHERE booking_id = %d', $booking_id ), ARRAY_A );
		foreach ( $lines as $l ) {
			$q = (int) $l['quantity'];
			$wpdb->query(
				$wpdb->prepare(
					"UPDATE $p SET
					   stock_status = CASE WHEN stock_status = 'in_stock' AND stock_qty - %d <= 0 THEN 'out_of_stock' ELSE stock_status END,
					   stock_qty = GREATEST(stock_qty - %d, 0)
					 WHERE id = %d",
					$q,
					$q,
					(int) $l['product_id']
				)
			);
			Db::check();
		}
	}

	/** On completion: deduct the ticket's stock and lock in the duration (base + every add-on, upsells included). */
	public static function finalise_completion( int $booking_id ): void {
		global $wpdb;
		self::deduct_stock( $booking_id );
		$wpdb->query(
			$wpdb->prepare(
				'UPDATE ' . Schema::table( 'bookings' ) . ' SET actual_duration_minutes = (SELECT COALESCE(SUM(duration_minutes), 0) FROM ' . Schema::table( 'booking_items' ) . ' WHERE booking_id = %d)
				 WHERE id = %d AND actual_duration_minutes IS NULL',
				$booking_id,
				$booking_id
			)
		);
		Db::check();
	}
}
