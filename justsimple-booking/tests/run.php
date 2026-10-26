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
use JustSimple\Booking\Engine\Interval;
use JustSimple\Booking\Engine\IntervalMath;
use JustSimple\Booking\Engine\Policy;
use JustSimple\Booking\Engine\ScheduleSource;
use JustSimple\Booking\Engine\StaffAllocator;
use JustSimple\Booking\Engine\StatusFlow;

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
final class MemSource implements ScheduleSource {
	public $days_off = array();
	public $hours    = array();
	public $staff    = array(); // id => true (active)
	public $links    = array(); // service id => [resource ids]
	public $shifts   = array();
	public $alloc    = array(); // [resource_id, start, end, state, hold_expires|null]

	public function days_off( int $module_id ): array {
		return $this->days_off;
	}
	public function operating_hours( int $module_id ): array {
		return $this->hours;
	}
	public function qualified_resource_ids( int $module_id, string $type, array $service_ids, ?int $only_id ): array {
		$ids = array_keys( array_filter( $this->staff ) );
		if ( null !== $only_id ) {
			$ids = array_values( array_intersect( $ids, array( $only_id ) ) );
		}
		foreach ( $service_ids as $sid ) {
			if ( isset( $this->links[ $sid ] ) ) {
				$ids = array_values( array_intersect( $ids, $this->links[ $sid ] ) );
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
test( 'the allocator factory knows staff and salon only', function () {
	ok( Allocators::for_type( 'salon' ) instanceof SalonAllocator, 'salon' );
	ok( Allocators::for_type( 'staff' ) instanceof StaffAllocator, 'staff' );
	throws( EngineError::VALIDATION, function () {
		Allocators::for_type( 'restaurant' );
	} );
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
