<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * Salon and barber: one base service plus optional add-ons, one stylist. Ported from allocators/salon.py.
 *
 * Add-ons lengthen the block (the stylist is reserved for base + add-ons back to back), but who can do the appointment
 * follows the base service only: an add-on linked to another stylist never narrows the choice.
 */
final class SalonAllocator implements Allocator {

	/** @var StaffAllocator */
	private $staff;

	public function __construct() {
		$this->staff = new StaffAllocator();
	}

	public function find_slots( ScheduleSource $source, AvailabilityRequest $req ): array {
		return $this->staff->find_slots( $source, self::prepare( $req ) );
	}

	public function allocate( ScheduleSource $source, AvailabilityRequest $req, int $start ): array {
		return $this->staff->allocate( $source, self::prepare( $req ), $start );
	}

	/**
	 * [base service, add-ons] of a cart.
	 *
	 * @throws EngineError validation unless the cart has exactly one base service
	 */
	public static function split_cart( array $services ): array {
		$base = array_values( array_filter( $services, static function ( $s ) {
			return empty( $s['is_addon'] );
		} ) );
		if ( 1 !== count( $base ) ) {
			throw new EngineError(
				EngineError::VALIDATION,
				sprintf( 'An appointment needs exactly one base service; add-ons are optional extras (got %d base services).', count( $base ) ),
				array( 'field' => 'service_ids' )
			);
		}
		return array( $base[0], array_values( array_filter( $services, static function ( $s ) {
			return ! empty( $s['is_addon'] );
		} ) ) );
	}

	private static function prepare( AvailabilityRequest $req ): AvailabilityRequest {
		list( $base ) = self::split_cart( $req->services );
		$copy                      = clone $req;
		$copy->qualifying_services = array( $base );
		return $copy;
	}
}
