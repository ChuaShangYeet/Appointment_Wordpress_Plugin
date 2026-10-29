<?php
namespace JustSimple\Showcase;

use JustSimple\Booking\Data\Schema;

defined( 'ABSPATH' ) || exit;

/**
 * Puts the demo back to fresh: removes the demo booking pages (and with them every booking visitors made), removes customers
 * left with no booking (visitors type real names, phones and emails into a public demo; they should not pile up), then builds
 * the five pages again. Runs from the admin page's button and nightly through WP-Cron.
 */
final class Reset {

	const HOOK   = 'jss_nightly_reset';
	const PAGES  = 'jss_pages';     // kind => booking page id
	const LAST   = 'jss_last_reset'; // { at, pages, errors, seconds }
	const HOUR   = 'jss_reset_hour'; // 0-23 in the site's time zone, or -1 for off
	const LOCK   = 'jss_resetting';

	public static function register() {
		add_action( self::HOOK, array( __CLASS__, 'run' ) );
		// a site restored from a backup, or a schedule lost: put it back
		if ( self::hour() >= 0 && ! wp_next_scheduled( self::HOOK ) ) {
			self::schedule();
		}
	}

	public static function hour(): int {
		return (int) get_option( self::HOUR, 3 );
	}

	/** The next run at the reset hour in the site's clock, then every day. */
	public static function schedule() {
		self::unschedule();
		if ( self::hour() < 0 ) {
			return;
		}
		$tz   = wp_timezone();
		$next = new \DateTimeImmutable( sprintf( 'today %02d:00', self::hour() ), $tz );
		if ( $next->getTimestamp() <= time() ) {
			$next = $next->modify( '+1 day' );
		}
		wp_schedule_event( $next->getTimestamp(), 'daily', self::HOOK );
	}

	public static function unschedule() {
		wp_clear_scheduled_hook( self::HOOK );
	}

	/** A user who may set booking pages up: the REST routes check it, as they would for a person. */
	public static function owner(): int {
		$users = get_users( array( 'capability' => 'jsb_manage', 'number' => 1, 'fields' => 'ID', 'orderby' => 'ID' ) );
		return (int) ( $users[0] ?? 0 );
	}

	/** @return array{at:int, pages:array<string,int>, errors:string[], seconds:float}|null null when a reset is already running */
	public static function run(): ?array {
		if ( get_transient( self::LOCK ) ) {
			return null;
		}
		set_transient( self::LOCK, 1, 10 * MINUTE_IN_SECONDS );
		$began = microtime( true );
		$was   = get_current_user_id();
		wp_set_current_user( self::owner() );
		add_filter( 'jsb_rate_limit', '__return_zero' ); // the reset books like a visitor, many times in a row
		try {
			self::clear();
			$built = DemoData::build();
			update_option( self::PAGES, $built['pages'], false );
			MailCatcher::clear(); // the demo's own bookings sent "emails" too; and visitors' addresses go with their bookings
			$out = array( 'at' => time(), 'pages' => $built['pages'], 'errors' => $built['errors'], 'seconds' => round( microtime( true ) - $began, 1 ) );
			update_option( self::LAST, $out, false );
			return $out;
		} finally {
			remove_filter( 'jsb_rate_limit', '__return_zero' );
			wp_set_current_user( $was );
			delete_transient( self::LOCK );
		}
	}

	/** Removes the demo pages (the ones recorded, and any left with a demo title by a reset cut off half-way) and orphaned customers. */
	private static function clear() {
		global $wpdb;
		$ids = array_map( 'intval', array_values( (array) get_option( self::PAGES, array() ) ) );
		foreach ( DemoData::TITLES as $title ) {
			$ids = array_merge( $ids, array_map( 'intval', $wpdb->get_col( $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'modules' ) . ' WHERE title = %s', $title ) ) ) );
		}
		foreach ( array_unique( $ids ) as $id ) {
			// a booking page with bookings still to come refuses deletion; in the demo they are simply ended
			$wpdb->query( $wpdb->prepare( 'UPDATE ' . Schema::table( 'bookings' ) . " SET status = 'cancelled' WHERE module_id = %d AND status IN ('pending','confirmed','in-progress','waiting-for-parts')", $id ) );
			$req = new \WP_REST_Request( 'DELETE', "/justsimple/v1/admin/modules/$id" );
			rest_do_request( $req );
		}
		$c = Schema::table( 'customers' );
		$b = Schema::table( 'bookings' );
		$wpdb->query( "DELETE FROM $c WHERE id NOT IN (SELECT DISTINCT customer_id FROM $b)" ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		foreach ( array( 'customer_addresses', 'vehicles' ) as $t ) {
			$wpdb->query( 'DELETE FROM ' . Schema::table( $t ) . " WHERE customer_id NOT IN (SELECT id FROM $c)" ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		}
		delete_option( self::PAGES );
	}
}
