<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

final class Customers {

	/**
	 * The number to book with: a mobile of the country chosen in front of it (else the booking page's country), kept as
	 * +<digits>; or a number this site already has on file, so customers saved before numbers were checked keep booking.
	 *
	 * @throws EngineError validation (field customer.phone) saying what a mobile of the country looks like
	 */
	public static function phone_for_booking( string $raw, $country, string $page_country ): string {
		$where = $page_country;
		if ( null !== $country && '' !== $country ) {
			if ( ! is_string( $country ) || ! Phone::known( $country ) ) {
				throw new EngineError( EngineError::VALIDATION, 'Choose the country of the phone number.', array( 'field' => 'customer.phone_country' ) );
			}
			$where = $country;
		}
		try {
			return Phone::mobile( $raw, $where );
		} catch ( EngineError $e ) {
			global $wpdb;
			$spelled = Phone::spelled( $raw, $where );
			$typed   = ( 0 === strpos( ltrim( $raw ), '+' ) ? '+' : '' ) . preg_replace( '/\D+/', '', $raw ); // how numbers were kept before 0.1.3
			$known   = array_values( array_unique( array_filter( array( $spelled, $typed ) ) ) );
			if ( $known ) {
				$found = $wpdb->get_var( $wpdb->prepare( 'SELECT phone FROM ' . Schema::table( 'customers' ) . ' WHERE phone IN (' . implode( ',', array_fill( 0, count( $known ), '%s' ) ) . ') ORDER BY id LIMIT 1', $known ) ); // phpcs:ignore WordPress.DB.PreparedSQL
				if ( null !== $found ) {
					return (string) $found;
				}
			}
			throw $e;
		}
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
