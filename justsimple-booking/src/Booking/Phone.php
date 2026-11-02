<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

/**
 * A customer's mobile number: the country chosen in front of it, and the check it must pass there.
 *
 * The business calls or messages the customer about a booking on this number (the plugin itself sends email only, so it must
 * be a number that reaches the person, not the front desk): a number new to the site must be a mobile of the chosen country,
 * of the right length (Malaysia: 9 or 10 digits after +60), and it is kept one way: +<digits> (E.164), so 012-345 6789 and
 * +60 12-345 6789 are one customer. A number typed with + or 00 keeps its own country whatever was chosen.
 *
 * The rules are Google's numbering plans (libphonenumber), written out by tools/make-phone-plans.py into phone-plans.php;
 * this class follows libphonenumber's own steps (international prefix, country code, trunk prefix, which country of a shared
 * code, which kind of number), and tests/phone.php checks it gives the backend's answer for every country's examples. (Only the
 * landline message is worded differently: the backend sends codes by WhatsApp or SMS, the plugin does not.)
 */
final class Phone {

	const LANDLINE = 'That looks like a landline: enter a mobile number, so the business can call or message you about your booking';
	const INVALID  = 'That is not a valid phone number';

	/** @var array|null */
	private static $plans;

	private static function plans(): array {
		if ( null === self::$plans ) {
			self::$plans = require __DIR__ . '/phone-plans.php';
		}
		return self::$plans;
	}

	/** Is this a country the check knows (two capital letters)? */
	public static function known( string $region ): bool {
		return isset( self::plans()['regions'][ $region ] );
	}

	/**
	 * The country a site's numbers are read in when nothing else says: the country of WordPress's time zone (Asia/Kuala_Lumpur
	 * is MY), else Malaysia. A UTC offset names no country.
	 */
	public static function site_country(): string {
		$zone = function_exists( 'wp_timezone_string' ) ? wp_timezone_string() : 'UTC';
		try {
			$where = ( new \DateTimeZone( $zone ) )->getLocation();
			$code  = is_array( $where ) ? (string) ( $where['country_code'] ?? '' ) : '';
		} catch ( \Exception $e ) {
			$code = '';
		}
		return self::known( $code ) ? $code : 'MY';
	}

	/**
	 * $raw as typed -> +<digits> when it is a mobile number; $region (MY) reads a number typed without its country code.
	 *
	 * @throws EngineError validation (field customer.phone) saying what a mobile of the country looks like
	 */
	public static function mobile( string $raw, string $region ): string {
		$text = preg_replace( '/[\s\-().]/u', '', trim( $raw ) );
		if ( 0 === strpos( $text, '00' ) ) {
			$text = '+' . substr( $text, 2 );
		}
		if ( ! preg_match( '/^\+?[0-9]+$/D', $text ) ) {
			self::fail( self::INVALID );
		}
		$number = self::parse( $text, $region );
		if ( null === $number ) {
			self::fail( self::INVALID );
		}
		list( $cc, $nsn ) = $number;
		$type             = self::type_of( $cc, $nsn );
		if ( null === $type ) {
			$main  = self::plans()['calling'][ $cc ][0];
			$where = self::known( $main ) ? $main : $region; // the country the number was typed for: +44 is the UK whatever was chosen
			self::fail( self::not_mobile_message( $where ) );
		}
		if ( 'mobile' !== $type ) {
			self::fail( self::LANDLINE );
		}
		return '+' . $cc . $nsn;
	}

	/** The number spelled the standard way without the strict check, for finding a customer already on file; null if it is no number at all. */
	public static function spelled( string $raw, string $region ): ?string {
		try {
			return self::mobile( $raw, $region );
		} catch ( EngineError $e ) {
			$text = preg_replace( '/[\s\-().]/u', '', trim( $raw ) );
			if ( 0 === strpos( $text, '00' ) ) {
				$text = '+' . substr( $text, 2 );
			}
			if ( ! preg_match( '/^\+?[0-9]+$/D', $text ) ) {
				return null;
			}
			$number = self::parse( $text, $region );
			return null === $number ? null : '+' . $number[0] . $number[1];
		}
	}

	/** "That is not a mobile number in Malaysia: it has 9 or 10 digits after +60, such as 012-345 6789" */
	public static function not_mobile_message( string $region ): string {
		$p     = self::plans()['regions'][ $region ];
		$count = implode( ' or ', $p['l'] );
		$such  = isset( $p['e'] ) ? ', such as ' . $p['e'] : '';
		return "That is not a mobile number in {$p['n']}: it has $count digits after +{$p['c']}$such";
	}

	/** The booking form's country list item for a region: name, calling code, mobile lengths, an example, the trunk prefix. */
	public static function country( string $region ): array {
		$p = self::plans()['regions'][ $region ];
		return array( 'code' => $region, 'name' => $p['n'], 'calling' => $p['c'], 'lengths' => $p['l'], 'example' => $p['e'] ?? null );
	}

	private static function fail( string $message ) {
		throw new EngineError( EngineError::VALIDATION, $message, array( 'field' => 'customer.phone' ) );
	}

	// ------------------------------------------------------------------ libphonenumber's steps

	/** @return array{0:string,1:string}|null [calling code, national significant number], as libphonenumber's parse() */
	private static function parse( string $text, string $region ): ?array {
		$plans = self::plans();
		$meta  = $plans['regions'][ $region ] ?? null;
		if ( null === $meta ) {
			return null;
		}
		$digits = ltrim( $text, '+' );
		$intl   = '+' === substr( $text, 0, 1 );
		if ( ! $intl && isset( $meta['ip'] ) && preg_match( '/^(?:' . $meta['ip'] . ')/', $digits, $m ) ) {
			$rest = substr( $digits, strlen( $m[0] ) );
			if ( '' !== $rest && '0' !== $rest[0] ) { // country codes never begin with 0
				$digits = $rest;
				$intl   = true;
			}
		}
		if ( $intl ) {
			if ( strlen( $digits ) <= 2 || '0' === $digits[0] ) {
				return null;
			}
			$cc = null;
			for ( $i = 1; $i <= 3 && $i <= strlen( $digits ); $i++ ) {
				if ( isset( $plans['calling'][ substr( $digits, 0, $i ) ] ) ) {
					$cc = substr( $digits, 0, $i );
					break;
				}
			}
			if ( null === $cc ) {
				return null;
			}
			$nsn  = substr( $digits, strlen( $cc ) );
			$meta = self::meta( $cc, $plans['calling'][ $cc ][0] );
		} else {
			$cc  = $meta['c'];
			$nsn = $digits;
			// typed with the country code but no + (60123456789): taken off when what is left reads better
			if ( 0 === strpos( $digits, $cc ) ) {
				$potential = self::strip_trunk( substr( $digits, strlen( $cc ) ), $meta );
				if ( ( ! self::full( $digits, $meta['g'][0] ) && self::full( $potential, $meta['g'][0] ) ) || 'too_long' === self::length_check( $digits, $meta ) ) {
					$nsn = $potential;
				}
			}
		}
		if ( strlen( $nsn ) < 2 ) {
			return null;
		}
		$stripped = self::strip_trunk( $nsn, $meta );
		if ( ! in_array( self::length_check( $stripped, $meta ), array( 'too_short', 'local_only', 'invalid' ), true ) ) {
			$nsn = $stripped;
		}
		return strlen( $nsn ) > 17 ? null : array( $cc, $nsn );
	}

	private static function meta( string $cc, string $region ): array {
		$plans = self::plans();
		return '001' === $region ? $plans['nongeo'][ $cc ] : $plans['regions'][ $region ];
	}

	/** The national number without its trunk prefix (Malaysia's 0), as libphonenumber's maybeStripNationalPrefixAndCarrierCode. */
	private static function strip_trunk( string $nsn, array $meta ): string {
		if ( '' === $nsn || ! isset( $meta['pp'] ) || '' === $meta['pp'] ) { // (not empty(): Malaysia's prefix is "0")
			return $nsn;
		}
		$pattern = '/^(?:' . $meta['pp'] . ')/';
		if ( ! preg_match( $pattern, $nsn, $m, PREG_UNMATCHED_AS_NULL ) ) {
			return $nsn;
		}
		$viable = self::full( $nsn, $meta['g'][0] );
		$groups = count( $m ) - 1;
		if ( empty( $meta['pt'] ) || 0 === $groups || null === $m[ $groups ] ) {
			$rest = substr( $nsn, strlen( $m[0] ) );
			return $viable && ! self::full( $rest, $meta['g'][0] ) ? $nsn : $rest;
		}
		$transformed = preg_replace( $pattern, $meta['pt'], $nsn, 1 );
		return $viable && ! self::full( $transformed, $meta['g'][0] ) ? $nsn : $transformed;
	}

	/** libphonenumber's testNumberLength against the country's general lengths. */
	private static function length_check( string $nsn, array $meta ): string {
		list( , $lengths, $local ) = $meta['g'];
		$n = strlen( $nsn );
		if ( in_array( $n, $local, true ) ) {
			return 'local_only';
		}
		if ( ! $lengths ) {
			return 'possible';
		}
		if ( $lengths[0] === $n ) {
			return 'possible';
		}
		if ( $lengths[0] > $n ) {
			return 'too_short';
		}
		if ( $lengths[ count( $lengths ) - 1 ] < $n ) {
			return 'too_long';
		}
		return in_array( $n, array_slice( $lengths, 1 ), true ) ? 'possible' : 'invalid';
	}

	/** 'mobile' (a mobile, or a number that may be either), 'other' (a landline, freephone, ...), or null (no number of that country). */
	private static function type_of( string $cc, string $nsn ): ?string {
		$regions = self::plans()['calling'][ $cc ];
		$region  = null;
		if ( 1 === count( $regions ) ) {
			$region = $regions[0];
		} else {
			foreach ( $regions as $r ) {
				$meta = self::meta( $cc, $r );
				if ( isset( $meta['ld'] ) ) {
					if ( preg_match( '/^(?:' . $meta['ld'] . ')/', $nsn ) ) {
						$region = $r;
						break;
					}
				} elseif ( null !== self::kind( $nsn, $meta ) ) {
					$region = $r;
					break;
				}
			}
		}
		return null === $region ? null : self::kind( $nsn, self::meta( $cc, $region ) );
	}

	/** libphonenumber's getNumberTypeHelper, folded to what the check needs. */
	private static function kind( string $nsn, array $meta ): ?string {
		if ( ! self::matches( $nsn, array( $meta['g'][0], $meta['g'][1] ) ) ) {
			return null;
		}
		foreach ( $meta['o'] as $d ) {
			if ( self::matches( $nsn, $d ) ) {
				return 'other';
			}
		}
		if ( $meta['f'] && self::matches( $nsn, $meta['f'] ) ) {
			return ! empty( $meta['s'] ) || ( $meta['m'] && self::matches( $nsn, $meta['m'] ) ) ? 'mobile' : 'other';
		}
		return empty( $meta['s'] ) && $meta['m'] && self::matches( $nsn, $meta['m'] ) ? 'mobile' : null;
	}

	/** @param array{0:string,1:int[]} $desc pattern and possible lengths (none listed: any length) */
	private static function matches( string $nsn, ?array $desc ): bool {
		if ( null === $desc ) {
			return false;
		}
		if ( $desc[1] && ! in_array( strlen( $nsn ), $desc[1], true ) ) {
			return false;
		}
		return self::full( $nsn, $desc[0] );
	}

	private static function full( string $nsn, string $pattern ): bool {
		return 1 === preg_match( '/^(?:' . $pattern . ')$/D', $nsn );
	}
}
