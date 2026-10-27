<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/**
 * The car on a workshop booking. Ported from backend/app/services/automotive.py.
 *
 * The vehicle is given as typed with each booking (plate, supported model, year): there is no customer sign-in yet, so a saved
 * vehicle id is not accepted from the public. The same customer and plate still land on one vehicle record.
 */
final class Vehicles {

	/** Upper-case, letters and digits only: "abc 123" and "ABC-123" are the same car. */
	public static function normalise_plate( string $plate ): string {
		return strtoupper( preg_replace( '/[^A-Za-z0-9]/', '', $plate ) );
	}

	/**
	 * Checks a typed vehicle against the workshop's supported models, before anything is written.
	 *
	 * @param mixed $in the request's `vehicle` field
	 * @return array{plate_no:string,supported:array,year:?int}
	 * @throws EngineError validation | incompatible_vehicle
	 */
	public static function check( int $module_id, $in ): array {
		if ( ! is_array( $in ) ) {
			throw new EngineError( EngineError::VALIDATION, 'Give the vehicle: plate_no, supported_vehicle_id and year.', array( 'field' => 'vehicle' ) );
		}
		$plate = self::normalise_plate( (string) ( $in['plate_no'] ?? '' ) );
		if ( '' === $plate || strlen( $plate ) > 20 ) {
			throw new EngineError( EngineError::VALIDATION, 'Give the number plate (up to 20 letters and digits).', array( 'field' => 'vehicle.plate_no' ) );
		}
		$year = $in['year'] ?? null;
		if ( null !== $year && '' !== $year ) {
			if ( ! is_numeric( $year ) || (int) $year != $year || $year < 1900 || $year > 2100 ) { // phpcs:ignore Universal.Operators.StrictComparisons -- "2018" and 2018 are both fine
				throw new EngineError( EngineError::VALIDATION, 'Give the model year as four digits.', array( 'field' => 'vehicle.year' ) );
			}
			$year = (int) $year;
		} else {
			$year = null;
		}
		global $wpdb;
		$sv = $wpdb->get_row(
			$wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'supported_vehicles' ) . ' WHERE id = %d AND module_id = %d', (int) ( $in['supported_vehicle_id'] ?? 0 ), $module_id ),
			ARRAY_A
		);
		if ( ! $sv ) {
			throw new EngineError( EngineError::INCOMPATIBLE_VEHICLE, 'This workshop does not service that vehicle.' );
		}
		if ( ! self::year_allowed( $sv, $year ) ) {
			throw new EngineError(
				EngineError::INCOMPATIBLE_VEHICLE,
				sprintf( 'This workshop services %s %s only for model years %s-%s.', $sv['make'], $sv['model'], $sv['year_from'] ?: '…', $sv['year_to'] ?: '…' )
			);
		}
		return array( 'plate_no' => $plate, 'supported' => $sv, 'year' => $year );
	}

	/** A model may be limited to a year range; then the vehicle's year must be known and inside it. */
	public static function year_allowed( array $sv, ?int $year ): bool {
		if ( null === $sv['year_from'] && null === $sv['year_to'] ) {
			return true;
		}
		if ( null === $year ) {
			return false;
		}
		return ( null === $sv['year_from'] || $year >= (int) $sv['year_from'] ) && ( null === $sv['year_to'] || $year <= (int) $sv['year_to'] );
	}

	/** The customer's vehicle record for this plate, created or brought up to date. Runs inside the booking transaction. */
	public static function upsert( int $customer_id, array $checked ): int {
		global $wpdb;
		$sv = $checked['supported'];
		$wpdb->query(
			$wpdb->prepare(
				'INSERT INTO ' . Schema::table( 'vehicles' ) . ' (customer_id, supported_vehicle_id, plate_no, make, model, year, created_utc)
				 VALUES (%d, %d, %s, %s, %s, ' . ( null === $checked['year'] ? 'NULL' : (int) $checked['year'] ) . ', %s)
				 ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id), supported_vehicle_id = VALUES(supported_vehicle_id), make = VALUES(make), model = VALUES(model), year = VALUES(year)',
				$customer_id,
				(int) $sv['id'],
				$checked['plate_no'],
				$sv['make'],
				$sv['model'],
				Db::now_sql()
			)
		);
		Db::check();
		return (int) $wpdb->insert_id;
	}
}
