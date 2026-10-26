<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** Spa, clinic, consultant: one staff member per booking, named or "any available". Ported from allocators/staff.py. */
final class StaffAllocator implements Allocator {

	public function find_slots( ScheduleSource $source, AvailabilityRequest $req ): array {
		return Availability::compute( $source, $req, 'staff' );
	}

	public function allocate( ScheduleSource $source, AvailabilityRequest $req, int $start ): array {
		$slots = $this->find_slots( $source, $req );
		$slot  = null;
		foreach ( $slots as $s ) {
			if ( $s->start === $start ) { // compared as instants, so both passes of a repeated clock hour can be booked
				$slot = $s;
				break;
			}
		}
		if ( null === $slot ) {
			throw new EngineError(
				EngineError::SLOT_UNAVAILABLE,
				'That time is not available.',
				array( 'slots' => array_map( static function ( $s ) use ( $req ) {
					return $s->to_array( $req->tz );
				}, $slots ) )
			);
		}

		if ( null !== $req->resource_id ) { // a named choice: the pipeline already restricted candidates to it
			$chosen = $req->resource_id;
		} else {
			$busy = $source->busy( $slot->resource_ids, $req->day_start(), $req->overnight_end(), $req->now );
			$load = array();
			foreach ( $busy as $rid => $ivs ) {
				$load[ $rid ] = array_sum( array_map( static function ( $i ) {
					return $i->minutes();
				}, $ivs ) );
			}
			$chosen = self::pick_least_loaded( $slot->resource_ids, $load );
		}
		return array( new Assignment( $chosen, $slot->start, $slot->end ) );
	}

	/**
	 * "Any available": fewest booked minutes that day; ties go to the lowest id so the choice is repeatable.
	 *
	 * @param int[]             $candidates
	 * @param array<int,float>  $load_minutes
	 */
	public static function pick_least_loaded( array $candidates, array $load_minutes ): int {
		$best = null;
		foreach ( $candidates as $c ) {
			$key = array( $load_minutes[ $c ] ?? 0.0, $c );
			if ( null === $best || $key < $best ) {
				$best = $key;
			}
		}
		return (int) $best[1];
	}
}
