<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * Distances on the ground for the service radius, in plain PHP (the engine measured them with PostGIS `geography`).
 *
 * Vincenty's inverse formula on the WGS-84 ellipsoid, the model PostGIS geography uses, so a radius of 25 km means 25 km on the
 * ground. For nearly antipodal points Vincenty can fail to converge; there the spherical (haversine) distance is used instead,
 * which no service radius comes near anyway.
 */
final class Geo {

	const A = 6378137.0;           // WGS-84 semi-major axis, metres
	const F = 1 / 298.257223563;   // WGS-84 flattening
	const MEAN_RADIUS_KM = 6371.0088;

	public static function distance_km( float $lat1, float $lon1, float $lat2, float $lon2 ): float {
		$b  = self::A * ( 1 - self::F );
		$l  = deg2rad( $lon2 - $lon1 );
		$u1 = atan( ( 1 - self::F ) * tan( deg2rad( $lat1 ) ) );
		$u2 = atan( ( 1 - self::F ) * tan( deg2rad( $lat2 ) ) );
		$sin_u1 = sin( $u1 );
		$cos_u1 = cos( $u1 );
		$sin_u2 = sin( $u2 );
		$cos_u2 = cos( $u2 );

		$lambda = $l;
		for ( $i = 0; $i < 200; $i++ ) {
			$sin_l     = sin( $lambda );
			$cos_l     = cos( $lambda );
			$sin_sigma = sqrt( ( $cos_u2 * $sin_l ) ** 2 + ( $cos_u1 * $sin_u2 - $sin_u1 * $cos_u2 * $cos_l ) ** 2 );
			if ( 0.0 === $sin_sigma ) {
				return 0.0; // the same point
			}
			$cos_sigma  = $sin_u1 * $sin_u2 + $cos_u1 * $cos_u2 * $cos_l;
			$sigma      = atan2( $sin_sigma, $cos_sigma );
			$sin_alpha  = $cos_u1 * $cos_u2 * $sin_l / $sin_sigma;
			$cos2_alpha = 1 - $sin_alpha * $sin_alpha;
			$cos_2sm    = 0.0 !== $cos2_alpha ? $cos_sigma - 2 * $sin_u1 * $sin_u2 / $cos2_alpha : 0.0; // on the equator
			$c          = self::F / 16 * $cos2_alpha * ( 4 + self::F * ( 4 - 3 * $cos2_alpha ) );
			$prev       = $lambda;
			$lambda     = $l + ( 1 - $c ) * self::F * $sin_alpha * ( $sigma + $c * $sin_sigma * ( $cos_2sm + $c * $cos_sigma * ( -1 + 2 * $cos_2sm * $cos_2sm ) ) );
			if ( abs( $lambda - $prev ) < 1e-12 ) {
				$u2b = $cos2_alpha * ( self::A * self::A - $b * $b ) / ( $b * $b );
				$aa  = 1 + $u2b / 16384 * ( 4096 + $u2b * ( -768 + $u2b * ( 320 - 175 * $u2b ) ) );
				$bb  = $u2b / 1024 * ( 256 + $u2b * ( -128 + $u2b * ( 74 - 47 * $u2b ) ) );
				$ds  = $bb * $sin_sigma * ( $cos_2sm + $bb / 4 * ( $cos_sigma * ( -1 + 2 * $cos_2sm * $cos_2sm ) - $bb / 6 * $cos_2sm * ( -3 + 4 * $sin_sigma * $sin_sigma ) * ( -3 + 4 * $cos_2sm * $cos_2sm ) ) );
				return $b * $aa * ( $sigma - $ds ) / 1000;
			}
		}
		return self::haversine_km( $lat1, $lon1, $lat2, $lon2 );
	}

	public static function haversine_km( float $lat1, float $lon1, float $lat2, float $lon2 ): float {
		$dlat = deg2rad( $lat2 - $lat1 );
		$dlon = deg2rad( $lon2 - $lon1 );
		$h    = sin( $dlat / 2 ) ** 2 + cos( deg2rad( $lat1 ) ) * cos( deg2rad( $lat2 ) ) * sin( $dlon / 2 ) ** 2;
		return 2 * self::MEAN_RADIUS_KM * asin( min( 1.0, sqrt( $h ) ) );
	}

	/**
	 * Refuses a point outside the business's service radius; returns the distance in km.
	 *
	 * @param array{latitude:?float,longitude:?float,service_radius_km:?float}|null $business
	 * @throws EngineError validation (no location set) | outside_service_area
	 */
	public static function ensure_within( ?array $business, float $lat, float $lon ): float {
		if ( ! $business || null === $business['latitude'] || null === $business['longitude'] || empty( $business['service_radius_km'] ) ) {
			throw new EngineError( EngineError::VALIDATION, 'This business has not set its location and service radius for on-site visits.' );
		}
		$km = self::distance_km( (float) $business['latitude'], (float) $business['longitude'], $lat, $lon );
		if ( $km > (float) $business['service_radius_km'] ) {
			throw new EngineError(
				EngineError::OUTSIDE_AREA,
				sprintf( 'That address is %.1f km away; this business visits up to %s km.', $km, (float) $business['service_radius_km'] ),
				array( 'distance_km' => round( $km, 2 ), 'service_radius_km' => (float) $business['service_radius_km'] )
			);
		}
		return $km;
	}
}
