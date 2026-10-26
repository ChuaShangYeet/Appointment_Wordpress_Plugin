<?php
namespace JustSimple\Booking\Data;

use JustSimple\Booking\Activator;

defined( 'ABSPATH' ) || exit;

/**
 * Checks the things a free host can quietly get wrong and that bookings depend on.
 * Each check returns array( 'label', 'ok' => true|false|null (null = warning), 'detail' ).
 */
final class Health {

	/** @return array<int,array{label:string,ok:?bool,detail:string}> */
	public static function checks() {
		return array(
			self::tables(),
			self::innodb(),
			self::named_locks(),
			self::timezone(),
			self::cron(),
		);
	}

	private static function tables() {
		global $wpdb;
		$missing = array();
		foreach ( Schema::TABLES as $name ) {
			$table = Schema::table( $name );
			if ( $wpdb->get_var( $wpdb->prepare( 'SHOW TABLES LIKE %s', $wpdb->esc_like( $table ) ) ) !== $table ) {
				$missing[] = $name;
			}
		}
		return array(
			'label'  => __( 'Database tables', 'justsimple-booking' ),
			'ok'     => ! $missing,
			'detail' => $missing
				/* translators: %s: comma-separated table names */
				? sprintf( __( 'Missing: %s. Deactivate and activate the plugin again.', 'justsimple-booking' ), implode( ', ', $missing ) )
				/* translators: 1: table count, 2: schema version */
				: sprintf( __( 'All %1$d tables present (schema version %2$d).', 'justsimple-booking' ), count( Schema::TABLES ), (int) get_option( Schema::OPTION ) ),
		);
	}

	/** Bookings are saved in a transaction; MyISAM ignores transactions, so a failed save could leave half a booking. */
	private static function innodb() {
		global $wpdb;
		$engine = $wpdb->get_var(
			$wpdb->prepare(
				'SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s',
				Schema::table( 'bookings' )
			)
		);
		return array(
			'label'  => __( 'Transactions (InnoDB)', 'justsimple-booking' ),
			'ok'     => is_string( $engine ) && strcasecmp( $engine, 'InnoDB' ) === 0,
			'detail' => $engine
				/* translators: %s: MySQL storage engine name */
				? sprintf( __( 'Bookings table uses %s.', 'justsimple-booking' ), $engine )
				: __( 'Could not read the storage engine.', 'justsimple-booking' ),
		);
	}

	/** GET_LOCK is how two visitors booking the same slot at the same moment are kept apart. */
	private static function named_locks() {
		global $wpdb;
		$name = 'jsb:health:' . wp_generate_password( 8, false );
		$got  = $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, 1)', $name ) );
		$wpdb->query( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $name ) );
		return array(
			'label'  => __( 'Booking locks (GET_LOCK)', 'justsimple-booking' ),
			'ok'     => '1' === (string) $got,
			'detail' => '1' === (string) $got
				? __( 'The database grants named locks.', 'justsimple-booking' )
				: __( 'The database refused a named lock. Two people could book the same slot at the same moment.', 'justsimple-booking' ),
		);
	}

	/** A "UTC+8" offset has no daylight-saving rules; a city name does. Only a warning: offsets are right in most of Asia. */
	private static function timezone() {
		$tz   = wp_timezone_string();
		$city = (bool) preg_match( '#^[A-Za-z]+/#', $tz );
		return array(
			'label'  => __( 'Time zone', 'justsimple-booking' ),
			'ok'     => $city ? true : null,
			'detail' => $city
				/* translators: %s: time zone name */
				? sprintf( __( 'Bookings use %s.', 'justsimple-booking' ), $tz )
				/* translators: %s: UTC offset */
				: sprintf( __( 'Set to the offset %s. Pick your city in Settings > General so daylight-saving changes are handled.', 'justsimple-booking' ), $tz ),
		);
	}

	/** Expired pending holds are also swept on every availability read, so a lazy WP-Cron is a warning, not a failure. */
	private static function cron() {
		$disabled = defined( 'DISABLE_WP_CRON' ) && DISABLE_WP_CRON;
		$next     = wp_next_scheduled( Activator::CRON_SWEEP );
		if ( ! $next ) {
			$detail = __( 'The hourly sweep is not scheduled. Deactivate and activate the plugin again.', 'justsimple-booking' );
		} elseif ( $disabled ) {
			$detail = __( 'WP-Cron is turned off on this site. Reminders go out only if your host runs wp-cron.php on a timer.', 'justsimple-booking' );
		} else {
			/* translators: %s: human time difference */
			$detail = sprintf( __( 'Next sweep in %s. On quiet sites WP-Cron runs only when someone visits.', 'justsimple-booking' ), human_time_diff( time(), $next ) );
		}
		return array(
			'label'  => __( 'Scheduled sweep (WP-Cron)', 'justsimple-booking' ),
			'ok'     => ! $next ? false : ( $disabled ? null : true ),
			'detail' => $detail,
		);
	}
}
