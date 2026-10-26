<?php
namespace JustSimple\Booking\Booking;

defined( 'ABSPATH' ) || exit;

/**
 * The public booking code and the secret that lets its customer view and cancel it.
 *
 * The token is an HMAC of the code with the site's secret salt, so it can be given again when the same booking request is
 * replayed (idempotency) without being stored; the bookings table keeps only its SHA-256 for a constant-time comparison.
 */
final class ManageLink {

	/** No 0/O, 1/I/L: the code is read out over the phone. */
	const ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';

	public static function new_code(): string {
		$code = '';
		for ( $i = 0; $i < 12; $i++ ) {
			$code .= self::ALPHABET[ random_int( 0, strlen( self::ALPHABET ) - 1 ) ];
		}
		return $code;
	}

	public static function token( string $code ): string {
		return substr( hash_hmac( 'sha256', 'jsb-manage|' . $code, wp_salt( 'auth' ) ), 0, 40 );
	}

	public static function hash( string $token ): string {
		return hash( 'sha256', $token );
	}

	public static function verify( string $stored_hash, string $token ): bool {
		return '' !== $token && hash_equals( $stored_hash, self::hash( $token ) );
	}

	/** Where the customer manages the booking. Empty until a page with the booking widget exists (filter `jsb_manage_url`). */
	public static function url( string $code, string $token, int $module_id ): string {
		return (string) apply_filters( 'jsb_manage_url', '', $code, $token, $module_id );
	}
}
