<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Activator;
use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

/**
 * "Own appointments only" (the engine's _own_job_only): a WordPress user linked to a staff member (resources.wp_user_id) who
 * can work bookings but not manage setup may change only the appointments they are allocated to. Managers, and desk users
 * not linked to any staff member, work every appointment.
 *
 * What they see follows the same line: the whole day, so they know the shop's day, but someone else's booking only as its time,
 * its service and the customer's first name.
 */
final class Access {

	/**
	 * The resources whose bookings this user sees and works in full; null when that is every booking.
	 *
	 * @return int[]|null
	 */
	public static function own_resources( int $user_id ): ?array {
		global $wpdb;
		if ( user_can( $user_id, Activator::CAP_MANAGE ) ) {
			return null;
		}
		$mine = array_map( 'intval', $wpdb->get_col( $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'resources' ) . ' WHERE wp_user_id = %d', $user_id ) ) );
		return $mine ?: null;
	}

	/** @throws EngineError forbidden */
	public static function assert_can_work( int $user_id, int $booking_id ): void {
		global $wpdb;
		$mine = self::own_resources( $user_id );
		if ( null === $mine ) {
			return;
		}
		$on_it = $wpdb->get_var(
			$wpdb->prepare(
				'SELECT 1 FROM ' . Schema::table( 'booking_allocations' ) . ' WHERE booking_id = %d AND resource_id IN (' . Db::ints( $mine ) . ') LIMIT 1', // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
				array_merge( array( $booking_id ), $mine )
			)
		);
		if ( ! $on_it ) {
			throw new EngineError( EngineError::FORBIDDEN, 'You can only change your own appointments.' );
		}
	}

	/** Whether a booking view (BookingService::view) has one of `$mine` among its staff, tables or bays. */
	public static function is_mine( array $view, ?array $mine ): bool {
		if ( null === $mine ) {
			return true;
		}
		foreach ( array( 'staff', 'tables', 'bays' ) as $k ) {
			foreach ( (array) ( $view[ $k ] ?? array() ) as $r ) {
				if ( in_array( (int) $r['id'], $mine, true ) ) {
					return true;
				}
			}
		}
		return false;
	}

	/** Someone else's booking, as a colleague sees it: when, what, with whom, and the customer's first name. */
	public static function redact( array $view ): array {
		$keep = array_intersect_key( $view, array_flip( array( 'id', 'status', 'module', 'start', 'end', 'staff', 'tables', 'bays', 'party_size', 'table_type' ) ) );
		return $keep + array(
			'services' => array_map( static function ( $s ) {
				return array( 'id' => $s['id'], 'name' => $s['name'], 'duration_minutes' => $s['duration_minutes'] );
			}, $view['services'] ),
			'customer' => array( 'name' => self::first_name( (string) $view['customer']['name'] ) ),
			'mine'     => false,
		);
	}

	public static function first_name( string $name ): string {
		$parts = preg_split( '/\s+/u', trim( $name ) );
		return (string) ( $parts[0] ?? '' );
	}

	/** A booking view as `$user_id` may see it. */
	public static function view_for( array $view, int $user_id ): array {
		$mine = self::own_resources( $user_id );
		return self::is_mine( $view, $mine ) ? $view + array( 'mine' => true ) : self::redact( $view );
	}
}
