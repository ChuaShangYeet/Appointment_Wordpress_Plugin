<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

final class Customers {

	/** Digits with an optional leading +; 7 to 15 digits (E.164 allows 15). */
	public static function normalise_phone( string $raw ): string {
		$plus   = 0 === strpos( ltrim( $raw ), '+' );
		$digits = preg_replace( '/\D+/', '', $raw );
		if ( strlen( $digits ) < 7 || strlen( $digits ) > 15 ) {
			throw new EngineError( EngineError::VALIDATION, 'Enter a phone number with 7 to 15 digits.', array( 'field' => 'customer.phone' ) );
		}
		return ( $plus ? '+' : '' ) . $digits;
	}

	/**
	 * The customer behind a phone number, created on first sight.
	 *
	 * A public booking proves nothing about who holds the phone, so it may only add to an existing record: the name follows the
	 * latest booking (people fix typos), but an email already on the record is never replaced. Otherwise anyone could attach their
	 * own email to another person's record. The email typed with a booking is kept on that booking (contact_email) instead.
	 */
	public static function upsert( string $name, string $phone, ?string $email ): int {
		global $wpdb;
		$t   = Schema::table( 'customers' );
		$now = Db::now_sql();
		// prepare() turns null into '' rather than NULL, and COALESCE must see a real NULL.
		// It goes through prepare() a second time below, so any % in it is doubled.
		$email_sql = null === $email ? 'NULL' : str_replace( '%', '%%', $wpdb->prepare( '%s', $email ) );
		// LAST_INSERT_ID(id) makes insert_id the existing row's id when the phone is already known.
		$wpdb->query(
			$wpdb->prepare(
				"INSERT INTO $t (name, phone, email, created_utc, updated_utc) VALUES (%s, %s, $email_sql, %s, %s)
				 ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id), name = VALUES(name), email = COALESCE(email, VALUES(email)), updated_utc = VALUES(updated_utc)", // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
				$name,
				$phone,
				$now,
				$now
			)
		);
		Db::check();
		return (int) $wpdb->insert_id;
	}
}
