<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * Module-type strategy: which slots exist and which resources serve a chosen one. Allocators only decide;
 * BookingService writes the reservation inside its lock and transaction.
 */
interface Allocator {

	/** @return SlotOption[] */
	public function find_slots( ScheduleSource $source, AvailabilityRequest $req ): array;

	/**
	 * @return Assignment[]
	 * @throws EngineError slot_unavailable, with the day's slots in data['slots']
	 */
	public function allocate( ScheduleSource $source, AvailabilityRequest $req, int $start ): array;
}
