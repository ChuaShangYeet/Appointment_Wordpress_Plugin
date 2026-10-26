<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * The availability pipeline, ported from compute_availability (backend/app/services/availability.py):
 *
 * 1. module-wide holidays and opening hours for the business day
 * 2. qualified active resources not on leave, and their shift windows minus breaks
 * 3. minus periods already held or active
 * 4. limited to the policy's notice and booking window
 * 5. discrete starts on the step grid, sized for duration + buffer
 */
final class Availability {

	/** @return SlotOption[] sorted by start */
	public static function compute( ScheduleSource $source, AvailabilityRequest $req, string $resource_type = 'staff' ): array {
		$tz        = $req->tz;
		$day_start = $req->day_start();

		// 1. module-wide constraints
		$offs = $source->days_off( $req->module_id );
		foreach ( $offs as $o ) {
			if ( null === $o['resource_id'] && Clock::covers_date( $o, $req->day ) ) {
				return array();
			}
		}
		$op_windows = self::operating_windows( $source, $req );
		if ( array() === $op_windows ) {
			return array();
		}

		// 2. qualified resources and their shifts
		$on_leave = array();
		foreach ( $offs as $o ) {
			if ( null !== $o['resource_id'] && Clock::covers_date( $o, $req->day ) ) {
				$on_leave[ (int) $o['resource_id'] ] = true;
			}
		}
		$ids = array_values( array_filter(
			$source->qualified_resource_ids( $req->module_id, $resource_type, $req->qualifying_ids(), $req->resource_id ),
			static function ( $id ) use ( $on_leave ) {
				return ! isset( $on_leave[ $id ] );
			}
		) );
		if ( ! $ids ) {
			return array();
		}
		$working = array();
		foreach ( $source->shifts( $ids, Clock::weekday_index( $req->day ) ) as $s ) {
			foreach ( Clock::shift_windows( $s, $req->day, $tz ) as $w ) {
				$working[ (int) $s['resource_id'] ][] = $w;
			}
		}

		// 3. existing allocations (windows may run past midnight)
		$busy = $source->busy( array_keys( $working ), $day_start, $req->overnight_end(), $req->now );

		// 4. notice and window
		list( $earliest, $latest ) = $req->policy->bounds( $req->now, $req->ignore_notice );

		// 5. candidates
		$block    = $req->block();
		$by_start = array();
		foreach ( $working as $rid => $windows ) {
			if ( null !== $op_windows ) {
				$windows = IntervalMath::intersect( $windows, $op_windows );
			}
			$free = IntervalMath::subtract( $windows, $busy[ $rid ] ?? array() );
			foreach ( IntervalMath::generate_slots( $free, $block, $req->step, $day_start, $earliest, $latest ) as $slot ) {
				$by_start[ $slot->start ][] = $rid;
			}
		}
		ksort( $by_start );
		$out = array();
		foreach ( $by_start as $start => $rids ) {
			$out[] = new SlotOption( $start, $start + $block, $rids );
		}
		return $out;
	}

	/** Opening windows for the day; null = unrestricted, array() = closed. @return Interval[]|null */
	public static function operating_windows( ScheduleSource $source, AvailabilityRequest $req ): ?array {
		$rows = $source->operating_hours( $req->module_id );
		if ( ! $rows ) {
			return null;
		}
		$wd  = Clock::weekday_index( $req->day );
		$out = array();
		foreach ( $rows as $r ) {
			if ( (int) $r['weekday'] === $wd && Clock::hms( $r['close_time'] ) !== Clock::hms( $r['open_time'] ) ) {
				$out[] = Clock::window_on( $req->day, $r['open_time'], $r['close_time'], $req->tz );
			}
		}
		return $out;
	}

	/**
	 * Allocate on `$req->day`; if that day does not offer the start, try the business day before, because with overnight hours a
	 * 01:00 start belongs to the evening before. Returns [assignments, business day used]. When neither day offers it, the error
	 * (with the requested date's slots) is the first one. Ported from _allocate_on_business_day.
	 *
	 * @return array{0:Assignment[],1:string}
	 */
	public static function allocate_on_business_day( Allocator $allocator, ScheduleSource $source, AvailabilityRequest $req, int $start ): array {
		try {
			return array( $allocator->allocate( $source, $req, $start ), $req->day );
		} catch ( EngineError $first ) {
			if ( EngineError::SLOT_UNAVAILABLE !== $first->error_code || Clock::local_date( $start, $req->tz ) !== $req->day ) {
				throw $first;
			}
			$yesterday = $req->with_day( Clock::add_days( $req->day, -1 ) );
			try {
				return array( $allocator->allocate( $source, $yesterday, $start ), $yesterday->day );
			} catch ( EngineError $second ) {
				throw $first;
			}
		}
	}
}
