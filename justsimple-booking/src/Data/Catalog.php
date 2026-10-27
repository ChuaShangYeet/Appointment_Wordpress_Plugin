<?php
namespace JustSimple\Booking\Data;

use JustSimple\Booking\Engine\EngineError;
use JustSimple\Booking\Engine\Policy;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/** Reads of modules, services and policies that both the booking service and the REST layer need. */
final class Catalog {

	/** Module settings and their defaults; stored as JSON in jsb_modules.settings. */
	const SETTINGS_DEFAULTS = array(
		'slot_step_minutes' => 15,
		'notify_email'      => '',
		// field: where the business is and how far it travels for on-site visits
		'latitude'          => null,
		'longitude'         => null,
		'service_radius_km' => null,
	);

	/** @return array<string,mixed>|null with `settings` decoded */
	public static function module( int $id ): ?array {
		global $wpdb;
		$row = $wpdb->get_row( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'modules' ) . ' WHERE id = %d', $id ), ARRAY_A );
		if ( ! $row ) {
			return null;
		}
		$row['id']       = (int) $row['id'];
		$settings        = json_decode( (string) $row['settings'], true );
		$row['settings'] = array_merge( self::SETTINGS_DEFAULTS, is_array( $settings ) ? $settings : array() );
		return $row;
	}

	/** @throws EngineError not_found when the module is missing or not taking bookings */
	public static function bookable_module( int $id ): array {
		$m = self::module( $id );
		if ( ! $m || 'active' !== $m['status'] ) {
			throw new EngineError( EngineError::NOT_FOUND, 'This booking page is not available.' );
		}
		return $m;
	}

	/**
	 * Active, bookable services of the module in the order asked for (duplicates dropped). Add-ons count only where
	 * `$allow_addons` (salon modules); the cart rule itself (one base service) is the allocator's.
	 *
	 * @param int[] $ids
	 * @throws EngineError validation | not_found
	 */
	public static function services_for_booking( int $module_id, array $ids, bool $allow_addons = false ): array {
		global $wpdb;
		$ids = array_values( array_unique( array_map( 'intval', $ids ) ) );
		if ( ! $ids ) {
			throw new EngineError( EngineError::VALIDATION, 'Choose at least one service.' );
		}
		$rows  = $wpdb->get_results(
			$wpdb->prepare(
				'SELECT id, name, duration_minutes, buffer_minutes, price, price_after_inspection, policy_id, is_addon, location_type, travel_buffer_minutes, service_category FROM ' . Schema::table( 'services' ) .
				' WHERE module_id = %d AND active = 1' . ( $allow_addons ? '' : ' AND is_addon = 0' ) . ' AND id IN (' . Db::ints( $ids ) . ')',
				array_merge( array( $module_id ), $ids )
			),
			ARRAY_A
		);
		$by_id = array();
		foreach ( $rows as $r ) {
			$r['id']               = (int) $r['id'];
			$r['duration_minutes'] = (int) $r['duration_minutes'];
			$r['buffer_minutes']   = (int) $r['buffer_minutes'];
			$r['is_addon']         = (bool) (int) $r['is_addon'];
			$r['travel_buffer_minutes'] = (int) $r['travel_buffer_minutes'];
			$r['policy_id']        = null === $r['policy_id'] ? null : (int) $r['policy_id'];
			$by_id[ $r['id'] ]     = $r;
		}
		$missing = array_values( array_diff( $ids, array_keys( $by_id ) ) );
		if ( $missing ) {
			throw new EngineError( EngineError::NOT_FOUND, 'Unknown or unavailable service.', array( 'service_ids' => $missing ) );
		}
		return array_map( static function ( $id ) use ( $by_id ) {
			return $by_id[ $id ];
		}, $ids );
	}

	/**
	 * A restaurant's policy: the seating-time setting's own policy, else the module default.
	 *
	 * @throws EngineError validation when none is configured
	 */
	public static function resolve_restaurant_policy( array $module, ?int $setting_policy_id ): Policy {
		global $wpdb;
		$id  = $setting_policy_id ?? ( $module['default_policy_id'] ? (int) $module['default_policy_id'] : null );
		$row = null === $id ? null : $wpdb->get_row( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'policies' ) . ' WHERE id = %d AND module_id = %d', $id, (int) $module['id'] ), ARRAY_A );
		if ( ! $row ) {
			throw new EngineError( EngineError::VALIDATION, 'This booking page has no booking policy yet.' );
		}
		return Policy::from_row( $row );
	}

	/**
	 * Each service's own policy, else the module default; several distinct policies merge into the strictest.
	 *
	 * @throws EngineError validation when no policy is configured
	 */
	public static function resolve_policy( array $module, array $services ): Policy {
		global $wpdb;
		$ids = array();
		foreach ( $services as $s ) {
			$ids[] = $s['policy_id'] ?? ( $module['default_policy_id'] ? (int) $module['default_policy_id'] : null );
		}
		if ( ! $ids ) {
			$ids[] = $module['default_policy_id'] ? (int) $module['default_policy_id'] : null;
		}
		if ( in_array( null, $ids, true ) ) {
			throw new EngineError( EngineError::VALIDATION, 'This booking page has no booking policy yet.' );
		}
		$ids      = array_values( array_unique( $ids ) );
		$rows     = $wpdb->get_results(
			$wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'policies' ) . ' WHERE module_id = %d AND id IN (' . Db::ints( $ids ) . ')', array_merge( array( (int) $module['id'] ), $ids ) ),
			ARRAY_A
		);
		if ( count( $rows ) !== count( $ids ) ) {
			throw new EngineError( EngineError::VALIDATION, 'This booking page has no booking policy yet.' );
		}
		return Policy::merge( array_map( array( Policy::class, 'from_row' ), $rows ) );
	}
}
