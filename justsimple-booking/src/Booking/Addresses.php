<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

/**
 * The customer's address for an on-site visit. Ported from backend/app/services/locations.py, without geocoding: the plugin calls
 * no outside service, so the coordinates come with the address (a map pin, or the browser's location button). A saved address id
 * is not accepted from the public until customer sign-in exists; each on-site booking carries its address.
 */
final class Addresses {

	/**
	 * @param mixed $in the request's `address` field
	 * @return array{line1:string,city:?string,postcode:?string,latitude:float,longitude:float}
	 * @throws EngineError validation
	 */
	public static function check( $in ): array {
		if ( ! is_array( $in ) ) {
			throw new EngineError( EngineError::VALIDATION, 'An on-site service needs the address to visit.', array( 'field' => 'address' ) );
		}
		$line1 = trim( sanitize_text_field( (string) ( $in['line1'] ?? '' ) ) );
		if ( '' === $line1 || mb_strlen( $line1 ) > 255 ) {
			throw new EngineError( EngineError::VALIDATION, 'Give the street address (up to 255 characters).', array( 'field' => 'address.line1' ) );
		}
		$city     = trim( sanitize_text_field( (string) ( $in['city'] ?? '' ) ) );
		$postcode = strtoupper( preg_replace( '/\s+/', ' ', trim( sanitize_text_field( (string) ( $in['postcode'] ?? '' ) ) ) ) );
		if ( mb_strlen( $city ) > 100 || mb_strlen( $postcode ) > 16 ) {
			throw new EngineError( EngineError::VALIDATION, 'The city or postcode is too long.', array( 'field' => 'address' ) );
		}
		$lat = $in['latitude'] ?? null;
		$lon = $in['longitude'] ?? null;
		if ( ! is_numeric( $lat ) || ! is_numeric( $lon ) || $lat < -90 || $lat > 90 || $lon < -180 || $lon > 180 ) {
			throw new EngineError( EngineError::VALIDATION, 'Drop a pin on the map (or use your location) so we know where to come.', array( 'field' => 'address.latitude' ) );
		}
		return array(
			'line1'     => $line1,
			'city'      => '' === $city ? null : $city,
			'postcode'  => '' === $postcode ? null : $postcode,
			'latitude'  => (float) $lat,
			'longitude' => (float) $lon,
		);
	}

	/** Saves the address for the customer. Runs inside the booking transaction. */
	public static function save( int $customer_id, array $a ): int {
		global $wpdb;
		$wpdb->insert(
			Schema::table( 'customer_addresses' ),
			array(
				'customer_id' => $customer_id,
				'line1'       => $a['line1'],
				'city'        => $a['city'],
				'postcode'    => $a['postcode'],
				'latitude'    => number_format( $a['latitude'], 6, '.', '' ),
				'longitude'   => number_format( $a['longitude'], 6, '.', '' ),
				'created_utc' => Db::now_sql(),
			)
		);
		Db::check();
		return (int) $wpdb->insert_id;
	}

	/** @return array{line1:string,city:?string,postcode:?string,latitude:?float,longitude:?float}|null */
	public static function get( $address_id ): ?array {
		global $wpdb;
		if ( ! $address_id ) {
			return null;
		}
		$r = $wpdb->get_row( $wpdb->prepare( 'SELECT line1, city, postcode, latitude, longitude FROM ' . Schema::table( 'customer_addresses' ) . ' WHERE id = %d', (int) $address_id ), ARRAY_A ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		if ( ! $r ) {
			return null;
		}
		$r['latitude']  = null === $r['latitude'] ? null : (float) $r['latitude'];
		$r['longitude'] = null === $r['longitude'] ? null : (float) $r['longitude'];
		return $r;
	}
}
