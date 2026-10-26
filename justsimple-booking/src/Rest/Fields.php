<?php
namespace JustSimple\Booking\Rest;

use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

/**
 * Validates request bodies against small field specs and casts table rows back to JSON types.
 *
 * Spec keys: type (str|text|int|decimal|bool|enum|date|time|email), required, nullable, min, max, values (enum).
 */
final class Fields {

	/**
	 * @param array<string,array> $spec
	 * @param bool $partial PATCH: only the fields given are checked, `required` is ignored
	 * @return array<string,mixed> column => value
	 * @throws EngineError validation, data.field naming the first bad field
	 */
	public static function clean( array $spec, array $in, bool $partial ): array {
		$out = array();
		foreach ( $spec as $field => $s ) {
			if ( ! array_key_exists( $field, $in ) ) {
				if ( ! $partial && ! empty( $s['required'] ) ) {
					self::fail( $field, 'is required' );
				}
				continue;
			}
			$v = $in[ $field ];
			if ( null === $v || '' === $v ) {
				if ( ! empty( $s['nullable'] ) ) {
					$out[ $field ] = null;
					continue;
				}
				if ( ! empty( $s['required'] ) || 'str' !== $s['type'] ) {
					self::fail( $field, 'cannot be empty' );
				}
			}
			$out[ $field ] = self::value( $field, $s, $v );
		}
		return $out;
	}

	private static function value( string $field, array $s, $v ) {
		switch ( $s['type'] ) {
			case 'str':
			case 'text':
				if ( ! is_scalar( $v ) ) {
					self::fail( $field, 'must be text' );
				}
				$v   = 'text' === $s['type'] ? sanitize_textarea_field( (string) $v ) : sanitize_text_field( (string) $v );
				$max = $s['max'] ?? ( 'text' === $s['type'] ? 2000 : 150 );
				if ( mb_strlen( $v ) > $max ) {
					self::fail( $field, "must be at most $max characters" );
				}
				return $v;
			case 'int':
				if ( ! is_numeric( $v ) || (int) $v != $v ) { // phpcs:ignore Universal.Operators.StrictComparisons -- "15" and 15 are both fine, 15.5 is not
					self::fail( $field, 'must be a whole number' );
				}
				$v = (int) $v;
				if ( ( isset( $s['min'] ) && $v < $s['min'] ) || ( isset( $s['max'] ) && $v > $s['max'] ) ) {
					self::fail( $field, sprintf( 'must be between %d and %d', $s['min'] ?? PHP_INT_MIN, $s['max'] ?? PHP_INT_MAX ) );
				}
				return $v;
			case 'decimal':
				if ( ! is_numeric( $v ) || $v < ( $s['min'] ?? 0 ) || $v > ( $s['max'] ?? 9999999 ) ) {
					self::fail( $field, 'must be an amount of 0 or more' );
				}
				return number_format( (float) $v, 2, '.', '' );
			case 'bool':
				if ( ! in_array( $v, array( true, false, 0, 1, '0', '1', 'true', 'false' ), true ) ) {
					self::fail( $field, 'must be true or false' );
				}
				return in_array( $v, array( true, 1, '1', 'true' ), true ) ? 1 : 0;
			case 'enum':
				if ( ! in_array( $v, $s['values'], true ) ) {
					self::fail( $field, 'must be one of: ' . implode( ', ', $s['values'] ) );
				}
				return $v;
			case 'date':
				if ( ! is_string( $v ) || ! preg_match( '/^(\d{4})-(\d{2})-(\d{2})$/', $v, $m ) || ! checkdate( (int) $m[2], (int) $m[3], (int) $m[1] ) ) {
					self::fail( $field, 'must be a date as YYYY-MM-DD' );
				}
				return $v;
			case 'time':
				if ( ! is_string( $v ) || ! preg_match( '/^([01]\d|2[0-3]):[0-5]\d$/', $v ) ) {
					self::fail( $field, 'must be a time as HH:MM (24-hour)' );
				}
				return $v . ':00';
			case 'email':
				if ( ! is_string( $v ) || ! is_email( $v ) ) {
					self::fail( $field, 'must be an email address' );
				}
				return strtolower( $v );
		}
		self::fail( $field, 'has an unknown type' );
	}

	/** Cast a table row to the spec's JSON types (MySQL returns strings). */
	public static function row( array $spec, array $row ): array {
		$out = array( 'id' => (int) $row['id'] );
		foreach ( $spec as $field => $s ) {
			if ( ! array_key_exists( $field, $row ) ) {
				continue;
			}
			$v = $row[ $field ];
			if ( null === $v ) {
				$out[ $field ] = null;
			} elseif ( 'int' === $s['type'] ) {
				$out[ $field ] = (int) $v;
			} elseif ( 'bool' === $s['type'] ) {
				$out[ $field ] = (bool) (int) $v;
			} elseif ( 'decimal' === $s['type'] ) {
				$out[ $field ] = $v; // kept as a string: "45.00" must not become 45.000000001
			} elseif ( 'time' === $s['type'] ) {
				$out[ $field ] = substr( $v, 0, 5 );
			} else {
				$out[ $field ] = $v;
			}
		}
		return $out;
	}

	public static function fail( string $field, string $why ): void {
		throw new EngineError( EngineError::VALIDATION, "$field $why.", array( 'field' => $field ) );
	}
}
