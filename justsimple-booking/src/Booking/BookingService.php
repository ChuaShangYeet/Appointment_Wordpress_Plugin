<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Catalog;
use JustSimple\Booking\Data\Db;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Data\WpScheduleSource;
use JustSimple\Booking\Engine\Availability;
use JustSimple\Booking\Engine\AvailabilityRequest;
use JustSimple\Booking\Engine\Clock;
use JustSimple\Booking\Engine\EngineError;
use JustSimple\Booking\Engine\MinimumSpend;
use JustSimple\Booking\Engine\Policy;
use JustSimple\Booking\Engine\RestaurantAllocator;
use JustSimple\Booking\Engine\Allocators;
use JustSimple\Booking\Engine\StatusFlow;

defined( 'ABSPATH' ) || exit;

// phpcs:disable WordPress.DB.PreparedSQL.InterpolatedNotPrepared -- table names come from Schema::table(), values are prepared.

/**
 * Creates and changes bookings. The one place that writes bookings and allocations.
 *
 * No double booking: MySQL has no exclusion constraint, so every booking decision for a module takes the named lock
 * jsb:module:<id> first, re-computes the slot against committed rows, checks the chosen resource once more, and writes
 * everything in one InnoDB transaction. Two requests for one module therefore decide one after the other.
 */
final class BookingService {

	/** Seconds a request waits for another booking of the same module to finish deciding. */
	const LOCK_WAIT = 10;

	/** Module types bookable in this release. */
	const BOOKABLE_TYPES = array( 'staff', 'salon', 'restaurant', 'workshop', 'field' );

	/**
	 * Availability for one business day.
	 *
	 * @return array{date:string,slots:array}
	 */
	public static function availability( int $module_id, string $date, array $service_ids, ?int $resource_id, array $party = array(), ?int $now = null ): array {
		$now = $now ?? time();
		HoldSweeper::run( $now );
		$module = Catalog::bookable_module( $module_id );
		$req    = self::request( $module, $service_ids, $resource_id, $date, $now, $party );
		$slots  = Allocators::for_type( $module['type'] )->find_slots( new WpScheduleSource(), $req );
		$out = array(
			'date'  => $date,
			'slots' => array_map( static function ( $s ) use ( $req ) {
				return $s->to_array( $req->tz );
			}, $slots ),
		);
		if ( 'workshop' === $module['type'] ) { // parts that must be ordered: the customer acknowledges the lead time before booking
			$out['lead_time_notice'] = Parts::lead_time_notice( $req->service_ids() );
		}
		return $out;
	}

	/**
	 * Which dates of a month have at least one free slot.
	 *
	 * @return array{month:string,dates:string[]}
	 */
	public static function month_availability( int $module_id, string $month, array $service_ids, ?int $resource_id, array $party = array(), ?int $now = null ): array {
		$now    = $now ?? time();
		HoldSweeper::run( $now );
		$module = Catalog::bookable_module( $module_id );
		$req    = self::request( $module, $service_ids, $resource_id, $month . '-01', $now, $party );
		list( $earliest, $latest ) = $req->policy->bounds( $now );
		$first_open = Clock::local_date( $earliest, $req->tz );
		$last_open  = Clock::local_date( $latest, $req->tz );
		$source     = new WpScheduleSource();
		$allocator  = Allocators::for_type( $module['type'] );
		$dates      = array();
		$days       = (int) ( new \DateTimeImmutable( $month . '-01' ) )->format( 't' );
		for ( $d = 1; $d <= $days; $d++ ) {
			$day = sprintf( '%s-%02d', $month, $d );
			// a night shift from the day before can reach into the first open day, so start one day early
			if ( $day < Clock::add_days( $first_open, -1 ) || $day > $last_open ) {
				continue;
			}
			if ( $allocator->find_slots( $source, $req->with_day( $day ) ) ) {
				$dates[] = $day;
			}
		}
		return array( 'month' => $month, 'dates' => $dates );
	}

	/**
	 * Book a slot.
	 *
	 * @param array{service_ids:int[],staff_id:?int,start:string,customer:array{name:string,phone:string,phone_country?:?string,email:?string},special_request:?string,idempotency_key:?string,source:?string} $in
	 * @return array{booking:array,manage_token:string,replayed:bool}
	 * @throws EngineError
	 */
	public static function create( int $module_id, array $in, ?int $now = null ): array {
		global $wpdb;
		$now    = $now ?? time();
		$module = Catalog::bookable_module( $module_id );
		if ( ! in_array( $module['type'], self::BOOKABLE_TYPES, true ) ) {
			throw new EngineError( EngineError::VALIDATION, 'This kind of booking page is not available yet.' );
		}
		if ( ! empty( $in['health_notes'] ) ) { // stored only once they can be encrypted (PDPA), never in clear
			throw new EngineError( EngineError::VALIDATION, 'Health notes are not accepted yet.', array( 'field' => 'health_notes' ) );
		}

		$start = self::parse_start( (string) ( $in['start'] ?? '' ) );
		$name  = trim( (string) ( $in['customer']['name'] ?? '' ) );
		if ( '' === $name || mb_strlen( $name ) > 150 ) {
			throw new EngineError( EngineError::VALIDATION, 'Enter your name (up to 150 characters).', array( 'field' => 'customer.name' ) );
		}
		$phone = Customers::phone_for_booking( (string) ( $in['customer']['phone'] ?? '' ), $in['customer']['phone_country'] ?? null, $module['settings']['country'] );
		$email = trim( (string) ( $in['customer']['email'] ?? '' ) );
		if ( '' !== $email && ! is_email( $email ) ) {
			throw new EngineError( EngineError::VALIDATION, 'Enter a valid email address, or leave it empty.', array( 'field' => 'customer.email' ) );
		}
		$email   = '' === $email ? null : strtolower( $email );
		$request = trim( (string) ( $in['special_request'] ?? '' ) );
		if ( mb_strlen( $request ) > 1000 ) {
			throw new EngineError( EngineError::VALIDATION, 'Keep the request under 1000 characters.', array( 'field' => 'special_request' ) );
		}
		$key        = self::clean_idempotency_key( $in['idempotency_key'] ?? null );
		$restaurant = 'restaurant' === $module['type'];
		$named      = $restaurant ? ( $in['table_id'] ?? null ) : ( $in['staff_id'] ?? null );
		$party      = $restaurant ? self::party( $in ) : array();
		$spend      = $restaurant ? self::accepted_spend( $in ) : array( false, null );
		$workshop   = 'workshop' === $module['type'];
		if ( ! $workshop && ( isset( $in['vehicle'] ) || isset( $in['vehicle_id'] ) ) ) {
			throw new EngineError( EngineError::VALIDATION, 'Vehicles only apply to workshop bookings.', array( 'field' => 'vehicle' ) );
		}
		if ( $workshop && isset( $in['vehicle_id'] ) ) { // no customer sign-in yet: a saved vehicle id is not taken from the public
			throw new EngineError( EngineError::VALIDATION, 'Give the vehicle as plate_no, supported_vehicle_id and year.', array( 'field' => 'vehicle_id' ) );
		}
		$vehicle = $workshop ? Vehicles::check( $module_id, $in['vehicle'] ?? null ) : null;
		$ack     = in_array( $in['acknowledge_lead_time'] ?? false, array( true, 1, '1', 'true' ), true );
		$field   = 'field' === $module['type'];
		if ( isset( $in['address_id'] ) ) { // no customer sign-in yet: a saved address id is not taken from the public
			throw new EngineError( EngineError::VALIDATION, 'Give the address with its map pin.', array( 'field' => 'address_id' ) );
		}
		if ( ! $field && isset( $in['address'] ) ) {
			throw new EngineError( EngineError::VALIDATION, 'An address applies only to on-site services.', array( 'field' => 'address' ) );
		}
		$address = $field && isset( $in['address'] ) ? Addresses::check( $in['address'] ) : null;
		if ( $address ) {
			$party['latitude']  = $address['latitude'];
			$party['longitude'] = $address['longitude'];
		}
		// what makes two requests "the same" for idempotency: everything the customer chose
		$fingerprint = hash( 'sha256', wp_json_encode( array( $module_id, array_map( 'intval', (array) ( $in['service_ids'] ?? array() ) ), $named, $start, $name, $phone, $email, $request, $party, $spend, $vehicle ? array( $vehicle['plate_no'], $vehicle['supported']['id'], $vehicle['year'] ) : null, $ack, $address ) ) );

		HoldSweeper::run( $now );
		$req = self::request( $module, (array) ( $in['service_ids'] ?? array() ), $named ? (int) $named : null, Clock::local_date( $start, wp_timezone() ), $now, $party );
		$req->policy->validate_start( $start, $now );
		if ( $field && 1 === count( $req->services ) ) {
			$on_site = 'on_site' === $req->services[0]['location_type'];
			if ( $on_site && ! $address ) {
				throw new EngineError( EngineError::VALIDATION, 'An on-site service needs the address to visit.', array( 'field' => 'address' ) );
			}
			if ( ! $on_site && $address ) {
				throw new EngineError( EngineError::VALIDATION, 'An address applies only to on-site services.', array( 'field' => 'address' ) );
			}
		}
		$notice = $workshop ? Parts::lead_time_notice( $req->service_ids() ) : array();
		if ( $notice && ! $ack ) { // checked before anything is written
			throw new EngineError( EngineError::LEAD_TIME, 'Some parts must be ordered; please acknowledge the estimated lead time.', array( 'parts' => $notice ) );
		}

		$lock = 'jsb:module:' . $module_id;
		if ( '1' !== (string) $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, %d)', $lock, self::LOCK_WAIT ) ) ) {
			throw new EngineError( EngineError::BUSY, 'Many people are booking right now. Please try again in a moment.' );
		}
		try {
			$result = Db::transaction(
				function () use ( $wpdb, $module, $req, $start, $name, $phone, $email, $request, $key, $fingerprint, $now, $in, $restaurant, $spend, $vehicle, $notice, $address ) {
					if ( null !== $key ) {
						$existing = $wpdb->get_row( $wpdb->prepare( 'SELECT id, idempotency_hash FROM ' . Schema::table( 'bookings' ) . ' WHERE module_id = %d AND idempotency_key = %s', $module['id'], $key ), ARRAY_A );
						if ( $existing ) {
							if ( ! hash_equals( (string) $existing['idempotency_hash'], $fingerprint ) ) {
								throw new EngineError( EngineError::IDEMPOTENCY_REUSED, 'This request key was already used for a different booking. Start a new booking.' );
							}
							return array( (int) $existing['id'], true );
						}
					}

					list( $assignments, $business_day ) = Availability::allocate_on_business_day( Allocators::for_type( $module['type'] ), new WpScheduleSource(), $req, $start );
					self::assert_still_free( $assignments, $now );
					// VIP tables: the amount is decided now, under the lock, and never above what the customer accepted
					$minimum_spend = (int) array_sum( array_map( static function ( $a ) {
						return $a->minimum_spend;
					}, $assignments ) );
					MinimumSpend::ensure_accepted( $minimum_spend, $spend[0], $spend[1] );

					$customer_id = Customers::upsert( $name, $phone, $email );
					$vehicle_id  = $vehicle ? Vehicles::upsert( $customer_id, $vehicle ) : null;
					$pending     = 'pending' === $req->policy->default_status;
					$booking_id  = self::write_booking(
						array(
							'module_id'         => $module['id'],
							'customer_id'       => $customer_id,
							'contact_email'     => $email,
							'idempotency_key'   => $key,
							'idempotency_hash'  => null === $key ? null : $fingerprint,
							'status'            => $req->policy->default_status,
							'policy_snapshot'   => wp_json_encode( $req->policy->snapshot() ),
							'special_request'   => '' === $request ? null : $request,
							'party_size'        => $restaurant ? $req->party_size : null,
							'table_type'        => $restaurant ? $req->table_type : null,
							'allow_separate_seating' => $restaurant ? (int) $req->allow_separate_seating : null,
							'minimum_spend_agreed'   => $minimum_spend > 0 ? MinimumSpend::format( $minimum_spend ) : null,
							'vehicle_id'             => $vehicle_id,
							'lead_time_acknowledged_utc' => $notice ? Db::to_sql( $now ) : null,
							'hold_expires_utc'  => $pending ? Db::to_sql( $req->policy->hold_until( $now, min( array_map( static function ( $a ) {
								return $a->start; // with a visit's travel time: the moment the professional must set off
							}, $assignments ) ) ) ) : null,
							'source'            => in_array( $in['source'] ?? '', array( 'widget', 'admin', 'api' ), true ) ? $in['source'] : 'widget',
							'address_id'        => $address ? Addresses::save( $customer_id, $address ) : null,
						),
						$req->services,
						$assignments,
						$pending ? 'held' : 'active',
						'Booked online.',
						$now
					);
					if ( ! $pending ) {
						FieldService::fill_meeting_link( $booking_id ); // a confirmed online appointment gets the professional's room
					}
					return array( $booking_id, false );
				}
			);
		} finally {
			$wpdb->query( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) );
		}

		list( $booking_id, $replayed ) = $result;
		$booking = self::view( $booking_id, false, $now );
		$token   = ManageLink::token( $booking['code'] );
		if ( ! $replayed ) {
			do_action( 'jsb_booking_created', $booking_id, $token );
		}
		return array( 'booking' => $booking, 'manage_token' => $token, 'replayed' => $replayed );
	}

	/**
	 * Move a booking through the state machine.
	 *
	 * @param string $actor 'staff' or 'customer'
	 * @throws EngineError
	 */
	public static function transition( int $booking_id, string $to, string $actor, ?int $user_id = null, ?string $note = null, ?int $now = null ): array {
		global $wpdb;
		$now = $now ?? time();
		if ( ! in_array( $to, Schema::BOOKING_STATUSES, true ) ) {
			throw new EngineError( EngineError::VALIDATION, 'Unknown status.', array( 'field' => 'status' ) );
		}
		HoldSweeper::run( $now );

		$from = Db::transaction(
			static function () use ( $wpdb, $booking_id, $to, $actor, $user_id, $note, $now ) {
				$b   = Schema::table( 'bookings' );
				$row = $wpdb->get_row( $wpdb->prepare( "SELECT b.id, b.status, b.start_utc, b.policy_snapshot, m.type AS module_type FROM $b b JOIN " . Schema::table( 'modules' ) . ' m ON m.id = b.module_id WHERE b.id = %d FOR UPDATE', $booking_id ), ARRAY_A );
				if ( ! $row ) {
					throw new EngineError( EngineError::NOT_FOUND, 'Booking not found.' );
				}
				$booking = array(
					'status'          => $row['status'],
					'start'           => Db::from_sql( $row['start_utc'] ),
					'policy_snapshot' => json_decode( $row['policy_snapshot'], true ),
				);
				StatusFlow::check( $booking, $to, $row['module_type'], $actor, $now );

				$alloc = Schema::table( 'booking_allocations' );
				if ( 'confirmed' === $to ) {
					$wpdb->query( $wpdb->prepare( "UPDATE $alloc SET state = 'active' WHERE booking_id = %d AND state = 'held'", $booking_id ) );
				} elseif ( in_array( $to, StatusFlow::RELEASING, true ) ) {
					$wpdb->query( $wpdb->prepare( "UPDATE $alloc SET state = 'released' WHERE booking_id = %d AND state IN ('held','active')", $booking_id ) );
				} elseif ( 'waiting-for-parts' === $to ) {
					// free the mechanic and bay from now on: rows not started are released, a running one is cut at now, finished work stays
					$sql_now = Db::to_sql( $now );
					$wpdb->query( $wpdb->prepare( "UPDATE $alloc SET state = 'released' WHERE booking_id = %d AND state IN ('held','active') AND start_utc >= %s", $booking_id, $sql_now ) );
					Db::check();
					$wpdb->query( $wpdb->prepare( "UPDATE $alloc SET end_utc = %s WHERE booking_id = %d AND state IN ('held','active') AND start_utc < %s AND end_utc > %s", $sql_now, $booking_id, $sql_now, $sql_now ) );
				}
				Db::check();
				$wpdb->update(
					$b,
					array(
						'status'           => $to,
						'hold_expires_utc' => null,
						'updated_utc'      => Db::to_sql( $now ),
					),
					array( 'id' => $booking_id )
				);
				Db::check();
				if ( 'completed' === $to && 'salon' === $row['module_type'] ) {
					Inventory::finalise_completion( $booking_id ); // retail stock leaves the shelf; the real duration is locked in
				} elseif ( 'completed' === $to && in_array( $row['module_type'], array( 'workshop', 'field' ), true ) ) {
					Inventory::deduct_stock( $booking_id ); // the job's parts leave the shelf
				}
				if ( 'confirmed' === $to ) {
					FieldService::fill_meeting_link( $booking_id );
				}
				self::history( $booking_id, $row['status'], $to, $user_id, $note, $now );
				return $row['status'];
			}
		);
		do_action( 'jsb_booking_status_changed', $booking_id, $from, $to, $actor );
		return self::view( $booking_id, 'customer' !== $actor, $now );
	}

	/**
	 * Reschedule a workshop job that is waiting for parts: new mechanic and bay allocations, status back to confirmed.
	 *
	 * Only the remaining services are scheduled when named (default: every service on the job). An admin action, so notice and
	 * booking window are skipped, but the start must be in the future and every mechanic and bay rule applies. The original start
	 * is kept; the end moves to the new end. Decided under the same per-module lock as a new booking. Ported from resume_booking.
	 *
	 * @param int[] $service_ids
	 */
	public static function resume( int $booking_id, string $start_iso, ?int $staff_id, array $service_ids, ?int $user_id = null, ?int $now = null ): array {
		global $wpdb;
		$now = $now ?? time();
		HoldSweeper::run( $now );
		$b   = Schema::table( 'bookings' );
		$row = $wpdb->get_row( $wpdb->prepare( "SELECT id, module_id, status FROM $b WHERE id = %d", $booking_id ), ARRAY_A );
		if ( ! $row ) {
			throw new EngineError( EngineError::NOT_FOUND, 'Booking not found.' );
		}
		if ( 'waiting-for-parts' !== $row['status'] ) {
			throw new EngineError( EngineError::INVALID_TRANSITION, sprintf( "Only a job waiting for parts can be resumed (status is '%s').", $row['status'] ) );
		}
		$start = self::parse_start( $start_iso );
		if ( $start <= $now ) {
			throw new EngineError( EngineError::VALIDATION, 'The new start must be in the future.', array( 'field' => 'start' ) );
		}
		$on_job = array_map( 'intval', $wpdb->get_col( $wpdb->prepare( 'SELECT service_id FROM ' . Schema::table( 'booking_items' ) . ' WHERE booking_id = %d ORDER BY seq', $booking_id ) ) );
		$wanted = $service_ids ? array_values( array_unique( array_map( 'intval', $service_ids ) ) ) : $on_job;
		if ( array_diff( $wanted, $on_job ) ) {
			throw new EngineError( EngineError::VALIDATION, 'service_ids must be services on this job.', array( 'field' => 'service_ids' ) );
		}
		$module = Catalog::module( (int) $row['module_id'] );
		$req    = self::request( $module, $wanted, $staff_id, Clock::local_date( $start, wp_timezone() ), $now );
		$req->ignore_notice = true;

		$lock = 'jsb:module:' . $module['id'];
		if ( '1' !== (string) $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, %d)', $lock, self::LOCK_WAIT ) ) ) {
			throw new EngineError( EngineError::BUSY, 'The schedule is busy. Please try again in a moment.' );
		}
		try {
			Db::transaction(
				static function () use ( $wpdb, $b, $booking_id, $module, $req, $start, $user_id, $now ) {
					if ( 'waiting-for-parts' !== $wpdb->get_var( $wpdb->prepare( "SELECT status FROM $b WHERE id = %d FOR UPDATE", $booking_id ) ) ) {
						throw new EngineError( EngineError::INVALID_TRANSITION, 'Only a job waiting for parts can be resumed.' );
					}
					list( $assignments ) = Availability::allocate_on_business_day( Allocators::for_type( $module['type'] ), new WpScheduleSource(), $req, $start );
					self::assert_still_free( $assignments, $now );
					foreach ( $assignments as $a ) {
						$wpdb->insert(
							Schema::table( 'booking_allocations' ),
							array(
								'booking_id'  => $booking_id,
								'resource_id' => $a->resource_id,
								'service_id'  => $a->service_id,
								'start_utc'   => Db::to_sql( $a->start ),
								'end_utc'     => Db::to_sql( $a->end ),
								'state'       => 'active',
							)
						);
						Db::check();
					}
					$wpdb->update( $b, array( 'status' => 'confirmed', 'end_utc' => Db::to_sql( max( array_map( static function ( $a ) {
						return $a->end;
					}, $assignments ) ) ), 'updated_utc' => Db::to_sql( $now ) ), array( 'id' => $booking_id ) );
					Db::check();
					self::history( $booking_id, 'waiting-for-parts', 'confirmed', $user_id, 'Resumed after waiting for parts.', $now );
				}
			);
		} finally {
			$wpdb->query( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) );
		}
		do_action( 'jsb_booking_status_changed', $booking_id, 'waiting-for-parts', 'confirmed', 'staff' );
		return self::view( $booking_id, true, $now );
	}

	/** Customer cancellation through the manage link. */
	public static function cancel_as_customer( string $code, string $token, ?int $now = null ): array {
		$row = self::find_by_code( $code, $token );
		return self::transition( (int) $row['id'], 'cancelled', 'customer', null, 'Cancelled by the customer.', $now );
	}

	/** The booking behind a code, if the token matches. Wrong code and wrong token look the same to the caller. */
	public static function find_by_code( string $code, string $token ): array {
		global $wpdb;
		$row = $wpdb->get_row( $wpdb->prepare( 'SELECT id, manage_token_hash FROM ' . Schema::table( 'bookings' ) . ' WHERE code = %s', strtoupper( $code ) ), ARRAY_A );
		if ( ! $row || ! ManageLink::verify( $row['manage_token_hash'], $token ) ) {
			throw new EngineError( EngineError::NOT_FOUND, 'Booking not found.' );
		}
		return $row;
	}

	/**
	 * A booking as the API shows it. The public view leaves out the customer's contact details and internal ids.
	 */
	public static function view( int $booking_id, bool $private, ?int $now = null ): array {
		global $wpdb;
		$now = $now ?? time();
		$tz  = wp_timezone();
		$b   = $wpdb->get_row(
			$wpdb->prepare(
				'SELECT b.*, m.title AS module_title, m.type AS module_type, c.name AS customer_name, c.phone AS customer_phone, c.email AS customer_email
				 FROM ' . Schema::table( 'bookings' ) . ' b JOIN ' . Schema::table( 'modules' ) . ' m ON m.id = b.module_id
				 JOIN ' . Schema::table( 'customers' ) . ' c ON c.id = b.customer_id WHERE b.id = %d',
				$booking_id
			),
			ARRAY_A
		);
		if ( ! $b ) {
			throw new EngineError( EngineError::NOT_FOUND, 'Booking not found.' );
		}
		$services = $wpdb->get_results( $wpdb->prepare( 'SELECT s.id, s.name, i.duration_minutes, i.is_upsell, s.location_type, s.service_category, s.price_after_inspection FROM ' . Schema::table( 'booking_items' ) . ' i JOIN ' . Schema::table( 'services' ) . ' s ON s.id = i.service_id WHERE i.booking_id = %d ORDER BY i.seq', $booking_id ), ARRAY_A );
		$staff    = $wpdb->get_results( $wpdb->prepare( 'SELECT DISTINCT r.id, r.name, r.type FROM ' . Schema::table( 'booking_allocations' ) . ' a JOIN ' . Schema::table( 'resources' ) . ' r ON r.id = a.resource_id WHERE a.booking_id = %d', $booking_id ), ARRAY_A );
		$snapshot = json_decode( $b['policy_snapshot'], true );
		$state    = array(
			'status'          => $b['status'],
			'start'           => Db::from_sql( $b['start_utc'] ),
			'policy_snapshot' => $snapshot,
		);
		$deadline = StatusFlow::cancel_deadline( $state );
		$out      = array(
			'code'            => $b['code'],
			'status'          => $b['status'],
			'module'          => array( 'id' => (int) $b['module_id'], 'title' => $b['module_title'] ),
			'start'           => Clock::format( Db::from_sql( $b['start_utc'] ), $tz ),
			'end'             => Clock::format( Db::from_sql( $b['end_utc'] ), $tz ),
			'services'        => array_map( static function ( $s ) {
				return array( 'id' => (int) $s['id'], 'name' => $s['name'], 'duration_minutes' => (int) $s['duration_minutes'], 'is_upsell' => (bool) (int) $s['is_upsell'], 'location_type' => $s['location_type'], 'service_category' => $s['service_category'], 'price_after_inspection' => (bool) (int) $s['price_after_inspection'] );
			}, $services ),
			( 'restaurant' === $b['module_type'] ? 'tables' : 'staff' ) => self::resources_of( $staff, 'restaurant' === $b['module_type'] ? 'table' : 'staff' ),
			'bays'            => 'workshop' === $b['module_type'] ? self::resources_of( $staff, 'bay' ) : null,
			'vehicle'         => self::vehicle_of( $b['vehicle_id'] ),
			'address'         => self::address_of( $b['address_id'] ),
			'meeting_link'    => $b['meeting_link'],
			'follow_up_of'    => $b['parent_booking_id'] ? (int) $b['parent_booking_id'] : null,
			'lead_time_acknowledged' => null !== $b['lead_time_acknowledged_utc'],
			'customer'        => array( 'name' => $b['customer_name'] ),
			'special_request' => $b['special_request'],
			'party_size'      => null === $b['party_size'] ? null : (int) $b['party_size'],
			'table_type'      => $b['table_type'],
			'minimum_spend_agreed' => $b['minimum_spend_agreed'],
			'hold_expires'    => $b['hold_expires_utc'] ? Clock::format( Db::from_sql( $b['hold_expires_utc'] ), $tz ) : null,
			'can_cancel'      => in_array( $b['status'], StatusFlow::CUSTOMER_CANCELLABLE, true ) && StatusFlow::can_cancel_online( $state, $now ),
			'cancel_deadline' => null === $deadline ? null : Clock::format( $deadline, $tz ),
		);
		if ( $private ) {
			$out['id']       = (int) $b['id'];
			$out['customer'] = array(
				'id'    => (int) $b['customer_id'],
				'name'  => $b['customer_name'],
				'phone' => $b['customer_phone'],
				'email' => $b['customer_email'],
			);
			$out['contact_email'] = $b['contact_email'];
			$out['source']        = $b['source'];
			$out['created']       = Clock::format( Db::from_sql( $b['created_utc'] ), $tz );
			$out['schedule_conflict'] = (bool) (int) $b['schedule_conflict'];
			$out['actual_duration_minutes'] = null === $b['actual_duration_minutes'] ? null : (int) $b['actual_duration_minutes'];
			if ( 'workshop' === $b['module_type'] ) {
				$out['parts'] = Parts::lines( $booking_id );
			}
			$out['products'] = array_map( static function ( $p ) {
				return array( 'product_id' => (int) $p['product_id'], 'name' => $p['name'], 'quantity' => (int) $p['quantity'], 'unit_price' => $p['unit_price'] );
			}, $wpdb->get_results( $wpdb->prepare( 'SELECT l.product_id, p.name, l.quantity, l.unit_price FROM ' . Schema::table( 'booking_products' ) . ' l JOIN ' . Schema::table( 'products' ) . ' p ON p.id = l.product_id WHERE l.booking_id = %d ORDER BY p.name', $booking_id ), ARRAY_A ) );
		}
		return $out;
	}

	// ------------------------------------------------------------------ internals

	/** The allocated resources of one type, as id and name. */
	private static function resources_of( array $rows, string $type ): array {
		return array_values( array_map( static function ( $s ) {
			return array( 'id' => (int) $s['id'], 'name' => $s['name'] );
		}, array_filter( $rows, static function ( $s ) use ( $type ) {
			return $s['type'] === $type;
		} ) ) );
	}

	/** The visit address without the map pin (the pin stays with the business). */
	private static function address_of( $address_id ): ?array {
		$a = Addresses::get( $address_id );
		return $a ? array( 'line1' => $a['line1'], 'city' => $a['city'], 'postcode' => $a['postcode'] ) : null;
	}

	/**
	 * Writes a booking, its services, its allocations and the first history row; returns its id. Runs inside the caller's
	 * transaction and lock. The booking's times are the customer-visible window: the allocations may hold travel time around it.
	 *
	 * @param array<string,mixed> $fields jsb_bookings columns (module_id, customer_id, status, policy_snapshot, ...)
	 */
	public static function write_booking( array $fields, array $services, array $assignments, string $state, string $note, int $now ): int {
		global $wpdb;
		$code    = self::unique_code();
		$sql_now = Db::to_sql( $now );
		$wpdb->insert(
			Schema::table( 'bookings' ),
			array(
				'code'              => $code,
				'manage_token_hash' => ManageLink::hash( ManageLink::token( $code ) ),
				'start_utc'         => Db::to_sql( min( array_map( static function ( $a ) {
					return $a->service_start();
				}, $assignments ) ) ),
				'end_utc'           => Db::to_sql( max( array_map( static function ( $a ) {
					return $a->service_end();
				}, $assignments ) ) ),
				'created_utc'       => $sql_now,
				'updated_utc'       => $sql_now,
			) + $fields
		);
		Db::check();
		$booking_id = (int) $wpdb->insert_id;
		foreach ( $services as $n => $s ) {
			$wpdb->insert(
				Schema::table( 'booking_items' ),
				array(
					'booking_id'       => $booking_id,
					'service_id'       => $s['id'],
					'duration_minutes' => $s['duration_minutes'],
					'buffer_minutes'   => $s['buffer_minutes'],
					'seq'              => $n,
				)
			);
			Db::check();
		}
		foreach ( $assignments as $a ) {
			$wpdb->insert(
				Schema::table( 'booking_allocations' ),
				array(
					'booking_id'  => $booking_id,
					'resource_id' => $a->resource_id,
					'service_id'  => $a->service_id,
					'start_utc'   => Db::to_sql( $a->start ),
					'end_utc'     => Db::to_sql( $a->end ),
					'state'       => $state,
				)
			);
			Db::check();
		}
		self::history( $booking_id, null, (string) $fields['status'], null, $note, $now );
		return $booking_id;
	}

	private static function vehicle_of( $vehicle_id ): ?array {
		global $wpdb;
		if ( ! $vehicle_id ) {
			return null;
		}
		$v = $wpdb->get_row( $wpdb->prepare( 'SELECT plate_no, make, model, year FROM ' . Schema::table( 'vehicles' ) . ' WHERE id = %d', (int) $vehicle_id ), ARRAY_A );
		return $v ? array( 'plate_no' => $v['plate_no'], 'make' => $v['make'], 'model' => $v['model'], 'year' => null === $v['year'] ? null : (int) $v['year'] ) : null;
	}

	public static function request( array $module, array $service_ids, ?int $resource_id, string $date, int $now, array $party = array() ): AvailabilityRequest {
		if ( ! preg_match( '/^\d{4}-\d{2}-\d{2}$/', $date ) || ! checkdate( (int) substr( $date, 5, 2 ), (int) substr( $date, 8, 2 ), (int) substr( $date, 0, 4 ) ) ) {
			throw new EngineError( EngineError::VALIDATION, 'Give the date as YYYY-MM-DD.', array( 'field' => 'date' ) );
		}
		if ( 'restaurant' === $module['type'] ) { // a party, not services: the seating-time setting decides the length
			$setting = RestaurantAllocator::settings_or_fail( new WpScheduleSource(), (int) $module['id'] );
			$policy  = Catalog::resolve_restaurant_policy( $module, $setting['policy_id'] );
			$req     = new AvailabilityRequest( (int) $module['id'], wp_timezone(), array(), $policy, $date, $resource_id ?: null, $now );
			$req->party_size             = $party['party_size'] ?? null;
			$req->table_type             = $party['table_type'] ?? 'normal';
			$req->allow_separate_seating = ! empty( $party['allow_separate_seating'] );
			return $req;
		}
		$services = Catalog::services_for_booking( (int) $module['id'], $service_ids, 'salon' === $module['type'] );
		$policy   = Catalog::resolve_policy( $module, $services );
		$req      = new AvailabilityRequest( (int) $module['id'], wp_timezone(), $services, $policy, $date, $resource_id ?: null, $now );
		$req->step = 60 * max( 5, (int) $module['settings']['slot_step_minutes'] );
		if ( 'field' === $module['type'] ) { // the customer's map pin and the business location, for the service radius
			$req->latitude          = isset( $party['latitude'] ) && is_numeric( $party['latitude'] ) ? (float) $party['latitude'] : null;
			$req->longitude         = isset( $party['longitude'] ) && is_numeric( $party['longitude'] ) ? (float) $party['longitude'] : null;
			$req->business_location = array(
				'latitude'          => $module['settings']['latitude'],
				'longitude'         => $module['settings']['longitude'],
				'service_radius_km' => $module['settings']['service_radius_km'],
			);
		}
		return $req;
	}

	/**
	 * The party fields of a restaurant request, validated.
	 *
	 * @return array{party_size:int,table_type:string,allow_separate_seating:bool}
	 */
	public static function party( array $in ): array {
		$size = $in['party_size'] ?? null;
		if ( ! is_numeric( $size ) || (int) $size != $size || (int) $size < 1 || (int) $size > 200 ) { // phpcs:ignore Universal.Operators.StrictComparisons -- "4" and 4 are both fine
			throw new EngineError( EngineError::VALIDATION, 'Give the party size as a whole number from 1 to 200.', array( 'field' => 'party_size' ) );
		}
		$type = $in['table_type'] ?? 'normal';
		if ( ! in_array( $type, array( 'normal', 'vip' ), true ) ) {
			throw new EngineError( EngineError::VALIDATION, "table_type must be 'normal' or 'vip'.", array( 'field' => 'table_type' ) );
		}
		return array(
			'party_size'             => (int) $size,
			'table_type'             => $type,
			'allow_separate_seating' => in_array( $in['allow_separate_seating'] ?? false, array( true, 1, '1', 'true' ), true ),
		);
	}

	/** [accept (yes/no), accepted amount in cents or null] from a restaurant request. */
	private static function accepted_spend( array $in ): array {
		$accepted = null;
		if ( isset( $in['accepted_minimum_spend'] ) && '' !== $in['accepted_minimum_spend'] ) {
			$accepted = MinimumSpend::to_cents( $in['accepted_minimum_spend'] );
			if ( null === $accepted ) {
				throw new EngineError( EngineError::VALIDATION, 'accepted_minimum_spend must be an amount such as 300 or 300.00.', array( 'field' => 'accepted_minimum_spend' ) );
			}
		}
		return array( in_array( $in['accept_minimum_spend'] ?? false, array( true, 1, '1', 'true' ), true ), $accepted );
	}

	/** An ISO 8601 instant with an explicit offset or Z; a time without one would be ambiguous. */
	public static function parse_start( string $s ): int {
		if ( ! preg_match( '/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})$/', $s ) ) {
			throw new EngineError( EngineError::VALIDATION, 'Give the start as an ISO 8601 time with its offset, as the availability response does.', array( 'field' => 'start' ) );
		}
		try {
			return ( new \DateTimeImmutable( $s ) )->getTimestamp();
		} catch ( \Exception $e ) {
			throw new EngineError( EngineError::VALIDATION, 'That start time is not a real date.', array( 'field' => 'start' ) );
		}
	}

	private static function clean_idempotency_key( $raw ): ?string {
		if ( null === $raw || '' === $raw ) {
			return null;
		}
		if ( ! is_string( $raw ) || ! preg_match( '/^[A-Za-z0-9_.:-]{8,100}$/', $raw ) ) {
			throw new EngineError( EngineError::VALIDATION, 'The request key must be 8-100 letters, digits or _ . : - (a UUID will do).', array( 'field' => 'idempotency_key' ) );
		}
		return $raw;
	}

	/**
	 * Last line of defence inside the lock: the chosen resources must not overlap a live allocation.
	 * The allocator already decided against committed rows, so this firing means a bug, and it refuses rather than double-books.
	 */
	public static function assert_still_free( array $assignments, int $now ): void {
		global $wpdb;
		foreach ( $assignments as $a ) {
			$clash = (int) $wpdb->get_var(
				$wpdb->prepare(
					'SELECT COUNT(*) FROM ' . Schema::table( 'booking_allocations' ) . ' a JOIN ' . Schema::table( 'bookings' ) . " b ON b.id = a.booking_id
					 WHERE a.resource_id = %d AND a.state IN ('held','active') AND a.start_utc < %s AND a.end_utc > %s
					   AND (a.state = 'active' OR b.hold_expires_utc > %s) FOR UPDATE",
					$a->resource_id,
					Db::to_sql( $a->end ),
					Db::to_sql( $a->start ),
					Db::to_sql( $now )
				)
			);
			if ( $clash > 0 ) {
				throw new EngineError( EngineError::SLOT_UNAVAILABLE, 'That time was just taken. Please pick another.' );
			}
		}
	}

	public static function unique_code(): string {
		global $wpdb;
		do {
			$code = ManageLink::new_code();
		} while ( $wpdb->get_var( $wpdb->prepare( 'SELECT 1 FROM ' . Schema::table( 'bookings' ) . ' WHERE code = %s', $code ) ) );
		return $code;
	}

	public static function history( int $booking_id, ?string $from, string $to, ?int $user_id, ?string $note, int $now ): void {
		global $wpdb;
		$wpdb->insert(
			Schema::table( 'status_history' ),
			array(
				'booking_id'    => $booking_id,
				'from_status'   => $from,
				'to_status'     => $to,
				'actor_user_id' => $user_id,
				'note'          => $note,
				'created_utc'   => Db::to_sql( $now ),
			)
		);
		Db::check();
	}
}
