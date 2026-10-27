<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * Field service: one professional for one service in the office, online or at the customer's address.
 * Ported from allocators/field_service.py (blueprint §11).
 *
 * - On-site requests carry the customer's coordinates and are refused outside the business's service radius.
 * - An on-site visit reserves travel time before and after the visit on the professional's schedule; the whole padded block must fit
 *   the shift, breaks and opening hours and be free. The booking shows the visit; the allocation holds the padded time.
 *   Travel is per visit (back to base each time), as in the engine.
 * - The least-loaded qualified professional that day, or a named one. No opening hours = open all day.
 */
final class FieldAllocator implements Allocator {

	public function find_slots( ScheduleSource $source, AvailabilityRequest $req ): array {
		$out = array();
		foreach ( $this->plan( self::source( $source ), $req ) as $p ) {
			$out[] = new SlotOption( $p[0], $p[1], array( $p[2][0]->resource_id ) );
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
			'That time is not available.',
			array( 'slots' => array_map( static function ( $p ) use ( $req ) {
				return ( new SlotOption( $p[0], $p[1], array( $p[2][0]->resource_id ) ) )->to_array( $req->tz );
			}, $plan ) )
		);
	}

	/** Travel time reserved on each side of a visit, in seconds: only on-site visits travel. */
	public static function travel_pad( string $location_type, int $travel_buffer_minutes ): int {
		return 'on_site' === $location_type ? 60 * $travel_buffer_minutes : 0;
	}

	/** @return array<int,array{0:int,1:int,2:Assignment[]}> [visit start, visit end, padded assignment] */
	private function plan( WorkshopSource $source, AvailabilityRequest $req ): array {
		if ( 1 !== count( $req->services ) ) {
			throw new EngineError( EngineError::VALIDATION, 'A field-service booking takes exactly one service.', array( 'field' => 'service_ids' ) );
		}
		$svc = $req->services[0];
		$loc = $svc['location_type'] ?? 'in_office';
		if ( 'on_site' === $loc ) {
			if ( null === $req->latitude || null === $req->longitude ) {
				throw new EngineError( EngineError::VALIDATION, 'latitude and longitude are required for an on-site service.', array( 'field' => 'latitude' ) );
			}
			Geo::ensure_within( $req->business_location, $req->latitude, $req->longitude );
		}
		$pad = self::travel_pad( $loc, (int) ( $svc['travel_buffer_minutes'] ?? 0 ) );
		if ( null !== $req->resource_id ) {
			$named = $source->resources( array( $req->resource_id ) );
			if ( ! isset( $named[ $req->resource_id ] ) || 'staff' !== $named[ $req->resource_id ]['type'] ) {
				throw new EngineError( EngineError::NOT_FOUND, 'Professional not found.', array( 'field' => 'staff_id' ) );
			}
		}

		$offs = $source->days_off( $req->module_id );
		foreach ( $offs as $o ) {
			if ( null === $o['resource_id'] && Clock::covers_date( $o, $req->day ) ) {
				return array();
			}
		}
		$day_start = $req->day_start();
		$day_end   = $req->overnight_end();
		$op        = Availability::operating_windows( $source, $req );
		if ( array() === $op ) {
			return array();
		}
		$hours = null === $op ? array( new Interval( $day_start, $day_end ) ) : $op;

		$on_leave = array();
		foreach ( $offs as $o ) {
			if ( null !== $o['resource_id'] && Clock::covers_date( $o, $req->day ) ) {
				$on_leave[] = (int) $o['resource_id'];
			}
		}
		$staff = array_values( array_diff( $source->qualified_resource_ids( $req->module_id, 'staff', array( (int) $svc['id'] ), $req->resource_id ), $on_leave ) );
		if ( ! $staff ) {
			return array();
		}
		$names = array_map( static function ( $r ) {
			return $r['name'];
		}, $source->resources( $staff ) );
		$working = array();
		foreach ( $source->shifts( $staff, Clock::weekday_index( $req->day ) ) as $sh ) {
			foreach ( Clock::shift_windows( $sh, $req->day, $req->tz ) as $w ) {
				$working[ (int) $sh['resource_id'] ][] = $w;
			}
		}
		$busy = $source->busy( $staff, $day_start, $day_end, $req->now );
		$free = array();
		$load = array();
		foreach ( $staff as $m ) {
			$free[ $m ] = IntervalMath::subtract( IntervalMath::intersect( $working[ $m ] ?? array(), $hours ), $busy[ $m ] ?? array() );
			$load[ $m ] = array_sum( array_map( static function ( $i ) {
				return $i->minutes();
			}, $busy[ $m ] ?? array() ) );
		}

		list( $earliest, $latest ) = $req->policy->bounds( $req->now, $req->ignore_notice );
		$block = $req->block();
		$plan  = array();
		for ( $start = $day_start; $start < $day_end; $start += $req->step ) {
			if ( $start < $earliest || $start > $latest ) {
				continue;
			}
			$padded     = new Interval( $start - $pad, $start + $block + $pad );
			$candidates = array_values( array_filter( $staff, static function ( $m ) use ( $names, $free, $padded ) {
				return isset( $names[ $m ] ) && IntervalMath::fits( $free[ $m ], $padded );
			} ) );
			if ( $candidates ) {
				$pick   = WorkshopAllocator::choose_resource( $candidates, null, $load, $names );
				$plan[] = array( $start, $start + $block, array( new Assignment( $pick, $padded->start, $padded->end, null, 0, $pad, $pad ) ) );
			}
		}
		return $plan;
	}

	private static function source( ScheduleSource $source ): WorkshopSource {
		if ( ! $source instanceof WorkshopSource ) {
			throw new \LogicException( 'Field service needs a WorkshopSource (resource names and types).' );
		}
		return $source;
	}
}
