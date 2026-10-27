<?php
/**
 * Engine test suite. Plain PHP, no WordPress, no PHPUnit: `php tests/run.php`.
 * Cases are ported from backend/tests (test_availability, test_availability_bounds, test_allocator_staff, test_overnight,
 * test_dst, test_customer_cancel) and keep the originals' names where they map one to one.
 */

define( 'ABSPATH', __DIR__ . '/' ); // the engine's files guard against direct access with this
spl_autoload_register(
	static function ( $class ) {
		$prefix = 'JustSimple\\Booking\\';
		if ( 0 === strncmp( $class, $prefix, strlen( $prefix ) ) ) {
			$file = dirname( __DIR__ ) . '/src/' . str_replace( '\\', '/', substr( $class, strlen( $prefix ) ) ) . '.php';
			if ( is_readable( $file ) ) {
				require $file;
			}
		}
	}
);

use JustSimple\Booking\Engine\Allocators;
use JustSimple\Booking\Engine\Availability;
use JustSimple\Booking\Engine\AvailabilityRequest;
use JustSimple\Booking\Engine\SalonAllocator;
use JustSimple\Booking\Engine\Clock;
use JustSimple\Booking\Engine\EngineError;
use JustSimple\Booking\Engine\FieldAllocator;
use JustSimple\Booking\Engine\Geo;
use JustSimple\Booking\Engine\Interval;
use JustSimple\Booking\Engine\IntervalMath;
use JustSimple\Booking\Engine\MinimumSpend;
use JustSimple\Booking\Engine\Policy;
use JustSimple\Booking\Engine\RestaurantSource;
use JustSimple\Booking\Engine\RestaurantAllocator;
use JustSimple\Booking\Engine\ScheduleSource;
use JustSimple\Booking\Engine\StaffAllocator;
use JustSimple\Booking\Engine\StatusFlow;
use JustSimple\Booking\Engine\WorkshopAllocator;
use JustSimple\Booking\Engine\WorkshopSource;

// ------------------------------------------------------------------ harness

$GLOBALS['jsb_tests'] = array();
function test( string $name, callable $fn ) {
	$GLOBALS['jsb_tests'][ $name ] = $fn;
}
function eq( $expected, $actual, string $what = '' ) {
	if ( $expected !== $actual ) {
		throw new Exception( ( $what ? "$what: " : '' ) . 'expected ' . var_export( $expected, true ) . ', got ' . var_export( $actual, true ) );
	}
}
function ok( bool $cond, string $what ) {
	if ( ! $cond ) {
		throw new Exception( $what );
	}
}
function throws( string $code, callable $fn ): EngineError {
	try {
		$fn();
	} catch ( EngineError $e ) {
		eq( $code, $e->error_code, 'error code' );
		return $e;
	}
	throw new Exception( "expected EngineError $code, nothing thrown" );
}

// ------------------------------------------------------------------ fixtures

const HOUR = 3600;
$KL  = new DateTimeZone( 'Asia/Kuala_Lumpur' );
$SYD = new DateTimeZone( 'Australia/Sydney' );
const D       = '2030-01-07'; // a Monday
const SPRING  = '2026-10-03'; // Saturday: the night crosses 02:00 Sunday 4 Oct, clocks go forward in Sydney
const AUTUMN  = '2027-04-03'; // Saturday: the night crosses 03:00 Sunday 4 Apr, clocks go back
const ORDINARY = '2026-09-05';
const EARLY = 946684800;  // 2000-01-01
const LATE  = 4102444800; // 2100-01-01

function at( string $day, int $h, int $m = 0, ?DateTimeZone $tz = null ): int {
	return Clock::clock_on( $day, sprintf( '%02d:%02d', $h, $m ), $tz ?? $GLOBALS['KL'] );
}
function iv( int $h1, int $m1, int $h2, int $m2 ): Interval {
	return new Interval( at( D, $h1, $m1 ), at( D, $h2, $m2 ) );
}
function hhmm( int $ts, ?DateTimeZone $tz = null ): string {
	return ( new DateTimeImmutable( '@' . $ts ) )->setTimezone( $tz ?? $GLOBALS['KL'] )->format( 'H:i' );
}
function starts( array $slots, ?DateTimeZone $tz = null ): array {
	return array_map( static function ( $s ) use ( $tz ) {
		return hhmm( $s->start, $tz );
	}, $slots );
}
function same_intervals( array $expected, array $actual, string $what = '' ) {
	$f = static function ( $l ) {
		return array_map( static function ( $i ) {
			return array( $i->start, $i->end );
		}, $l );
	};
	eq( $f( $expected ), $f( $actual ), $what );
}

/** In-memory stand-in for the jsb_ tables. */
final class MemSource implements RestaurantSource, WorkshopSource {
	public $days_off = array();
	public $hours    = array();
	public $staff    = array(); // id => true (active)
	public $links    = array(); // service id => [resource ids]
	public $shifts   = array();
	public $alloc    = array(); // [resource_id, start, end, state, hold_expires|null]
	public $tables   = array(); // restaurant: [id, name, capacity, status, is_vip, minimum_spend (cents)]
	public $seating  = null;    // restaurant: the seating-time setting
	public $rules    = array(); // restaurant: [table_type, weekday|null, specific_date|null, open_tables]
	public $bays     = array(); // workshop: id => true (active)
	public $names    = array(); // id => name (defaults to "r<id>")

	public function days_off( int $module_id ): array {
		return $this->days_off;
	}
	public function operating_hours( int $module_id ): array {
		return $this->hours;
	}
	public function qualified_resource_ids( int $module_id, string $type, array $service_ids, ?int $only_id ): array {
		$pool = 'bay' === $type ? $this->bays : $this->staff;
		$ids  = array_keys( array_filter( $pool ) );
		if ( null !== $only_id ) {
			$ids = array_values( array_intersect( $ids, array( $only_id ) ) );
		}
		foreach ( $service_ids as $sid ) {
			// links count per type, as in WpScheduleSource: a link to a bay never narrows the staff
			$of_type = array_values( array_intersect( $this->links[ $sid ] ?? array(), array_keys( $pool ) ) );
			if ( $of_type ) {
				$ids = array_values( array_intersect( $ids, $of_type ) );
			}
		}
		sort( $ids );
		return $ids;
	}
	public function shifts( array $resource_ids, int $weekday ): array {
		return array_values( array_filter( $this->shifts, static function ( $s ) use ( $resource_ids, $weekday ) {
			return in_array( $s['resource_id'], $resource_ids, true ) && $s['weekday'] === $weekday;
		} ) );
	}
	public function busy( array $resource_ids, int $lo, int $hi, int $now ): array {
		$out = array();
		foreach ( $this->alloc as $a ) {
			list( $rid, $s, $e, $state, $hold ) = $a + array( 4 => null );
			$live = 'active' === $state || ( 'held' === $state && null !== $hold && $hold > $now );
			if ( in_array( $rid, $resource_ids, true ) && $live && $s < $hi && $e > $lo ) {
				$out[ $rid ][] = new Interval( $s, $e );
			}
		}
		return $out;
	}

	public function resources( array $ids ): array {
		$out = array();
		foreach ( $ids as $id ) {
			if ( isset( $this->staff[ $id ] ) || isset( $this->bays[ $id ] ) ) {
				$out[ $id ] = array( 'name' => $this->names[ $id ] ?? "r$id", 'type' => isset( $this->bays[ $id ] ) ? 'bay' : 'staff' );
			}
		}
		return $out;
	}
	public function tables( int $module_id ): array {
		return $this->tables;
	}
	public function slot_settings( int $module_id ): ?array {
		return $this->seating;
	}
	public function open_rules( int $module_id, string $table_type ): array {
		return array_values( array_filter( $this->rules, static function ( $r ) use ( $table_type ) {
			return $r['table_type'] === $table_type;
		} ) );
	}

	/** A staff member working 09:00-18:00 with a 13:00-14:00 break every day (conftest.add_staff). */
	public function add_staff( int $id, string $from = '09:00', string $to = '18:00', ?string $bs = '13:00', ?string $be = '14:00' ) {
		$this->staff[ $id ] = true;
		for ( $d = 0; $d < 7; $d++ ) {
			$this->shifts[] = array( 'resource_id' => $id, 'weekday' => $d, 'start_time' => $from, 'end_time' => $to, 'break_start' => $bs, 'break_end' => $be );
		}
	}
	public function book( int $rid, int $start, int $end ) {
		$this->alloc[] = array( $rid, $start, $end, 'active', null );
	}
}

function policy( int $notice_min = 0, int $window_days = 3650, string $status = 'confirmed', int $cancel_min = 0 ): Policy {
	return new Policy( 1, 'Test', $status, 60 * $notice_min, 60 * $cancel_min, 86400 * $window_days, 1800 );
}
const SERVICE = array( 'id' => 1, 'duration_minutes' => 60, 'buffer_minutes' => 0 );
function req( string $day, ?int $staff = null, ?Policy $p = null, ?int $now = null, ?DateTimeZone $tz = null, array $services = array( SERVICE ) ): AvailabilityRequest {
	$tz = $tz ?? $GLOBALS['KL'];
	// "now" defaults to the start of the day before: the whole day is bookable under the default policy
	return new AvailabilityRequest( 1, $tz, $services, $p ?? policy(), $day, $staff, $now ?? Clock::clock_on( Clock::add_days( $day, -1 ), '00:00', $tz ) );
}

// ------------------------------------------------------------------ interval maths (test_availability.py)

test( 'subtract cuts holes and keeps edges', function () {
	same_intervals( array( iv( 9, 0, 10, 0 ), iv( 11, 0, 17, 0 ) ), IntervalMath::subtract( array( iv( 9, 0, 18, 0 ) ), array( iv( 10, 0, 11, 0 ), iv( 17, 0, 19, 0 ) ) ) );
} );
test( 'subtract touching busy does not shrink', function () {
	same_intervals( array( iv( 9, 0, 10, 0 ) ), IntervalMath::subtract( array( iv( 9, 0, 10, 0 ) ), array( iv( 10, 0, 11, 0 ) ) ) );
} );
test( 'subtract fully covered window disappears', function () {
	same_intervals( array(), IntervalMath::subtract( array( iv( 9, 0, 10, 0 ) ), array( iv( 8, 0, 11, 0 ) ) ) );
} );
test( 'intersect', function () {
	same_intervals( array( iv( 10, 0, 12, 0 ), iv( 17, 0, 18, 0 ) ), IntervalMath::intersect( array( iv( 9, 0, 18, 0 ) ), array( iv( 10, 0, 12, 0 ), iv( 17, 0, 20, 0 ) ) ) );
} );
test( 'slots align to grid and fit before window end', function () {
	$day0 = at( D, 0 );
	eq( array( '09:15', '09:30', '09:45', '10:00' ), starts( IntervalMath::generate_slots( array( iv( 9, 10, 11, 0 ) ), HOUR, 900, $day0, $day0, $day0 + 9 * 86400 ) ) );
} );
test( 'slot must end exactly at or before shift end', function () {
	$day0 = at( D, 0 );
	eq( array( '09:00' ), starts( IntervalMath::generate_slots( array( iv( 9, 0, 10, 0 ) ), HOUR, 1800, $day0, $day0, $day0 + 86400 ) ) );
} );
test( 'earliest and latest bounds', function () {
	eq( array( '12:00', '13:00', '14:00' ), starts( IntervalMath::generate_slots( array( iv( 9, 0, 18, 0 ) ), HOUR, HOUR, at( D, 0 ), at( D, 11, 30 ), at( D, 14, 0 ) ) ) );
} );
test( 'shift break splits window', function () {
	$w = Clock::shift_windows( array( 'start_time' => '09:00', 'end_time' => '18:00', 'break_start' => '13:00', 'break_end' => '14:00' ), D, $GLOBALS['KL'] );
	same_intervals( array( iv( 9, 0, 13, 0 ), iv( 14, 0, 18, 0 ) ), $w );
} );
test( 'weekday index is sunday zero', function () {
	eq( 0, Clock::weekday_index( '2030-01-06' ) );
	eq( 1, Clock::weekday_index( D ) );
	eq( 6, Clock::weekday_index( '2030-01-12' ) );
} );
test( 'days off single and yearly and year boundary', function () {
	$off = static function ( $s, $e, $y = false ) {
		return array( 'start_date' => $s, 'end_date' => $e, 'repeat_yearly' => $y );
	};
	ok( Clock::covers_date( $off( '2030-05-01', '2030-05-01' ), '2030-05-01' ), 'single day' );
	ok( ! Clock::covers_date( $off( '2030-05-01', '2030-05-01' ), '2031-05-01' ), 'single day, next year' );
	ok( Clock::covers_date( $off( '2020-05-01', '2020-05-01', true ), '2031-05-01' ), 'yearly' );
	$span = $off( '2020-12-30', '2021-01-02', true );
	ok( Clock::covers_date( $span, '2031-01-01' ), 'yearly across new year' );
	ok( ! Clock::covers_date( $span, '2031-01-03' ), 'after the span' );
	ok( Clock::covers_date( $off( '2020-02-29', '2020-02-29', true ), '2031-02-28' ), 'leap day falls back to 28 Feb' );
} );
test( 'an interval cannot end before it starts', function () {
	try {
		new Interval( 100, 99 );
	} catch ( InvalidArgumentException $e ) {
		eq( 0.0, ( new Interval( 100, 100 ) )->minutes(), 'empty interval' );
		return;
	}
	throw new Exception( 'no exception' );
} );

// ------------------------------------------------------------------ the pipeline (test_availability.py, end-to-end part)

test( 'slots respect shift and lunch break', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$s = starts( Availability::compute( $src, req( D ) ) );
	eq( '09:00', $s[0], 'first' );
	eq( '17:00', end( $s ), 'last: a 60-minute service must end by 18:00' );
	ok( ! in_array( '12:15', $s, true ) && ! in_array( '12:45', $s, true ), 'would run into the 13:00 break' );
	ok( in_array( '12:00', $s, true ) && in_array( '14:00', $s, true ), 'either side of the break' );
	ok( ! array_intersect( array( '13:00', '13:15', '13:45' ), $s ), 'inside the break' );
} );
test( 'company holiday removes all slots', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->days_off[] = array( 'resource_id' => null, 'start_date' => D, 'end_date' => D, 'repeat_yearly' => 0 );
	eq( array(), Availability::compute( $src, req( D ) ) );
} );
test( 'operating hours clip shifts', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->hours[] = array( 'weekday' => 1, 'open_time' => '10:00', 'close_time' => '16:00' );
	$s = starts( Availability::compute( $src, req( D ) ) );
	eq( '10:00', $s[0] );
	eq( '15:00', end( $s ) );
	$src->hours = array( array( 'weekday' => 2, 'open_time' => '10:00', 'close_time' => '16:00' ) );
	eq( array(), Availability::compute( $src, req( D ) ), 'hours exist but not for Monday = closed' );
} );
test( 'booked period is subtracted', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->book( 1, at( D, 10 ), at( D, 11 ) );
	$s = starts( Availability::compute( $src, req( D ) ) );
	ok( in_array( '09:00', $s, true ) && in_array( '11:00', $s, true ), 'touching slots stay' );
	ok( ! array_intersect( array( '09:15', '09:45', '10:00', '10:45' ), $s ), 'overlapping starts gone' );
} );
test( 'an expired hold does not block, a live one does', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$now          = at( D, 8 );
	$src->alloc[] = array( 1, at( D, 10 ), at( D, 11 ), 'held', $now - 1 );
	ok( in_array( '10:00', starts( Availability::compute( $src, req( D, null, null, $now ) ) ), true ), 'expired hold ignored' );
	$src->alloc[0][4] = $now + 60;
	ok( ! in_array( '10:00', starts( Availability::compute( $src, req( D, null, null, $now ) ) ), true ), 'live hold blocks' );
} );
test( 'staff slots follow the notice and the window', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$now = at( D, 9, 5 );
	$s   = starts( Availability::compute( $src, req( D, null, policy( 120 ), $now ) ) );
	eq( '11:15', $s[0], 'two hours of notice from 09:05, on the quarter-hour grid' );
	$tomorrow = Clock::add_days( D, 1 );
	$last     = starts( Availability::compute( $src, req( $tomorrow, null, policy( 0, 1 ), $now ) ) );
	eq( '09:00', end( $last ), 'a one-day window ends at 09:05 tomorrow: the 09:00 start is the last one offered' );
} );
test( 'booking window enforced on a chosen start', function () {
	$now = at( D, 9 );
	throws( EngineError::OUTSIDE_WINDOW, function () use ( $now ) {
		policy( 60 )->validate_start( $now + 1800, $now );
	} );
	throws( EngineError::OUTSIDE_WINDOW, function () use ( $now ) {
		policy( 0, 30 )->validate_start( $now + 31 * 86400, $now );
	} );
	policy( 60, 30 )->validate_start( $now + 3600, $now ); // the boundary itself is allowed
} );

// ------------------------------------------------------------------ staff allocator (test_allocator_staff.py)

test( 'least loaded wins', function () {
	eq( 2, StaffAllocator::pick_least_loaded( array( 1, 2, 3 ), array( 1 => 120.0, 2 => 30.0, 3 => 60.0 ) ) );
} );
test( 'unloaded staff counts as zero and ties are deterministic', function () {
	eq( 1, StaffAllocator::pick_least_loaded( array( 2, 1 ), array() ) );
	eq( 2, StaffAllocator::pick_least_loaded( array( 1, 2 ), array( 1 => 60.0 ) ) );
} );
test( 'any available routes to least loaded', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->add_staff( 2 );
	$src->book( 1, at( D, 9 ), at( D, 10 ) );
	$a = ( new StaffAllocator() )->allocate( $src, req( D ), at( D, 15 ) );
	eq( 2, $a[0]->resource_id );
} );
test( 'any available falls back to the free one', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->add_staff( 2 );
	$src->book( 2, at( D, 10 ), at( D, 11 ) );
	eq( 1, ( new StaffAllocator() )->allocate( $src, req( D ), at( D, 10 ) )[0]->resource_id );
} );
test( 'specific staff cannot be double booked even if others are free', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->add_staff( 2 );
	$src->book( 1, at( D, 10 ), at( D, 11 ) );
	$e = throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src ) {
		( new StaffAllocator() )->allocate( $src, req( D, 1 ), at( D, 10 ) );
	} );
	ok( count( $e->data['slots'] ) > 0, 'the error carries the slots still free' );
} );
test( 'break time cannot be booked', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	foreach ( array( at( D, 13 ), at( D, 12, 30 ) ) as $start ) {
		throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src, $start ) {
			( new StaffAllocator() )->allocate( $src, req( D, 1 ), $start );
		} );
	}
} );
test( 'staff on leave is excluded', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->add_staff( 2 );
	$src->days_off[] = array( 'resource_id' => 1, 'start_date' => D, 'end_date' => D, 'repeat_yearly' => 0 );
	$slots           = Availability::compute( $src, req( D ) );
	ok( $slots && array_reduce( $slots, static function ( $c, $s ) {
		return $c && array( 2 ) === $s->resource_ids;
	}, true ), 'only staff 2 offered' );
} );
test( 'service qualification restricts staff', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->add_staff( 2 );
	$src->links[1] = array( 1 );
	eq( array( 1 ), Availability::compute( $src, req( D ) )[0]->resource_ids );
	$src->links[2] = array( 2 );
	$both          = array( SERVICE, array( 'id' => 2, 'duration_minutes' => 30, 'buffer_minutes' => 0 ) );
	eq( array(), Availability::compute( $src, req( D, null, null, null, null, $both ) ), 'no one does both' );
} );
test( 'inactive staff are never offered', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->staff[1] = false;
	eq( array(), Availability::compute( $src, req( D ) ) );
} );
test( 'buffer extends the block but not past the shift', function () {
	$src = new MemSource();
	$src->add_staff( 1, '09:00', '11:00', null, null );
	$svc = array( array( 'id' => 1, 'duration_minutes' => 60, 'buffer_minutes' => 15 ) );
	$s   = Availability::compute( $src, req( D, null, null, null, null, $svc ) );
	eq( array( '09:00', '09:15', '09:30', '09:45' ), starts( $s ) );
	eq( 75 * 60, $s[0]->end - $s[0]->start, 'block is duration + buffer' );
} );

// ------------------------------------------------------------------ overnight (test_overnight.py)

test( 'hours that end before they start run into the next day', function () {
	$w = Clock::window_on( D, '18:00', '02:00', $GLOBALS['KL'] );
	eq( 8 * 60.0, $w->minutes() );
	eq( Clock::add_days( D, 1 ), Clock::local_date( $w->end, $GLOBALS['KL'] ) );
} );
test( 'a break inside an overnight shift is cut out wherever it falls', function () {
	$shift = array( 'start_time' => '20:00', 'end_time' => '04:00', 'break_start' => '23:30', 'break_end' => '00:30' );
	eq( array( '20:00-23:30', '00:30-04:00' ), array_map( static function ( $i ) {
		return hhmm( $i->start ) . '-' . hhmm( $i->end );
	}, Clock::shift_windows( $shift, D, $GLOBALS['KL'] ) ) );
	$shift['break_start'] = '01:00';
	$shift['break_end']   = '02:00';
	$w                    = Clock::shift_windows( $shift, D, $GLOBALS['KL'] );
	eq( Clock::add_days( D, 1 ), Clock::local_date( $w[0]->end, $GLOBALS['KL'] ), 'a break after midnight is on the next date' );
} );
test( 'a night shift offers slots across midnight under the day it started', function () {
	$src = new MemSource();
	$src->add_staff( 1, '22:00', '04:00', null, null );
	$s = starts( Availability::compute( $src, req( D ) ) );
	eq( '22:00', $s[0] );
	eq( '03:00', end( $s ) );
	ok( in_array( '00:30', $s, true ), 'after midnight' );
} );
test( 'an after midnight slot can be booked on the business day before', function () {
	$src   = new MemSource();
	$src->add_staff( 1, '22:00', '04:00', null, null );
	$start = at( Clock::add_days( D, 1 ), 1 ); // 01:00 on Tuesday belongs to Monday night
	$req   = req( Clock::add_days( D, 1 ) );
	list( $a, $day ) = Availability::allocate_on_business_day( new StaffAllocator(), $src, $req, $start );
	eq( D, $day );
	eq( $start, $a[0]->start );
} );
test( 'bookings either side of midnight cannot overlap', function () {
	$src = new MemSource();
	$src->add_staff( 1, '22:00', '04:00', null, null );
	$src->book( 1, at( D, 23, 30 ), at( Clock::add_days( D, 1 ), 0, 30 ) );
	$s = starts( Availability::compute( $src, req( D ) ) );
	ok( ! array_intersect( array( '22:45', '23:00', '23:30', '00:00', '00:15' ), $s ), 'overlapping starts gone' );
	ok( in_array( '00:30', $s, true ) && in_array( '22:30', $s, true ), 'touching starts stay' );
} );
test( 'a day off belongs to the business day the night began on', function () {
	$src = new MemSource();
	$src->add_staff( 1, '22:00', '04:00', null, null );
	$src->days_off[] = array( 'resource_id' => null, 'start_date' => D, 'end_date' => D, 'repeat_yearly' => 0 );
	eq( array(), Availability::compute( $src, req( D ) ), 'Monday night closed, including its early Tuesday hours' );
	ok( count( Availability::compute( $src, req( Clock::add_days( D, 1 ) ) ) ) > 0, 'Tuesday night open' );
} );

// ------------------------------------------------------------------ daylight saving (test_dst.py)

test( 'the dates the tests rely on really are the clock changes', function () {
	$off = static function ( $day ) {
		return ( new DateTimeImmutable( $day . ' 00:00', $GLOBALS['SYD'] ) )->getOffset() / 3600;
	};
	eq( array( 10, 11 ), array( $off( SPRING ), $off( Clock::add_days( SPRING, 2 ) ) ) );
	eq( array( 11, 10 ), array( $off( AUTUMN ), $off( Clock::add_days( AUTUMN, 2 ) ) ) );
} );
test( 'a night window is as long as the hours that really pass', function () {
	foreach ( array( ORDINARY => 8, SPRING => 7, AUTUMN => 9 ) as $day => $hours ) {
		eq( $hours * 60.0, Clock::window_on( $day, '22:00', '06:00', $GLOBALS['SYD'] )->minutes(), $day );
	}
} );
test( 'a daytime window is not affected', function () {
	foreach ( array( ORDINARY, SPRING, AUTUMN ) as $day ) {
		eq( 480.0, Clock::window_on( $day, '09:00', '17:00', $GLOBALS['SYD'] )->minutes(), $day );
	}
} );
test( 'a window ends at the clock time it names', function () {
	foreach ( array( SPRING, AUTUMN, ORDINARY ) as $day ) {
		$w = Clock::window_on( $day, '22:00', '06:00', $GLOBALS['SYD'] );
		eq( array( '22:00', '06:00' ), array( hhmm( $w->start, $GLOBALS['SYD'] ), hhmm( $w->end, $GLOBALS['SYD'] ) ), $day );
	}
} );
test( 'a break across the change is still an hour of real time', function () {
	list( $before, $after ) = Clock::shift_windows( array( 'start_time' => '22:00', 'end_time' => '06:00', 'break_start' => '01:00', 'break_end' => '02:00' ), SPRING, $GLOBALS['SYD'] );
	eq( array( 180.0, 180.0 ), array( $before->minutes(), $after->minutes() ) );
	eq( HOUR, $after->start - $before->end );
} );
function syd_slots( string $day ): array {
	$w = Clock::window_on( $day, '22:00', '06:00', $GLOBALS['SYD'] );
	return IntervalMath::generate_slots( array( $w ), HOUR, 900, Clock::clock_on( $day, '00:00', $GLOBALS['SYD'] ), EARLY, LATE );
}
test( 'spring forward offers no start in the hour that does not exist', function () {
	$slots = syd_slots( SPRING );
	eq( ( 7 - 1 ) * 4 + 1, count( $slots ) );
	ok( ! in_array( '02', array_map( static function ( $s ) {
		return substr( hhmm( $s->start, $GLOBALS['SYD'] ), 0, 2 );
	}, $slots ), true ), 'no 02:xx start' );
} );
test( 'autumn back offers both passes of the repeated hour', function () {
	$slots = syd_slots( AUTUMN );
	eq( ( 9 - 1 ) * 4 + 1, count( $slots ) );
	$twice = array_values( array_filter( $slots, static function ( $s ) {
		return '02:30' === hhmm( $s->start, $GLOBALS['SYD'] );
	} ) );
	eq( 2, count( $twice ) );
	eq( HOUR, $twice[1]->start - $twice[0]->start );
} );
test( 'the slot grid stays on the clock after the change', function () {
	eq( array( 0, 15, 30, 45 ), array_values( array_unique( array_map( static function ( $s ) {
		return (int) substr( hhmm( $s->start, $GLOBALS['SYD'] ), 3 );
	}, syd_slots( SPRING ) ) ) ) );
} );
test( 'a slot that starts before the jump and ends after it is a full hour long', function () {
	$straddle = array_values( array_filter( syd_slots( SPRING ), static function ( $s ) {
		return '01:30' === hhmm( $s->start, $GLOBALS['SYD'] );
	} ) );
	eq( 1, count( $straddle ) );
	eq( '03:30', hhmm( $straddle[0]->end, $GLOBALS['SYD'] ) );
} );
test( 'opening hours and a shift intersect in real time', function () {
	$both = IntervalMath::intersect( array( Clock::window_on( SPRING, '22:00', '06:00', $GLOBALS['SYD'] ) ), array( Clock::window_on( SPRING, '20:00', '04:00', $GLOBALS['SYD'] ) ) );
	eq( 1, count( $both ) );
	eq( 300.0, $both[0]->minutes() );
} );
test( 'a skipped clock time becomes the moment after the jump', function () {
	eq( '03:30', hhmm( Clock::clock_on( Clock::add_days( SPRING, 1 ), '02:30', $GLOBALS['SYD'] ), $GLOBALS['SYD'] ) );
} );
test( 'a repeated clock time is its first pass', function () {
	$t = Clock::clock_on( Clock::add_days( AUTUMN, 1 ), '02:30', $GLOBALS['SYD'] );
	eq( 11 * 3600, ( new DateTimeImmutable( '@' . $t ) )->setTimezone( $GLOBALS['SYD'] )->getOffset(), 'still on summer time (+11)' );
} );
test( 'a business day starts at midnight and a night shift may run to the end of the next', function () {
	foreach ( array( ORDINARY => 48, SPRING => 47, AUTUMN => 49 ) as $day => $hours ) {
		$r = req( $day, null, null, null, $GLOBALS['SYD'] );
		eq( '00:00', hhmm( $r->day_start(), $GLOBALS['SYD'] ) );
		eq( $hours * HOUR, $r->overnight_end() - $r->day_start(), $day );
	}
} );
test( 'the repeated hour can be booked once per pass and never twice', function () {
	$src = new MemSource();
	$src->add_staff( 1, '22:00', '06:00', null, null );
	$req   = req( AUTUMN, 1, null, null, $GLOBALS['SYD'] );
	$twice = array_values( array_filter( Availability::compute( $src, $req ), static function ( $s ) {
		return '02:30' === hhmm( $s->start, $GLOBALS['SYD'] );
	} ) );
	$a     = ( new StaffAllocator() )->allocate( $src, $req, $twice[1]->start ); // the second pass
	eq( $twice[1]->start, $a[0]->start );
	$src->book( 1, $a[0]->start, $a[0]->end );
	throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src, $req, $twice ) {
		( new StaffAllocator() )->allocate( $src, $req, $twice[1]->start );
	} );
	( new StaffAllocator() )->allocate( $src, $req, $twice[0]->start - HOUR ); // an hour before the first pass is still free
} );

// ------------------------------------------------------------------ policy merge

test( 'merged policy is the strictest', function () {
	$a = new Policy( 1, 'A', 'confirmed', 3600, 86400, 60 * 86400, 1800 );
	$b = new Policy( 2, 'B', 'pending', 7200, 3600, 30 * 86400, 3600 );
	$m = Policy::merge( array( $a, $b, $a ) );
	eq( array( 'pending', 7200, 86400, 30 * 86400, 1800 ), array( $m->default_status, $m->min_notice_booking, $m->min_notice_cancel, $m->booking_window, $m->pending_timeout ) );
	eq( array( 1, 2 ), $m->merged_from );
	ok( Policy::merge( array( $a, $a ) ) === $a, 'one distinct policy is returned as it is' );
	eq( $m->snapshot(), Policy::from_snapshot( $m->snapshot() )->snapshot(), 'snapshot round trip' );
} );

// ------------------------------------------------------------------ status flow and cancellation (test_customer_cancel.py)

function booking( string $status, int $start, int $cancel_notice_s = 86400 ): array {
	return array( 'status' => $status, 'start' => $start, 'policy_snapshot' => array( 'min_notice_cancel_s' => $cancel_notice_s ) );
}
test( 'cancelling after the notice period is refused', function () {
	$start = at( D, 10 );
	throws( EngineError::TOO_LATE_TO_CANCEL, function () use ( $start ) {
		StatusFlow::check( booking( 'confirmed', $start ), 'cancelled', 'staff', 'customer', $start - 86400 + 1 );
	} );
} );
test( 'the notice boundary is inclusive', function () {
	$start = at( D, 10 );
	StatusFlow::check( booking( 'confirmed', $start ), 'cancelled', 'staff', 'customer', $start - 86400 );
	ok( StatusFlow::can_cancel_online( booking( 'confirmed', $start ), $start - 86400 ), 'can_cancel_online agrees' );
} );
test( 'pending requests can be withdrawn freely but confirmed ones cannot', function () {
	$start = at( D, 10 );
	StatusFlow::check( booking( 'pending', $start ), 'cancelled', 'staff', 'customer', $start - 60 );
	ok( StatusFlow::can_cancel_online( booking( 'pending', $start ), $start - 60 ), 'pending' );
	ok( ! StatusFlow::can_cancel_online( booking( 'confirmed', $start ), $start - 60 ), 'confirmed, too late' );
} );
test( 'started or finished bookings cannot be cancelled online', function () {
	foreach ( array( 'completed', 'cancelled', 'no-show', 'rejected' ) as $status ) {
		throws( EngineError::INVALID_TRANSITION, function () use ( $status ) {
			StatusFlow::check( booking( $status, at( D, 10 ) ), 'cancelled', 'staff', 'customer', at( D, 1 ) );
		} );
	}
} );
test( 'customers can only cancel', function () {
	throws( EngineError::INVALID_TRANSITION, function () {
		StatusFlow::check( booking( 'pending', at( D, 10 ) ), 'confirmed', 'staff', 'customer', at( D, 1 ) );
	} );
} );
test( 'transitions follow the table', function () {
	$future = at( D, 10 );
	StatusFlow::check( booking( 'pending', $future ), 'confirmed', 'staff', 'staff', at( D, 1 ) );
	throws( EngineError::INVALID_TRANSITION, function () use ( $future ) {
		StatusFlow::check( booking( 'cancelled', $future ), 'confirmed', 'staff', 'staff', at( D, 1 ) );
	} );
	throws( EngineError::INVALID_TRANSITION, function () use ( $future ) {
		StatusFlow::check( booking( 'confirmed', $future ), 'in-progress', 'staff', 'staff', $future );
	} ); // staff modules do not use in-progress
	StatusFlow::check( booking( 'confirmed', $future ), 'in-progress', 'salon', 'staff', $future );
} );
test( 'no-show and completed wait for their time', function () {
	$start = at( D, 10 );
	throws( EngineError::INVALID_TRANSITION, function () use ( $start ) {
		StatusFlow::check( booking( 'confirmed', $start ), 'no-show', 'staff', 'staff', $start - 1 );
	} );
	StatusFlow::check( booking( 'confirmed', $start ), 'no-show', 'staff', 'staff', $start );
	throws( EngineError::INVALID_TRANSITION, function () use ( $start ) {
		StatusFlow::check( booking( 'confirmed', $start ), 'completed', 'staff', 'staff', $start - HOUR - 1 );
	} );
	StatusFlow::check( booking( 'confirmed', $start ), 'completed', 'staff', 'staff', $start - HOUR );
} );

// ------------------------------------------------------------------ salon (test_salon.py)

const CUT   = array( 'id' => 11, 'duration_minutes' => 45, 'buffer_minutes' => 0, 'is_addon' => false );
const COLOR = array( 'id' => 12, 'duration_minutes' => 90, 'buffer_minutes' => 0, 'is_addon' => false );
const GEL   = array( 'id' => 13, 'duration_minutes' => 30, 'buffer_minutes' => 0, 'is_addon' => true );
const TREAT = array( 'id' => 14, 'duration_minutes' => 20, 'buffer_minutes' => 5, 'is_addon' => true );

test( 'a cart has exactly one base service', function () {
	list( $base, $addons ) = SalonAllocator::split_cart( array( GEL, CUT, TREAT ) );
	eq( 11, $base['id'] );
	eq( array( 13, 14 ), array_column( $addons, 'id' ) );
	foreach ( array( array( GEL ), array( CUT, COLOR ), array() ) as $cart ) {
		$e = throws( EngineError::VALIDATION, function () use ( $cart ) {
			SalonAllocator::split_cart( $cart );
		} );
		ok( false !== strpos( $e->getMessage(), 'base service' ), 'message names the rule' );
	}
	throws( EngineError::VALIDATION, function () {
		( new SalonAllocator() )->find_slots( new MemSource(), req( D, null, null, null, null, array( GEL ) ) );
	} );
} );
test( 'add-ons extend the appointment', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$slots = ( new SalonAllocator() )->find_slots( $src, req( D, null, null, null, null, array( CUT, GEL ) ) );
	eq( 75 * 60, $slots[0]->end - $slots[0]->start, '45 + 30 minutes' );
	eq( '16:45', hhmm( end( $slots )->start ), '45 + 30 minutes must still end by 18:00' );
	$a = ( new SalonAllocator() )->allocate( $src, req( D, 1, null, null, null, array( CUT, GEL, TREAT ) ), at( D, 10 ) );
	eq( array( '10:00', '11:40' ), array( hhmm( $a[0]->start ), hhmm( $a[0]->end ) ), '45 + 30 + (20 + 5 buffer)' );
} );
test( 'stylist qualification follows the base service only', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->add_staff( 2 );
	$src->links[ CUT['id'] ] = array( 1 ); // Mei cuts
	$src->links[ GEL['id'] ] = array( 2 ); // only Lin is linked to the add-on: that must not narrow the choice
	$slots = ( new SalonAllocator() )->find_slots( $src, req( D, null, null, null, null, array( CUT, GEL ) ) );
	ok( $slots && array_reduce( $slots, static function ( $c, $s ) {
		return $c && array( 1 ) === $s->resource_ids;
	}, true ), 'every slot is Mei only' );
	eq( 1, ( new SalonAllocator() )->allocate( $src, req( D, null, null, null, null, array( CUT, GEL ) ), at( D, 10 ) )[0]->resource_id );
} );
test( 'with no stylist chosen the least loaded one is assigned', function () {
	$src = new MemSource();
	$src->add_staff( 1 );
	$src->add_staff( 2 );
	$src->book( 1, at( D, 10 ), at( D, 10, 45 ) );
	eq( 2, ( new SalonAllocator() )->allocate( $src, req( D, null, null, null, null, array( CUT, GEL ) ), at( D, 15 ) )[0]->resource_id );
} );
test( 'the allocator factory knows the types built so far', function () {
	ok( Allocators::for_type( 'salon' ) instanceof SalonAllocator, 'salon' );
	ok( Allocators::for_type( 'staff' ) instanceof StaffAllocator, 'staff' );
	ok( Allocators::for_type( 'restaurant' ) instanceof RestaurantAllocator, 'restaurant' );
	ok( Allocators::for_type( 'workshop' ) instanceof WorkshopAllocator, 'workshop' );
	ok( Allocators::for_type( 'field' ) instanceof FieldAllocator, 'field' );
	throws( EngineError::VALIDATION, function () {
		Allocators::for_type( 'spaceship' );
	} );
} );

// ------------------------------------------------------------------ restaurant (test_restaurant.py)

/** A table for the pure solver tests; the id is fixed by the name, like uuid5 in the original. */
function tbl( string $name, int $cap ): array {
	return array( 'id' => crc32( $name ), 'name' => $name, 'capacity' => $cap );
}
function tnames( ?array $tables ): ?array {
	if ( null === $tables ) {
		return null;
	}
	$n = array_column( $tables, 'name' );
	sort( $n );
	return $n;
}

test( 'smallest single table that fits', function () {
	$tables = array( tbl( 'a', 6 ), tbl( 'b', 2 ), tbl( 'c', 4 ), tbl( 'd', 2 ) );
	eq( array( 'b' ), tnames( RestaurantAllocator::solve_seating( $tables, 2, false ) ), '2-top, not the 4 or 6' );
	eq( array( 'c' ), tnames( RestaurantAllocator::solve_seating( $tables, 3, false ) ) );
	eq( array( 'a' ), tnames( RestaurantAllocator::solve_seating( $tables, 5, false ) ) );
} );
test( 'equal capacity is deterministic by name', function () {
	eq( array( 'b' ), tnames( RestaurantAllocator::solve_seating( array( tbl( 'z', 2 ), tbl( 'm', 2 ), tbl( 'b', 2 ) ), 2, false ) ) );
} );
test( 'no single fit and separate seating not allowed means none', function () {
	eq( null, RestaurantAllocator::solve_seating( array( tbl( 'a', 4 ), tbl( 'b', 4 ) ), 7, false ) );
} );
test( 'separate seating uses fewest tables', function () {
	eq( array( 'c', 'd' ), tnames( RestaurantAllocator::solve_seating( array( tbl( 'a', 2 ), tbl( 'b', 2 ), tbl( 'c', 4 ), tbl( 'd', 4 ) ), 7, true ) ), '4+4 beats 4+2+2' );
} );
test( 'separate seating tie prefers least spare capacity', function () {
	eq( array( 'a', 'c' ), tnames( RestaurantAllocator::solve_seating( array( tbl( 'a', 3 ), tbl( 'b', 3 ), tbl( 'c', 5 ), tbl( 'd', 5 ) ), 7, true ) ), '3+5=8 rather than 5+5=10' );
} );
test( 'single table always preferred over combining', function () {
	eq( array( 'c' ), tnames( RestaurantAllocator::solve_seating( array( tbl( 'a', 4 ), tbl( 'b', 4 ), tbl( 'c', 8 ) ), 8, true ) ) );
} );
test( 'separate seating impossible when total capacity too small', function () {
	eq( null, RestaurantAllocator::solve_seating( array( tbl( 'a', 2 ), tbl( 'b', 2 ) ), 5, true ) );
} );
test( 'combination size is bounded', function () {
	$ones = array();
	for ( $i = 0; $i < 7; $i++ ) {
		$ones[] = tbl( "t$i", 1 );
	}
	eq( null, RestaurantAllocator::solve_seating( $ones, 7, true, 6 ) );
	eq( 6, count( RestaurantAllocator::solve_seating( $ones, 6, true, 6 ) ?? array() ) );
} );
test( 'solver result is independent of input order', function () {
	$tables = array( tbl( 'a', 3 ), tbl( 'b', 3 ), tbl( 'c', 5 ), tbl( 'd', 5 ), tbl( 'e', 2 ) );
	eq( tnames( RestaurantAllocator::solve_seating( $tables, 7, true ) ), tnames( RestaurantAllocator::solve_seating( array_reverse( $tables ), 7, true ) ) );
} );
test( 'max concurrency counts overlap but not touching', function () {
	$win = iv( 18, 0, 21, 0 );
	eq( 1, RestaurantAllocator::max_concurrency( array( iv( 18, 0, 19, 0 ), iv( 19, 0, 20, 0 ) ), $win ), 'back to back' );
	eq( 3, RestaurantAllocator::max_concurrency( array( iv( 18, 0, 19, 30 ), iv( 19, 0, 20, 0 ), iv( 19, 15, 19, 45 ) ), $win ) );
	eq( 0, RestaurantAllocator::max_concurrency( array( iv( 10, 0, 12, 0 ) ), $win ), 'outside the window' );
	eq( 1, RestaurantAllocator::max_concurrency( array( iv( 17, 0, 18, 30 ), iv( 20, 30, 22, 0 ) ), $win ), 'clipped to the window' );
} );
function grid( string $mode, ?array $windows = null, string $first = '11:00', string $last = '20:30', int $interval = 30 ): array {
	$starts = RestaurantAllocator::candidate_starts( $mode, $windows ?? array( iv( 11, 0, 22, 0 ) ), D, $GLOBALS['KL'], 90 * 60, $interval * 60, $first, $last );
	return array_map( 'hhmm', $starts );
}
test( 'flexible mode runs from opening to closing minus duration', function () {
	$s = grid( 'flexible' );
	eq( array( '11:00', '20:30', 20 ), array( $s[0], end( $s ), count( $s ) ) );
} );
test( 'flexible mode anchors at opening time', function () {
	eq( array( '17:15', '17:45', '18:15' ), array_slice( grid( 'flexible', array( iv( 17, 15, 20, 0 ) ) ), 0, 3 ) );
} );
test( 'fixed mode uses first to last slot time', function () {
	eq( array( '12:00', '13:00', '14:00' ), grid( 'fixed', null, '12:00', '14:00', 60 ) );
} );
test( 'fixed mode drops starts that do not fit before closing', function () {
	eq( array( '12:00', '13:00' ), grid( 'fixed', array( iv( 11, 0, 14, 30 ) ), '12:00', '15:00', 60 ), '14:00 + 90 min would run past 14:30' );
} );
test( 'fixed slots can run past midnight', function () {
	$late = Clock::window_on( D, '18:00', '02:00', $GLOBALS['KL'] );
	$s    = RestaurantAllocator::candidate_starts( 'fixed', array( $late ), D, $GLOBALS['KL'], 60 * 60, 3600, '22:00', '01:00' );
	eq( array( '22:00', '23:00', '00:00', '01:00' ), array_map( 'hhmm', $s ) );
	eq( Clock::add_days( D, 1 ), Clock::local_date( end( $s ), $GLOBALS['KL'] ), 'the last seating is on the next date' );
} );

test( 'the minimum spend rule', function () {
	$cases = array( // spend, accept, accepted, ok (test_minimum_spend_rule)
		array( '0', false, null, true ), array( '0', false, '0', true ),
		array( '300', false, null, false ), array( '300', true, null, true ),
		array( '300', false, '300', true ), array( '300', false, '299.99', false ), array( '200', false, '500', true ),
		array( '300', true, '299', false ),
	);
	foreach ( $cases as $k ) {
		list( $spend, $accept, $accepted, $want ) = $k;
		$c = null === $accepted ? null : MinimumSpend::to_cents( $accepted );
		try {
			MinimumSpend::ensure_accepted( MinimumSpend::to_cents( $spend ), $accept, $c );
			eq( true, $want, "$spend/$accepted accepted" );
		} catch ( EngineError $e ) {
			eq( false, $want, "$spend/$accepted refused" );
			eq( EngineError::MINIMUM_SPEND, $e->error_code );
			eq( MinimumSpend::format( MinimumSpend::to_cents( $spend ) ), $e->data['minimum_spend'] );
			eq( null === $accepted ? null : MinimumSpend::format( $c ), $e->data['accepted_minimum_spend'] ?? null, 'the accepted amount is echoed only when sent' );
		}
	}
} );
test( 'amounts are parsed strictly', function () {
	eq( array( 30000, 30050, 30050, 0, 1999 ), array( MinimumSpend::to_cents( '300' ), MinimumSpend::to_cents( '300.5' ), MinimumSpend::to_cents( 300.5 ), MinimumSpend::to_cents( 0 ), MinimumSpend::to_cents( '19.99' ) ) );
	foreach ( array( '-1', 'abc', true, '1.234', '', array() ) as $bad ) {
		eq( null, MinimumSpend::to_cents( $bad ), var_export( $bad, true ) );
	}
} );

/** test_restaurant.py's restaurant: 11:00-22:00 daily, 90 min seatings every 30 min + 15 min buffer, 2-tops a2 b2, c4, d6, VIP v4 ($300) and v2 ($200). */
function restaurant(): MemSource {
	$src = new MemSource();
	for ( $d = 0; $d < 7; $d++ ) {
		$src->hours[] = array( 'weekday' => $d, 'open_time' => '11:00', 'close_time' => '22:00' );
	}
	$src->seating = array( 'booking_mode' => 'flexible', 'slot_duration_minutes' => 90, 'slot_interval_minutes' => 30, 'first_slot_time' => '11:00', 'last_slot_time' => '20:30', 'buffer_minutes' => 15, 'allow_table_selection' => false, 'policy_id' => null );
	$id = 100;
	foreach ( array( array( 'a2', 2, false, 0 ), array( 'b2', 2, false, 0 ), array( 'c4', 4, false, 0 ), array( 'd6', 6, false, 0 ), array( 'v4', 4, true, 30000 ), array( 'v2', 2, true, 20000 ) ) as $t ) {
		$src->tables[] = array( 'id' => ++$id, 'name' => $t[0], 'capacity' => $t[1], 'status' => 'active', 'is_vip' => $t[2], 'minimum_spend' => $t[3] );
	}
	return $src;
}
function tid( MemSource $src, string $name ): int {
	foreach ( $src->tables as $t ) {
		if ( $t['name'] === $name ) {
			return $t['id'];
		}
	}
	throw new Exception( "no table $name" );
}
function party( int $size, string $type = 'normal', bool $separate = false, ?int $table = null ): AvailabilityRequest {
	$r                         = req( D, $table, null, null, null, array() );
	$r->party_size             = $size;
	$r->table_type             = $type;
	$r->allow_separate_seating = $separate;
	return $r;
}
function seat( MemSource $src, AvailabilityRequest $r, int $h, int $m = 0 ): array {
	$a = ( new RestaurantAllocator() )->allocate( $src, $r, at( D, $h, $m ) );
	foreach ( $a as $x ) {
		$src->book( $x->resource_id, $x->start, $x->end );
	}
	return $a;
}
function table_names( MemSource $src, array $ids ): array {
	$n = array_map( static function ( $id ) use ( $src ) {
		foreach ( $src->tables as $t ) {
			if ( $t['id'] === $id ) {
				return $t['name'];
			}
		}
	}, $ids );
	sort( $n );
	return $n;
}

test( 'a party gets the smallest fitting normal table', function () {
	$src = restaurant();
	foreach ( array( 2 => 'a2', 3 => 'c4', 5 => 'd6' ) as $size => $name ) {
		$ids = array();
		foreach ( ( new RestaurantAllocator() )->find_slots( $src, party( $size ) ) as $s ) {
			$ids = array_merge( $ids, $s->resource_ids );
		}
		eq( array( $name ), array_values( array_unique( table_names( $src, $ids ) ) ), "party of $size" );
	}
} );
test( 'the slot grid and occupancy follow the seating setting', function () {
	$s = ( new RestaurantAllocator() )->find_slots( restaurant(), party( 2 ) );
	eq( array( '11:00', '20:30' ), array( hhmm( $s[0]->start ), hhmm( end( $s )->start ) ), 'closing 22:00 - 90 min' );
	eq( 105 * 60, $s[0]->end - $s[0]->start, '90 duration + 15 buffer' );
	$arr = $s[0]->to_array( $GLOBALS['KL'] );
	ok( isset( $arr['table_ids'] ) && false === $arr['combined'] && null === $arr['minimum_spend'] && ! isset( $arr['staff_ids'] ), 'slot names tables, not staff' );
} );
test( 'separate seating combines the fewest tables only when allowed', function () {
	$src = restaurant();
	eq( array(), ( new RestaurantAllocator() )->find_slots( $src, party( 8 ) ), 'no single table seats 8' );
	$combos = ( new RestaurantAllocator() )->find_slots( $src, party( 8, 'normal', true ) );
	eq( array( 'a2', 'd6' ), table_names( $src, $combos[0]->resource_ids ), '6 + 2' );
	ok( true === $combos[0]->to_array( $GLOBALS['KL'] )['combined'], 'marked combined' );
	throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src ) {
		seat( $src, party( 8 ), 19 );
	} );
	eq( array( 'a2', 'd6' ), table_names( $src, array_column( array_map( 'get_object_vars', seat( $src, party( 8, 'normal', true ), 19 ) ), 'resource_id' ) ) );
} );
test( 'vip and normal pools are never mixed', function () {
	$src = restaurant();
	$vip = array( tid( $src, 'v4' ), tid( $src, 'v2' ) );
	foreach ( ( new RestaurantAllocator() )->find_slots( $src, party( 2 ) ) as $s ) {
		ok( ! array_intersect( $s->resource_ids, $vip ), 'normal party never offered a VIP table' );
	}
	eq( array(), ( new RestaurantAllocator() )->find_slots( $src, party( 5, 'vip' ) ), 'VIP alone cannot seat 5 singly, and must not borrow the 6-top' );
	$combo = ( new RestaurantAllocator() )->find_slots( $src, party( 5, 'vip', true ) );
	eq( array( 'v2', 'v4' ), table_names( $src, $combo[0]->resource_ids ) );
	eq( 50000, $combo[0]->minimum_spend, 'combined VIP minimum spend is the sum' );
	eq( array(), ( new RestaurantAllocator() )->find_slots( $src, party( 15, 'normal', true ) ), 'normal tables seat 14; VIP must not top them up' );
	ok( count( ( new RestaurantAllocator() )->find_slots( $src, party( 12, 'normal', true ) ) ) > 0, '6 + 4 + 2 within the normal pool' );
} );
test( 'tables fill smallest first then overflow upward', function () {
	$src = restaurant();
	$got = array();
	for ( $i = 0; $i < 4; $i++ ) {
		$got[] = table_names( $src, array( seat( $src, party( 2 ), 19 )[0]->resource_id ) )[0];
	}
	eq( array( 'a2', 'b2', 'c4', 'd6' ), $got );
	throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src ) {
		seat( $src, party( 2 ), 19 );
	} );
} );
test( 'occupancy blocks the table but the next free grid start is fine', function () {
	$src = restaurant();
	seat( $src, party( 6 ), 18 ); // 18:00-19:45
	foreach ( array( array( 19, 0 ), array( 19, 30 ), array( 19, 45 ) ) as $hm ) {
		throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src, $hm ) {
			seat( $src, party( 6 ), $hm[0], $hm[1] );
		} );
	}
	seat( $src, party( 6 ), 20 );
} );
test( 'the open table rule caps tables in use at the same time', function () {
	$src          = restaurant();
	$src->rules[] = array( 'table_type' => 'normal', 'weekday' => Clock::weekday_index( D ), 'specific_date' => null, 'open_tables' => 2 );
	seat( $src, party( 2 ), 12 ); // 12:00-13:45
	seat( $src, party( 2 ), 12 );
	throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src ) {
		seat( $src, party( 2 ), 12 );
	} ); // 4 normal tables exist, only 2 open
	throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src ) {
		seat( $src, party( 2 ), 13 );
	} ); // still overlaps both
	seat( $src, party( 2 ), 14 ); // after both have finished
} );
test( 'a date rule overrides the weekday rule, and 0 closes the day', function () {
	$src   = restaurant();
	$wd    = Clock::weekday_index( D );
	$src->rules = array(
		array( 'table_type' => 'normal', 'weekday' => $wd, 'specific_date' => null, 'open_tables' => 4 ),
		array( 'table_type' => 'normal', 'weekday' => null, 'specific_date' => D, 'open_tables' => 1 ),
	);
	seat( $src, party( 2 ), 19 );
	throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src ) {
		seat( $src, party( 2 ), 19 );
	} );
	$src->rules[1]['open_tables'] = 0;
	eq( array(), ( new RestaurantAllocator() )->find_slots( $src, party( 2 ) ) );
	eq( true, count( ( new RestaurantAllocator() )->find_slots( $src, party( 2, 'vip' ) ) ) > 0, 'a normal-table rule leaves VIP tables alone' );
} );
test( 'closed days, holidays and table leave', function () {
	$src        = restaurant();
	$src->hours = array_values( array_filter( $src->hours, static function ( $h ) {
		return $h['weekday'] !== Clock::weekday_index( D );
	} ) );
	eq( array(), ( new RestaurantAllocator() )->find_slots( $src, party( 2 ) ), 'not an opening day' );
	$src->hours = array();
	eq( array(), ( new RestaurantAllocator() )->find_slots( $src, party( 2 ) ), 'no opening hours at all: closed' );
	$src             = restaurant();
	$src->days_off[] = array( 'resource_id' => tid( $src, 'd6' ), 'start_date' => D, 'end_date' => D, 'repeat_yearly' => 0 );
	eq( array(), ( new RestaurantAllocator() )->find_slots( $src, party( 5 ) ), 'the only 5-seat table is out of service' );
	$src->days_off[] = array( 'resource_id' => null, 'start_date' => D, 'end_date' => D, 'repeat_yearly' => 0 );
	eq( array(), ( new RestaurantAllocator() )->find_slots( $src, party( 2 ) ), 'holiday' );
} );
test( 'input validation and missing configuration', function () {
	$src = restaurant();
	throws( EngineError::VALIDATION, function () use ( $src ) {
		( new RestaurantAllocator() )->find_slots( $src, party( 0 ) );
	} );
	throws( EngineError::VALIDATION, function () use ( $src ) {
		( new RestaurantAllocator() )->find_slots( $src, party( 2, 'terrace' ) );
	} );
	$src->seating = null;
	throws( EngineError::VALIDATION, function () use ( $src ) {
		( new RestaurantAllocator() )->find_slots( $src, party( 2 ) );
	} );
} );
test( 'choosing a table follows the setting and its rules', function () {
	$src = restaurant();
	throws( EngineError::VALIDATION, function () use ( $src ) {
		( new RestaurantAllocator() )->find_slots( $src, party( 2, 'normal', false, tid( $src, 'c4' ) ) );
	} ); // off by default: refused, not ignored
	$src->seating['allow_table_selection'] = true;
	$a                                     = seat( $src, party( 2, 'normal', false, tid( $src, 'c4' ) ), 19 );
	eq( tid( $src, 'c4' ), $a[0]->resource_id, 'the customer gets the table they chose' );
	throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src ) {
		seat( $src, party( 2, 'normal', false, tid( $src, 'c4' ) ), 19 );
	} ); // taken
	seat( $src, party( 2 ), 19 ); // others stay bookable
	eq( array(), ( new RestaurantAllocator() )->find_slots( $src, party( 5, 'normal', false, tid( $src, 'c4' ) ) ), 'too small' );
	throws( EngineError::VALIDATION, function () use ( $src ) {
		( new RestaurantAllocator() )->find_slots( $src, party( 2, 'normal', false, tid( $src, 'v2' ) ) );
	} ); // other class
	throws( EngineError::NOT_FOUND, function () use ( $src ) {
		( new RestaurantAllocator() )->find_slots( $src, party( 2, 'normal', false, 999 ) );
	} );
	throws( EngineError::VALIDATION, function () use ( $src ) {
		( new RestaurantAllocator() )->find_slots( $src, party( 2, 'normal', true, tid( $src, 'd6' ) ) );
	} ); // never combined
	$src->days_off[] = array( 'resource_id' => tid( $src, 'd6' ), 'start_date' => D, 'end_date' => D, 'repeat_yearly' => 0 );
	eq( array(), ( new RestaurantAllocator() )->find_slots( $src, party( 2, 'normal', false, tid( $src, 'd6' ) ) ), 'chosen table out of service' );
} );
test( 'an after midnight seating is booked on the business day before', function () {
	$src        = restaurant();
	$src->hours = array();
	for ( $d = 0; $d < 7; $d++ ) {
		$src->hours[] = array( 'weekday' => $d, 'open_time' => '18:00', 'close_time' => '02:00' );
	}
	$src->seating = array_merge( $src->seating, array( 'booking_mode' => 'fixed', 'slot_duration_minutes' => 60, 'slot_interval_minutes' => 60, 'first_slot_time' => '19:00', 'last_slot_time' => '01:00', 'buffer_minutes' => 0 ) );
	$start        = at( Clock::add_days( D, 1 ), 0, 0 );
	$r            = party( 2 );
	$r->day       = Clock::add_days( D, 1 );
	list( $a, $day ) = Availability::allocate_on_business_day( new RestaurantAllocator(), $src, $r, $start );
	eq( D, $day );
	eq( $start, $a[0]->start );
} );

// ------------------------------------------------------------------ workshop (test_automotive.py)

const OIL   = array( 'id' => 21, 'duration_minutes' => 60, 'buffer_minutes' => 0 );
const BRAKE = array( 'id' => 22, 'duration_minutes' => 120, 'buffer_minutes' => 0 );

/** test_automotive.py's garage: open 08:00-18:00; mechanics Ali (1) and Bala (2) 09:00-18:00 with a 13:00-14:00 break; Bay 1 (11) and Bay 2 (12). */
function garage( array $mechanics = array( 1 => 'Ali', 2 => 'Bala' ), array $bays = array( 11 => 'Bay 1', 12 => 'Bay 2' ) ): MemSource {
	$src = new MemSource();
	for ( $d = 0; $d < 7; $d++ ) {
		$src->hours[] = array( 'weekday' => $d, 'open_time' => '08:00', 'close_time' => '18:00' );
	}
	foreach ( $mechanics as $id => $name ) {
		$src->add_staff( $id );
		$src->names[ $id ] = $name;
	}
	foreach ( $bays as $id => $name ) {
		$src->bays[ $id ]  = true;
		$src->names[ $id ] = $name;
	}
	return $src;
}
function job( array $services, ?int $mechanic = null ): AvailabilityRequest {
	return req( D, $mechanic, null, null, null, $services );
}
/** Books the job at h:m into the source and returns [mechanic name, bay name, start, end] per segment. */
function book_job( MemSource $src, array $services, int $h, int $m = 0, ?int $mechanic = null ): array {
	$a   = ( new WorkshopAllocator() )->allocate( $src, job( $services, $mechanic ), at( D, $h, $m ) );
	$out = array();
	for ( $i = 0; $i < count( $a ); $i += 2 ) {
		$src->book( $a[ $i ]->resource_id, $a[ $i ]->start, $a[ $i ]->end );
		$src->book( $a[ $i + 1 ]->resource_id, $a[ $i + 1 ]->start, $a[ $i + 1 ]->end );
		$out[] = array( $src->names[ $a[ $i ]->resource_id ], $src->names[ $a[ $i + 1 ]->resource_id ], hhmm( $a[ $i ]->start ), hhmm( $a[ $i ]->end ) );
	}
	return $out;
}
function job_starts( MemSource $src, array $services, ?int $mechanic = null ): array {
	return starts( ( new WorkshopAllocator() )->find_slots( $src, job( $services, $mechanic ) ) );
}

test( 'segments are contiguous and include buffers', function () {
	$segs = WorkshopAllocator::plan_segments( at( D, 9 ), array( OIL, array( 'id' => 9, 'duration_minutes' => 120, 'buffer_minutes' => 15 ), array( 'id' => 8, 'duration_minutes' => 30, 'buffer_minutes' => 0 ) ) );
	eq( array( '09:00-10:00', '10:00-12:15', '12:15-12:45' ), array_map( static function ( $s ) {
		return hhmm( $s[1]->start ) . '-' . hhmm( $s[1]->end );
	}, $segs ) );
} );
test( 'fits needs the whole segment inside one free interval', function () {
	$free = array( iv( 9, 0, 13, 0 ), iv( 14, 0, 18, 0 ) );
	ok( IntervalMath::fits( $free, iv( 12, 0, 13, 0 ) ), 'ends exactly at the break' );
	ok( ! IntervalMath::fits( $free, iv( 12, 30, 13, 30 ) ), 'runs into the break' );
	ok( ! IntervalMath::fits( $free, iv( 8, 0, 9, 30 ) ), 'starts before the window' );
	ok( ! IntervalMath::fits( array(), iv( 9, 0, 10, 0 ) ), 'nothing free' );
} );
test( 'choose resource prefers continuity then load then name', function () {
	$names = array( 1 => 'Ali', 2 => 'Bala', 3 => 'Chen' );
	$load  = array( 1 => 120.0, 2 => 30.0, 3 => 30.0 );
	eq( 1, WorkshopAllocator::choose_resource( array( 1, 2, 3 ), 1, $load, $names ), 'same person as the previous segment' );
	eq( 2, WorkshopAllocator::choose_resource( array( 2, 3 ), 1, $load, $names ), 'previous unavailable: least loaded, then name' );
	eq( 1, WorkshopAllocator::choose_resource( array( 3, 1 ), null, array(), $names ), 'equal load: name' );
} );
test( 'a slot needs both a mechanic and a bay', function () {
	eq( array(), job_starts( garage( array( 1 => 'Ali' ), array() ), array( OIL ) ), 'no bays' );
	eq( array(), job_starts( garage( array(), array( 11 => 'Bay 1' ) ), array( OIL ) ), 'no mechanics' );
	ok( count( job_starts( garage( array( 1 => 'Ali' ), array( 11 => 'Bay 1' ) ), array( OIL ) ) ) > 0, 'both' );
} );
test( 'a booking reserves one mechanic row and one bay row per segment', function () {
	$a = ( new WorkshopAllocator() )->allocate( garage(), job( array( OIL ) ), at( D, 10 ) );
	eq( 2, count( $a ) );
	eq( array( 1, 11 ), array( $a[0]->resource_id, $a[1]->resource_id ), 'Ali (least loaded, then name) and Bay 1' );
	eq( array( OIL['id'], OIL['id'] ), array( $a[0]->service_id, $a[1]->service_id ), 'both rows tagged with the service' );
	$slot = ( new WorkshopAllocator() )->find_slots( garage(), job( array( OIL ) ) )[0]->to_array( $GLOBALS['KL'] );
	ok( isset( $slot['staff_ids'], $slot['bay_ids'] ), 'a job slot names mechanics and bays' );
} );
test( 'the bay is the bottleneck when mechanics outnumber bays', function () {
	$src = garage( array( 1 => 'Ali', 2 => 'Bala' ), array( 11 => 'Bay 1' ) );
	book_job( $src, array( OIL ), 10 );
	throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src ) {
		book_job( $src, array( OIL ), 10 );
	} );
	ok( ! in_array( '10:00', job_starts( $src, array( OIL ) ), true ), '10:00 no longer offered' );
} );
test( 'the mechanic is the bottleneck when bays outnumber mechanics', function () {
	$src = garage( array( 1 => 'Ali' ), array( 11 => 'Bay 1', 12 => 'Bay 2' ) );
	book_job( $src, array( OIL ), 10 );
	throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src ) {
		book_job( $src, array( OIL ), 10 );
	} );
} );
test( 'service qualifications restrict mechanic and bay separately', function () {
	$src                     = garage();
	$src->links[ OIL['id'] ]   = array( 2, 12 ); // Bala, Bay 2
	$src->links[ BRAKE['id'] ] = array( 1, 11 ); // Ali, Bay 1
	eq( array( array( 'Bala', 'Bay 2', '10:00', '11:00' ) ), book_job( $src, array( OIL ), 10 ) );
	eq( array( array( 'Ali', 'Bay 1', '10:00', '12:00' ) ), book_job( $src, array( BRAKE ), 10 ) );
} );
test( 'a service linked only to a bay does not disqualify the mechanics', function () {
	$src                   = garage();
	$src->links[ OIL['id'] ] = array( 12 );
	eq( 'Bay 2', book_job( $src, array( OIL ), 10 )[0][1] );
} );
test( 'mechanic breaks apply but bays have none', function () {
	$s = job_starts( garage(), array( OIL ) );
	eq( array( '09:00', '17:00' ), array( $s[0], end( $s ) ) );
	ok( in_array( '12:00', $s, true ) && in_array( '14:00', $s, true ), 'either side of the break' );
	ok( ! array_intersect( array( '12:15', '13:00', '13:45' ), $s ), 'nothing running into or inside the break' );
} );
test( 'services are stacked back to back with two rows per segment', function () {
	eq( array( array( 'Ali', 'Bay 1', '09:00', '10:00' ), array( 'Ali', 'Bay 1', '10:00', '12:00' ) ), book_job( garage(), array( OIL, BRAKE ), 9 ), 'and the same mechanic and bay are kept' );
} );
test( 'segments block resources one at a time', function () {
	$src                     = garage();
	$src->links[ OIL['id'] ]   = array( 1 ); // Ali does oil
	$src->links[ BRAKE['id'] ] = array( 2 ); // Bala does brakes
	eq( array( 'Ali', 'Bala' ), array_column( book_job( $src, array( OIL, BRAKE ), 9 ), 0 ) );
	book_job( $src, array( OIL ), 10 ); // Ali is free again from 10:00
	throws( EngineError::SLOT_UNAVAILABLE, function () use ( $src ) {
		book_job( $src, array( OIL, BRAKE ), 9 );
	} ); // the second stacked job needs Bala from 10:00
} );
test( 'a segment running into the break makes the start unavailable', function () {
	$s = job_starts( garage(), array( OIL, BRAKE ) );
	ok( in_array( '10:00', $s, true ), '10-11 oil, 11-13 brake ends exactly at the break' );
	ok( ! in_array( '10:15', $s, true ) && ! in_array( '12:00', $s, true ), 'the brake segment would cross 13:00-14:00' );
	ok( in_array( '15:00', $s, true ) && ! in_array( '15:15', $s, true ), 'a 3-hour job must end by 18:00' );
} );
test( 'buffer time is part of each segment', function () {
	$align = array( 'id' => 23, 'duration_minutes' => 60, 'buffer_minutes' => 15 );
	eq( array( '10:00', '11:15' ), array_slice( book_job( garage(), array( OIL, $align ), 9 )[1], 2 ) );
} );
test( 'a named mechanic must be qualified for and free during every segment', function () {
	$src                   = garage();
	$src->links[ OIL['id'] ] = array( 1 ); // only Ali does oil; anyone does brakes
	ok( count( job_starts( $src, array( OIL, BRAKE ), 1 ) ) > 0, 'Ali can do the whole job' );
	eq( array(), job_starts( $src, array( OIL, BRAKE ), 2 ), 'Bala cannot do the oil segment' );
	eq( array( 'Ali', 'Ali' ), array_column( book_job( $src, array( OIL, BRAKE ), 9, 0, 1 ), 0 ) );
	throws( EngineError::NOT_FOUND, function () use ( $src ) {
		job_starts( $src, array( OIL ), 999 );
	} );
	throws( EngineError::NOT_FOUND, function () use ( $src ) {
		job_starts( $src, array( OIL ), 11 );
	} ); // a bay is not a mechanic
} );
test( 'a workshop with no opening hours is open all day (shifts decide)', function () {
	$src        = garage();
	$src->hours = array();
	$s          = job_starts( $src, array( OIL ) );
	eq( array( '09:00', '17:00' ), array( $s[0], end( $s ) ) );
} );
test( 'a job needs at least one service', function () {
	throws( EngineError::VALIDATION, function () {
		job_starts( garage(), array() );
	} );
} );

// ------------------------------------------------------------------ field service (test_field_service.py)

const KL_PIN   = array( 3.1390, 101.6869 ); // the business
const NEAR     = array( 3.1200, 101.6700 ); // ~3 km away
const SUBANG   = array( 3.0738, 101.5183 ); // ~20 km away
const SEREMBAN = array( 2.7297, 101.9381 ); // ~55 km away
const SITE     = array( 'id' => 31, 'duration_minutes' => 60, 'buffer_minutes' => 0, 'location_type' => 'on_site', 'travel_buffer_minutes' => 30, 'service_category' => 'inspection' );
const OFFICE   = array( 'id' => 32, 'duration_minutes' => 60, 'buffer_minutes' => 0, 'location_type' => 'in_office', 'travel_buffer_minutes' => 30, 'service_category' => 'inspection' );
const VIDEO    = array( 'id' => 33, 'duration_minutes' => 30, 'buffer_minutes' => 0, 'location_type' => 'virtual', 'travel_buffer_minutes' => 30, 'service_category' => 'inspection' );

/** test_field_service.py's practice: open 08:00-18:00, Dr Lim (1) 09:00-18:00 with a 13:00-14:00 break. */
function practice( array $pros = array( 1 => 'Dr Lim' ) ): MemSource {
	$src = new MemSource();
	for ( $d = 0; $d < 7; $d++ ) {
		$src->hours[] = array( 'weekday' => $d, 'open_time' => '08:00', 'close_time' => '18:00' );
	}
	foreach ( $pros as $id => $name ) {
		$src->add_staff( $id );
		$src->names[ $id ] = $name;
	}
	return $src;
}
function visit( array $svc, ?array $pin = null, ?int $pro = null, float $radius = 25.0 ): AvailabilityRequest {
	$r                    = req( D, $pro, null, null, null, array( $svc ) );
	$r->latitude          = $pin ? $pin[0] : null;
	$r->longitude         = $pin ? $pin[1] : null;
	$r->business_location = array( 'latitude' => KL_PIN[0], 'longitude' => KL_PIN[1], 'service_radius_km' => $radius );
	return $r;
}
function visit_starts( MemSource $src, AvailabilityRequest $r ): array {
	return starts( ( new FieldAllocator() )->find_slots( $src, $r ) );
}
function book_visit( MemSource $src, AvailabilityRequest $r, int $h, int $m = 0 ) {
	$a = ( new FieldAllocator() )->allocate( $src, $r, at( D, $h, $m ) )[0];
	$src->book( $a->resource_id, $a->start, $a->end );
	return $a;
}

test( 'only on-site visits reserve travel time', function () {
	eq( array( 1800, 0, 0, 0 ), array( FieldAllocator::travel_pad( 'on_site', 30 ), FieldAllocator::travel_pad( 'in_office', 30 ), FieldAllocator::travel_pad( 'virtual', 30 ), FieldAllocator::travel_pad( 'on_site', 0 ) ) );
} );
test( 'distances on the ground', function () {
	$subang = Geo::distance_km( KL_PIN[0], KL_PIN[1], SUBANG[0], SUBANG[1] );
	ok( $subang > 19.5 && $subang < 20.6, "Kuala Lumpur to Subang ~20 km (the engine test's range), got $subang" );
	$near = Geo::distance_km( KL_PIN[0], KL_PIN[1], NEAR[0], NEAR[1] );
	ok( $near > 2.5 && $near < 3.5, "NEAR ~3 km, got $near" );
	ok( Geo::distance_km( KL_PIN[0], KL_PIN[1], SEREMBAN[0], SEREMBAN[1] ) > 50, 'Seremban is over 50 km' );
	eq( 0.0, Geo::distance_km( 3.1, 101.6, 3.1, 101.6 ), 'the same point' );
	$across = Geo::distance_km( 0.0, 179.5, 0.0, -179.5 );
	ok( abs( $across - 111.32 ) < 0.1, "one degree of longitude across the antimeridian on the equator, got $across" );
	ok( abs( $subang - Geo::haversine_km( KL_PIN[0], KL_PIN[1], SUBANG[0], SUBANG[1] ) ) / $subang < 0.005, 'ellipsoid and sphere agree within 0.5%' );
	ok( Geo::distance_km( 40.0, 0.0, -40.0, 179.9 ) > 19000, 'nearly antipodal points still give a distance' );
} );
test( 'the radius boundary is exact', function () {
	$km = Geo::distance_km( KL_PIN[0], KL_PIN[1], SUBANG[0], SUBANG[1] );
	ok( count( visit_starts( practice(), visit( SITE, SUBANG, null, $km + 0.01 ) ) ) > 0, 'a hair inside' );
	throws( EngineError::OUTSIDE_AREA, function () use ( $km ) {
		visit_starts( practice(), visit( SITE, SUBANG, null, $km - 0.01 ) );
	} );
} );
test( 'on-site availability needs coordinates and offers slots inside the radius', function () {
	$e = throws( EngineError::VALIDATION, function () {
		visit_starts( practice(), visit( SITE ) );
	} );
	eq( 'latitude', $e->data['field'] );
	ok( count( visit_starts( practice(), visit( SITE, NEAR ) ) ) > 0, 'near' );
	ok( count( visit_starts( practice(), visit( SITE, SUBANG ) ) ) > 0, '~20 km is inside 25 km' );
	$far = throws( EngineError::OUTSIDE_AREA, function () {
		visit_starts( practice(), visit( SITE, SEREMBAN ) );
	} );
	ok( 25.0 === $far->data['service_radius_km'] && $far->data['distance_km'] > 50, 'the error says how far and how far is allowed' );
} );
test( 'the business location must be set for on-site visits, never for office or online', function () {
	$r                    = visit( SITE, NEAR );
	$r->business_location = array( 'latitude' => null, 'longitude' => null, 'service_radius_km' => null );
	throws( EngineError::VALIDATION, function () use ( $r ) {
		visit_starts( practice(), $r );
	} );
	$o                    = visit( OFFICE );
	$o->business_location = null;
	ok( count( visit_starts( practice(), $o ) ) > 0, 'in office needs no location' );
} );
test( 'on-site visits block travel time before and after', function () {
	$s = visit_starts( practice(), visit( SITE, NEAR ) );
	eq( array( '09:30', '16:30' ), array( $s[0], end( $s ) ), 'the 30-minute drive must also fit the 09:00-18:00 shift' );
	$o = visit_starts( practice(), visit( OFFICE ) );
	eq( array( '09:00', '17:00' ), array( $o[0], end( $o ) ), 'no travel in the office' );
	$a = ( new FieldAllocator() )->allocate( practice(), visit( SITE, NEAR ), at( D, 10 ) )[0];
	eq( array( '09:30', '11:30', '10:00', '11:00' ), array( hhmm( $a->start ), hhmm( $a->end ), hhmm( $a->service_start() ), hhmm( $a->service_end() ) ), 'allocation padded, visit not' );
} );
test( 'travel buffers shape what can be booked next', function () {
	$src = practice();
	book_visit( $src, visit( SITE, NEAR ), 10 ); // holds 09:30-11:30
	$s = visit_starts( $src, visit( SITE, NEAR ) );
	ok( ! in_array( '11:30', $s, true ) && ! in_array( '12:00', $s, true ), 'drive time would overlap, or run into lunch' );
	eq( '14:30', $s[0], 'the first visit whose padded block fits after lunch' );
	$o = visit_starts( $src, visit( OFFICE ) );
	ok( in_array( '11:30', $o, true ) && ! in_array( '11:15', $o, true ), 'an office consult only waits for the drive back' );
} );
test( 'online appointments carry no travel time', function () {
	$a = ( new FieldAllocator() )->allocate( practice(), visit( VIDEO ), at( D, 10 ) )[0];
	eq( array( 0, 0, '10:00', '10:30' ), array( $a->pad_before, $a->pad_after, hhmm( $a->start ), hhmm( $a->end ) ) );
} );
test( 'a field-service booking takes exactly one service', function () {
	throws( EngineError::VALIDATION, function () {
		( new FieldAllocator() )->find_slots( practice(), req( D, null, null, null, null, array( OFFICE, VIDEO ) ) );
	} );
} );
test( 'auto-assignment prefers the less loaded professional and respects qualification', function () {
	$src = practice( array( 1 => 'Ali', 2 => 'Bala' ) );
	eq( 1, book_visit( $src, visit( OFFICE ), 10 )->resource_id, 'equal load: name' );
	eq( 2, book_visit( $src, visit( OFFICE ), 15 )->resource_id, 'the second goes to whoever is less loaded' );
	$src->links[ VIDEO['id'] ] = array( 2 );
	eq( 2, book_visit( $src, visit( VIDEO ), 11 )->resource_id, 'only Bala does video calls' );
} );
test( 'a named professional must be one', function () {
	eq( 1, ( new FieldAllocator() )->allocate( practice(), visit( OFFICE, null, 1 ), at( D, 10 ) )[0]->resource_id );
	throws( EngineError::NOT_FOUND, function () {
		visit_starts( practice(), visit( OFFICE, null, 999 ) );
	} );
} );
test( 'a practice with no opening hours is open all day (shifts decide)', function () {
	$src        = practice();
	$src->hours = array();
	$s          = visit_starts( $src, visit( OFFICE ) );
	eq( array( '09:00', '17:00' ), array( $s[0], end( $s ) ) );
} );

// ------------------------------------------------------------------ run

$failed = 0;
foreach ( $GLOBALS['jsb_tests'] as $name => $fn ) {
	try {
		$fn();
		echo "  ok   $name\n";
	} catch ( Throwable $e ) {
		$failed++;
		echo "  FAIL $name\n       " . $e->getMessage() . ' (line ' . $e->getLine() . ")\n";
	}
}
$total = count( $GLOBALS['jsb_tests'] );
echo "\n" . ( $total - $failed ) . "/$total passed\n";
exit( $failed ? 1 : 0 );
