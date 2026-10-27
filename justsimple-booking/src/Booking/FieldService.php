<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Catalog;
use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Data\WpScheduleSource;
use JustSimple\Booking\Engine\Allocators;
use JustSimple\Booking\Engine\Availability;
use JustSimple\Booking\Engine\Clock;
use JustSimple\Booking\Engine\EngineError;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/**
 * Field-service workflows beyond making a booking. Ported from backend/app/services/field_service.py and meetings.py.
 *
 * - Meeting links: the business brings its own meeting service. A confirmed online appointment with no link gets its professional's
 *   saved room; a link set by hand always wins and, on a confirmed booking, is emailed to the customer.
 * - Two-step work: an inspection, then a follow-up standard service linked to it, at the same customer and address.
 * - Overruns: the professional reports how long a running job really took. Longer and free afterwards: the reservation stretches.
 *   Longer into the next appointment: nothing moves, both are flagged for a human (the blueprint says flag, never auto-cancel).
 */
final class FieldService {

	/**
	 * Gives a confirmed online field appointment that has no link yet its professional's saved room. Called inside the transaction
	 * that confirms it (a new confirmed booking, or the approval of a pending one).
	 */
	public static function fill_meeting_link( int $booking_id ): void {
		global $wpdb;
		$b = $wpdb->get_row( $wpdb->prepare( 'SELECT b.status, b.meeting_link, m.type FROM ' . Schema::table( 'bookings' ) . ' b JOIN ' . Schema::table( 'modules' ) . ' m ON m.id = b.module_id WHERE b.id = %d', $booking_id ), ARRAY_A );
		if ( ! $b || 'field' !== $b['type'] || 'confirmed' !== $b['status'] || $b['meeting_link'] ) {
			return;
		}
		$virtual = $wpdb->get_var( $wpdb->prepare( 'SELECT 1 FROM ' . Schema::table( 'booking_items' ) . ' i JOIN ' . Schema::table( 'services' ) . " s ON s.id = i.service_id WHERE i.booking_id = %d AND s.location_type = 'virtual' LIMIT 1", $booking_id ) );
		if ( ! $virtual ) {
			return;
		}
		$room = $wpdb->get_var(
			$wpdb->prepare(
				'SELECT r.meeting_link FROM ' . Schema::table( 'booking_allocations' ) . ' a JOIN ' . Schema::table( 'resources' ) . " r ON r.id = a.resource_id
				 WHERE a.booking_id = %d AND a.state IN ('held','active') AND r.type = 'staff' AND r.meeting_link IS NOT NULL AND r.meeting_link <> '' ORDER BY r.name LIMIT 1",
				$booking_id
			)
		);
		if ( $room ) {
			$wpdb->update( Schema::table( 'bookings' ), array( 'meeting_link' => $room ), array( 'id' => $booking_id ) );
			Db::check();
		}
	}

	/** An http(s) web link, or null when the text is not one. */
	public static function web_link( $url ): ?string {
		if ( ! is_string( $url ) || strlen( $url ) > 2000 || ! preg_match( '#^https?://#i', $url ) || false === filter_var( $url, FILTER_VALIDATE_URL ) ) {
			return null;
		}
		return $url;
	}

	/** Sets a link on the booking by hand; it replaces the professional's room. A changed link on a confirmed booking is emailed. */
	public static function set_meeting_link( int $booking_id, $url ): array {
		global $wpdb;
		$link = self::web_link( $url );
		if ( null === $link ) {
			throw new EngineError( EngineError::VALIDATION, 'Give the meeting link as an http(s) web address.', array( 'field' => 'url' ) );
		}
		$b = self::field_booking( $booking_id );
		if ( $b['meeting_link'] !== $link ) {
			$wpdb->update( Schema::table( 'bookings' ), array( 'meeting_link' => $link, 'updated_utc' => Db::now_sql() ), array( 'id' => $booking_id ) );
			Db::check();
			if ( in_array( $b['status'], array( 'confirmed', 'in-progress' ), true ) ) {
				do_action( 'jsb_meeting_link_ready', $booking_id );
			}
		}
		return BookingService::view( $booking_id, true );
	}

	/**
	 * Books a standard service after an inspection, linked through parent_booking_id.
	 *
	 * The parent must be an inspection that is in progress or completed. The follow-up reuses the parent's customer and address (an
	 * on-site follow-up is checked against the radius again), is confirmed at once (staff make it), skips the notice rules, and must
	 * start in the future. Decided under the per-module lock like any booking.
	 */
	public static function follow_up( int $parent_id, int $service_id, string $start_iso, ?int $staff_id, ?int $user_id = null, ?int $now = null ): array {
		global $wpdb;
		$now    = $now ?? time();
		HoldSweeper::run( $now );
		$parent = self::field_booking( $parent_id );
		$kind   = $wpdb->get_var( $wpdb->prepare( 'SELECT s.service_category FROM ' . Schema::table( 'booking_items' ) . ' i JOIN ' . Schema::table( 'services' ) . ' s ON s.id = i.service_id WHERE i.booking_id = %d ORDER BY i.seq LIMIT 1', $parent_id ) );
		if ( 'inspection' !== $kind ) {
			throw new EngineError( EngineError::VALIDATION, 'Only an inspection or consultation can have a follow-up service.' );
		}
		if ( ! in_array( $parent['status'], array( 'in-progress', 'completed' ), true ) ) {
			throw new EngineError( EngineError::INVALID_TRANSITION, sprintf( "A follow-up can be booked once the inspection is under way (status is '%s').", $parent['status'] ) );
		}
		$start = BookingService::parse_start( $start_iso );
		if ( $start <= $now ) {
			throw new EngineError( EngineError::VALIDATION, 'The follow-up must start in the future.', array( 'field' => 'start' ) );
		}
		$module   = Catalog::module( (int) $parent['module_id'] );
		$services = Catalog::services_for_booking( (int) $module['id'], array( $service_id ) );
		if ( 'standard' !== $services[0]['service_category'] ) {
			throw new EngineError( EngineError::VALIDATION, 'A follow-up must be a standard service, not another inspection.', array( 'field' => 'service_id' ) );
		}
		$coords = array();
		if ( 'on_site' === $services[0]['location_type'] ) {
			$address = Addresses::get( $parent['address_id'] );
			if ( ! $address || null === $address['latitude'] || null === $address['longitude'] ) {
				throw new EngineError( EngineError::VALIDATION, 'The inspection has no mapped address to visit for on-site work.' );
			}
			$coords = array( 'latitude' => $address['latitude'], 'longitude' => $address['longitude'] );
		}
		$req                = BookingService::request( $module, array( $service_id ), $staff_id, Clock::local_date( $start, wp_timezone() ), $now, $coords );
		$req->ignore_notice = true;

		$lock = 'jsb:module:' . $module['id'];
		if ( '1' !== (string) $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, %d)', $lock, BookingService::LOCK_WAIT ) ) ) {
			throw new EngineError( EngineError::BUSY, 'The schedule is busy. Please try again in a moment.' );
		}
		try {
			$id = Db::transaction(
				static function () use ( $module, $req, $start, $parent, $coords, $now ) {
					list( $assignments ) = Availability::allocate_on_business_day( Allocators::for_type( $module['type'] ), new WpScheduleSource(), $req, $start );
					BookingService::assert_still_free( $assignments, $now );
					$id = BookingService::write_booking(
						array(
							'module_id'         => $module['id'],
							'customer_id'       => (int) $parent['customer_id'],
							'contact_email'     => $parent['contact_email'],
							'status'            => 'confirmed',
							'policy_snapshot'   => wp_json_encode( $req->policy->snapshot() ),
							'address_id'        => $coords ? (int) $parent['address_id'] : null,
							'parent_booking_id' => (int) $parent['id'],
							'source'            => 'admin',
						),
						$req->services,
						$assignments,
						'active',
						'Follow-up booked after the inspection.',
						$now
					);
					self::fill_meeting_link( $id );
					return $id;
				}
			);
		} finally {
			$wpdb->query( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) );
		}
		do_action( 'jsb_booking_created', $id, ManageLink::token( (string) $wpdb->get_var( $wpdb->prepare( 'SELECT code FROM ' . Schema::table( 'bookings' ) . ' WHERE id = %d', $id ) ) ) );
		return BookingService::view( $id, true, $now );
	}

	/**
	 * Records how long a running job really took. Not longer: only the figure. Longer and the professional is free afterwards: the
	 * reservation and the end stretch. Longer into another appointment: nothing moves and both are flagged.
	 *
	 * @return array{booking:array,conflicting_booking_ids:int[],conflicts:array}
	 */
	public static function record_actual_duration( int $booking_id, $minutes, ?int $now = null ): array {
		global $wpdb;
		$now = $now ?? time();
		if ( ! is_numeric( $minutes ) || (int) $minutes != $minutes || $minutes < 1 || $minutes > 1440 ) { // phpcs:ignore Universal.Operators.StrictComparisons -- "90" and 90 are both fine
			throw new EngineError( EngineError::VALIDATION, 'Give the minutes the job really took (1 to 1440).', array( 'field' => 'minutes' ) );
		}
		$minutes = (int) $minutes;
		$b       = self::field_booking( $booking_id );
		$lock    = 'jsb:module:' . $b['module_id'];
		if ( '1' !== (string) $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, %d)', $lock, BookingService::LOCK_WAIT ) ) ) {
			throw new EngineError( EngineError::BUSY, 'The schedule is busy. Please try again in a moment.' );
		}
		try {
			$clash_ids = Db::transaction(
				static function () use ( $wpdb, $booking_id, $minutes, $now ) {
					$t   = Schema::table( 'bookings' );
					$row = $wpdb->get_row( $wpdb->prepare( "SELECT status, start_utc FROM $t WHERE id = %d FOR UPDATE", $booking_id ), ARRAY_A );
					if ( 'in-progress' !== $row['status'] ) {
						throw new EngineError( EngineError::INVALID_TRANSITION, sprintf( "Only a job in progress can overrun (status is '%s').", $row['status'] ) );
					}
					$planned = (int) $wpdb->get_var( $wpdb->prepare( 'SELECT COALESCE(SUM(duration_minutes), 0) FROM ' . Schema::table( 'booking_items' ) . ' WHERE booking_id = %d', $booking_id ) );
					$wpdb->update( $t, array( 'actual_duration_minutes' => $minutes, 'updated_utc' => Db::to_sql( $now ) ), array( 'id' => $booking_id ) );
					Db::check();
					$overrun = 60 * ( $minutes - $planned );
					if ( $overrun <= 0 ) {
						return array();
					}
					list( $extended, $clash_ids ) = Extend::try_extend( $booking_id, $overrun, $now );
					if ( ! $extended && $clash_ids ) {
						Extend::flag( $booking_id, $clash_ids, $now );
					}
					return $clash_ids;
				}
			);
		} finally {
			$wpdb->query( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) );
		}
		if ( $clash_ids ) {
			do_action( 'jsb_schedule_conflict', $booking_id, $clash_ids );
		}
		return array(
			'booking'                 => BookingService::view( $booking_id, true, $now ),
			'conflicting_booking_ids' => $clash_ids,
			'conflicts'               => Extend::clash_details( $clash_ids ),
		);
	}

	/** The booking row, refusing bookings of other module types. */
	private static function field_booking( int $booking_id ): array {
		global $wpdb;
		$b = $wpdb->get_row( $wpdb->prepare( 'SELECT b.*, m.type FROM ' . Schema::table( 'bookings' ) . ' b JOIN ' . Schema::table( 'modules' ) . ' m ON m.id = b.module_id WHERE b.id = %d', $booking_id ), ARRAY_A );
		if ( ! $b ) {
			throw new EngineError( EngineError::NOT_FOUND, 'Booking not found.' );
		}
		if ( 'field' !== $b['type'] ) {
			throw new EngineError( EngineError::INVALID_TRANSITION, 'This action applies to field-service bookings only.' );
		}
		return $b;
	}
}
