<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * VIP minimum spend. Amounts are whole cents in the engine, so sums never drift; the API speaks "300.00".
 * Ported from booking.ensure_minimum_spend_accepted.
 */
final class MinimumSpend {

	/**
	 * Refuse tables whose minimum spend the customer has not agreed to.
	 *
	 * `$accepted` is the amount the page showed and the customer accepted: anything up to it is fine. Tables are decided when the
	 * request arrives, not when the slot was shown, so another booking in between can leave only a dearer room; that is refused
	 * with the new amount, never booked at an amount the customer never saw. Without `$accepted`, the older yes/no `$accept`
	 * stands for any amount (for clients that do not send the number).
	 *
	 * @throws EngineError minimum_spend_not_accepted
	 */
	public static function ensure_accepted( int $needed, bool $accept, ?int $accepted ): void {
		if ( $needed <= 0 || ( null !== $accepted ? $needed <= $accepted : $accept ) ) {
			return;
		}
		if ( null === $accepted ) {
			throw new EngineError( EngineError::MINIMUM_SPEND, 'This table has a minimum spend that must be accepted.', array( 'minimum_spend' => self::format( $needed ) ) );
		}
		throw new EngineError(
			EngineError::MINIMUM_SPEND,
			sprintf( 'The tables free now need a minimum spend of %s, more than the %s accepted; please confirm the new amount.', self::format( $needed ), self::format( $accepted ) ),
			array( 'minimum_spend' => self::format( $needed ), 'accepted_minimum_spend' => self::format( $accepted ) )
		);
	}

	/**
	 * "300", "300.5", "300.50", 300 or 300.5 -> 30050 cents; null for anything that is not a non-negative amount with at most two decimals.
	 *
	 * @param mixed $v
	 */
	public static function to_cents( $v ): ?int {
		if ( is_bool( $v ) || ( ! is_int( $v ) && ! is_float( $v ) && ! is_string( $v ) ) ) {
			return null;
		}
		$s = is_float( $v ) ? number_format( $v, 2, '.', '' ) : trim( (string) $v );
		if ( ! preg_match( '/^(\d{1,9})(?:\.(\d{1,2}))?$/', $s, $m ) ) {
			return null;
		}
		return 100 * (int) $m[1] + (int) str_pad( $m[2] ?? '0', 2, '0' );
	}

	public static function format( int $cents ): string {
		return sprintf( '%d.%02d', intdiv( $cents, 100 ), $cents % 100 );
	}
}
