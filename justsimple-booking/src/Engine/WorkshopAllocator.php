<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * Workshop: a mechanic AND a bay for every service of a job, the services run back to back. Ported from allocators/workshop.py (blueprint §10).
 *
 * - Each service is a segment of duration + buffer; segment n starts when segment n-1 ends. Every segment needs a qualified mechanic
 *   and a qualified bay, both free for the whole segment, and writes two allocation rows (one each), tagged with its service.
 * - Segments never overlap, so the choice per segment is independent: a start is offered when every segment has a free mechanic and bay.
 * - Continuity: the previous segment's mechanic and bay are kept while still free; otherwise the least-loaded mechanic or the
 *   lowest-named bay, then the id.
 * - Mechanics follow shifts, breaks, days off and opening hours; bays only opening hours and days off. No opening hours = open all day.
 * - A named mechanic must be qualified for, and free during, every segment; the bay is still chosen.
 */
final class WorkshopAllocator implements Allocator {

	public function find_slots( ScheduleSource $source, AvailabilityRequest $req ): array {
		$out = array();
		foreach ( $this->plan( self::source( $source ), $req ) as $p ) {
			$out[] = self::slot( $p );
		}
		return $out;
	}

	public function allocate( ScheduleSource $source, AvailabilityRequest $req, int $start ): array {
		$plan = $this->plan( self::source( $source ), $req );
		foreach ( $plan as $p ) {
			if ( $p[0] === $start ) {
				return $p[2];
			}
		}
		throw new EngineError(
			EngineError::SLOT_UNAVAILABLE,
			'That time is not available for this job.',
			array( 'slots' => array_map( static function ( $p ) use ( $req ) {
				return self::slot( $p )->to_array( $req->tz );
			}, $plan ) )
		);
	}

	// ------------------------------------------------------------------ pure logic

	/**
	 * Contiguous [service, interval] segments, each duration + buffer long, in the order the services were chosen.
	 *
	 * @return array<int,array{0:array,1:Interval}>
	 */
	public static function plan_segments( int $start, array $services ): array {
		$out    = array();
		$cursor = $start;
		foreach ( $services as $s ) {
			$end    = $cursor + 60 * ( (int) $s['duration_minutes'] + (int) $s['buffer_minutes'] );
			$out[]  = array( $s, new Interval( $cursor, $end ) );
			$cursor = $end;
		}
		return $out;
	}

	/**
	 * Continuity first (the previous segment's resource), else least loaded, then name, then id.
	 *
	 * @param int[]              $candidates
	 * @param array<int,float>   $load
	 * @param array<int,string>  $names
	 */
	public static function choose_resource( array $candidates, ?int $previous, array $load, array $names ): int {
		if ( null !== $previous && in_array( $previous, $candidates, true ) ) {
			return $previous;
		}
		usort( $candidates, static function ( $a, $b ) use ( $load, $names ) {
			return ( $load[ $a ] ?? 0.0 ) <=> ( $load[ $b ] ?? 0.0 ) ?: strcmp( $names[ $a ] ?? '', $names[ $b ] ?? '' ) ?: $a <=> $b;
		} );
		return $candidates[0];
	}

	// ------------------------------------------------------------------ the plan

	/** @return array<int,array{0:int,1:int,2:Assignment[]}> [start, end, assignments] for every offered start */
	private function plan( WorkshopSource $source, AvailabilityRequest $req ): array {
		if ( ! $req->services ) {
			throw new EngineError( EngineError::VALIDATION, 'Choose at least one service.', array( 'field' => 'service_ids' ) );
		}
		if ( null !== $req->resource_id ) {
			$named = $source->resources( array( $req->resource_id ) );
			if ( ! isset( $named[ $req->resource_id ] ) || 'staff' !== $named[ $req->resource_id ]['type'] ) {
				throw new EngineError( EngineError::NOT_FOUND, 'Mechanic not found.', array( 'field' => 'staff_id' ) );
			}
		}

		$offs = $source->days_off( $req->module_id );
		foreach ( $offs as $o ) {
			if ( null === $o['resource_id'] && Clock::covers_date( $o, $req->day ) ) {
				return array();
			}
		}
		$day_start = $req->day_start();
		$day_end   = $req->overnight_end(); // a night shift may run into the next day
		$op        = Availability::operating_windows( $source, $req );
		if ( array() === $op ) {
			return array();
		}
		$hours = null === $op ? array( new Interval( $day_start, $day_end ) ) : $op;

		// who and where, per service and per resource type
		$mechanics = array();
		$bays      = array();
		foreach ( $req->services as $s ) {
			$mechanics[ $s['id'] ] = $source->qualified_resource_ids( $req->module_id, 'staff', array( (int) $s['id'] ), $req->resource_id );
			$bays[ $s['id'] ]      = $source->qualified_resource_ids( $req->module_id, 'bay', array( (int) $s['id'] ), null );
			if ( ! $mechanics[ $s['id'] ] || ! $bays[ $s['id'] ] ) {
				return array(); // some service has nobody, or no bay, that can do it at all
			}
		}
		$on_leave = array();
		foreach ( $offs as $o ) {
			if ( null !== $o['resource_id'] && Clock::covers_date( $o, $req->day ) ) {
				$on_leave[] = (int) $o['resource_id'];
			}
		}
		$mech_ids = array_values( array_diff( array_unique( array_merge( ...array_values( $mechanics ) ) ), $on_leave ) );
		$bay_ids  = array_values( array_diff( array_unique( array_merge( ...array_values( $bays ) ) ), $on_leave ) );
		$names    = array_map( static function ( $r ) {
			return $r['name'];
		}, $source->resources( array_merge( $mech_ids, $bay_ids ) ) );

		$working = array();
		foreach ( $source->shifts( $mech_ids, Clock::weekday_index( $req->day ) ) as $sh ) {
			foreach ( Clock::shift_windows( $sh, $req->day, $req->tz ) as $w ) {
				$working[ (int) $sh['resource_id'] ][] = $w;
			}
		}
		$busy      = $source->busy( array_merge( $mech_ids, $bay_ids ), $day_start, $day_end, $req->now );
		$mech_free = array();
		$load      = array();
		foreach ( $mech_ids as $m ) {
			$mech_free[ $m ] = IntervalMath::subtract( IntervalMath::intersect( $working[ $m ] ?? array(), $hours ), $busy[ $m ] ?? array() );
			$load[ $m ]      = array_sum( array_map( static function ( $i ) {
				return $i->minutes();
			}, $busy[ $m ] ?? array() ) );
		}
		$bay_free = array();
		foreach ( $bay_ids as $b ) {
			$bay_free[ $b ] = IntervalMath::subtract( $hours, $busy[ $b ] ?? array() );
		}

		list( $earliest, $latest ) = $req->policy->bounds( $req->now, $req->ignore_notice );
		$plan = array();
		for ( $start = $day_start; $start < $day_end; $start += $req->step ) { // the business day may run past midnight
			if ( $start < $earliest || $start > $latest ) {
				continue;
			}
			$assignments = self::assign( $start, $req->services, $mechanics, $bays, $mech_free, $bay_free, $names, $load );
			if ( $assignments ) {
				$plan[] = array( $start, max( array_map( static function ( $a ) {
					return $a->end;
				}, $assignments ) ), $assignments );
			}
		}
		return $plan;
	}

	/** Mechanic and bay for every segment starting at `$start`, or array() when any segment cannot be served. */
	private static function assign( int $start, array $services, array $mechanics, array $bays, array $mech_free, array $bay_free, array $names, array $load ): array {
		$out       = array();
		$prev_mech = null;
		$prev_bay  = null;
		foreach ( self::plan_segments( $start, $services ) as $segment ) {
			list( $svc, $seg ) = $segment;
			$m = array_values( array_filter( $mechanics[ $svc['id'] ], static function ( $id ) use ( $names, $mech_free, $seg ) {
				return isset( $names[ $id ], $mech_free[ $id ] ) && IntervalMath::fits( $mech_free[ $id ], $seg );
			} ) );
			$b = array_values( array_filter( $bays[ $svc['id'] ], static function ( $id ) use ( $names, $bay_free, $seg ) {
				return isset( $names[ $id ], $bay_free[ $id ] ) && IntervalMath::fits( $bay_free[ $id ], $seg );
			} ) );
			if ( ! $m || ! $b ) {
				return array();
			}
			$prev_mech = self::choose_resource( $m, $prev_mech, $load, $names );
			$prev_bay  = self::choose_resource( $b, $prev_bay, array(), $names );
			$out[]     = new Assignment( $prev_mech, $seg->start, $seg->end, (int) $svc['id'] );
			$out[]     = new Assignment( $prev_bay, $seg->start, $seg->end, (int) $svc['id'] );
		}
		return $out;
	}

	/** @param array{0:int,1:int,2:Assignment[]} $p */
	private static function slot( array $p ): SlotOption {
		$mechs = array();
		$bays  = array();
		foreach ( $p[2] as $i => $a ) { // assign() writes mechanic, then bay, for every segment
			if ( 0 === $i % 2 ) {
				$mechs[] = $a->resource_id;
			} else {
				$bays[] = $a->resource_id;
			}
		}
		return SlotOption::job( $p[0], $p[1], array_values( array_unique( $mechs ) ), array_values( array_unique( $bays ) ) );
	}

	private static function source( ScheduleSource $source ): WorkshopSource {
		if ( ! $source instanceof WorkshopSource ) {
			throw new \LogicException( 'A workshop needs a WorkshopSource.' );
		}
		return $source;
	}
}
