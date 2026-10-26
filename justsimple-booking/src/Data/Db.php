<?php
namespace JustSimple\Booking\Data;

defined( 'ABSPATH' ) || exit;

/** Small helpers shared by everything that talks to the jsb_ tables. */
final class Db {

	/** UTC seconds -> the DATETIME string stored in *_utc columns. */
	public static function to_sql( int $ts ): string {
		return gmdate( 'Y-m-d H:i:s', $ts );
	}

	/** A *_utc DATETIME string -> UTC seconds. */
	public static function from_sql( string $s ): int {
		return ( new \DateTimeImmutable( $s, new \DateTimeZone( 'UTC' ) ) )->getTimestamp();
	}

	public static function now_sql(): string {
		return self::to_sql( time() );
	}

	/** '%d,%d,%d' for an IN (...) list. Callers must pass a non-empty array. */
	public static function ints( array $ids ): string {
		return implode( ',', array_fill( 0, count( $ids ), '%d' ) );
	}

	/** Runs `$fn` in a transaction; rolls back and rethrows on any exception. */
	public static function transaction( callable $fn ) {
		global $wpdb;
		$wpdb->query( 'START TRANSACTION' );
		try {
			$result = $fn();
			$wpdb->query( 'COMMIT' );
			return $result;
		} catch ( \Throwable $e ) {
			$wpdb->query( 'ROLLBACK' );
			throw $e;
		}
	}

	/** Throws when the last $wpdb query failed, so a failed insert never passes silently inside a transaction. */
	public static function check(): void {
		global $wpdb;
		if ( $wpdb->last_error ) {
			throw new \RuntimeException( 'Database error: ' . $wpdb->last_error );
		}
	}
}
