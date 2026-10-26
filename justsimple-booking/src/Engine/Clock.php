<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * Wall clock <-> real time. Days are 'Y-m-d' strings and clock times 'H:i' or 'H:i:s' strings, as MySQL returns them.
 *
 * Business Day Rule (blueprint §6): time after midnight belongs to the day the window started on, so a Friday 18:00-02:00
 * window is Friday's, including its early hours of Saturday.
 */
final class Clock {

	/**
	 * The moment the clock in `$tz` reads `$at` on `$day`, as UTC seconds.
	 *
	 * A reading skipped by a clock change (the hour they go forward) becomes the moment after the jump, as PHP and Python both do.
	 * A reading that happens twice (the hour they go back) is its first time round: checked by the engine tests, because PHP
	 * versions have differed here.
	 */
	public static function clock_on( string $day, string $at, \DateTimeZone $tz ): int {
		$wall = $day . ' ' . self::hms( $at );
		$ts   = ( new \DateTimeImmutable( $wall, $tz ) )->getTimestamp();
		// PHP resolves a repeated reading to its second pass; Python (fold=0) and the engine use the first. If the clocks went
		// back within the last few hours, the reading `delta` seconds earlier may be the same wall time: then that is the first pass.
		$delta = $tz->getOffset( new \DateTimeImmutable( '@' . ( $ts - 3 * 3600 ) ) ) - $tz->getOffset( new \DateTimeImmutable( '@' . $ts ) );
		if ( $delta > 0 && ( new \DateTimeImmutable( '@' . ( $ts - $delta ) ) )->setTimezone( $tz )->format( 'Y-m-d H:i:s' ) === $wall ) {
			return $ts - $delta;
		}
		return $ts;
	}

	/** Opening hours or a shift whose end is not after its start run past midnight. */
	public static function is_overnight( string $start, string $end ): bool {
		return self::hms( $end ) <= self::hms( $start );
	}

	public static function window_on( string $day, string $start, string $end, \DateTimeZone $tz ): Interval {
		$end_day = self::is_overnight( $start, $end ) ? self::add_days( $day, 1 ) : $day;
		return new Interval( self::clock_on( $day, $start, $tz ), self::clock_on( $end_day, $end, $tz ) );
	}

	/**
	 * Working windows of one shift that starts on `$day` (it may end the next day), with the break removed.
	 *
	 * @param array{start_time:string,end_time:string,break_start:?string,break_end:?string} $shift
	 * @return Interval[]
	 */
	public static function shift_windows( array $shift, string $day, \DateTimeZone $tz ): array {
		$whole = self::window_on( $day, $shift['start_time'], $shift['end_time'], $tz );
		if ( empty( $shift['break_start'] ) || empty( $shift['break_end'] ) ) {
			return array( $whole );
		}
		// a break time earlier than the shift's start on the clock is after midnight; likewise for its end
		$lo_day = self::hms( $shift['break_start'] ) < self::hms( $shift['start_time'] ) ? self::add_days( $day, 1 ) : $day;
		$hi_day = self::hms( $shift['break_end'] ) <= self::hms( $shift['break_start'] ) ? self::add_days( $lo_day, 1 ) : $lo_day;
		$lo     = self::clock_on( $lo_day, $shift['break_start'], $tz );
		$hi     = self::clock_on( $hi_day, $shift['break_end'], $tz );
		return array( new Interval( $whole->start, $lo ), new Interval( $hi, $whole->end ) );
	}

	/** 0 = Sunday .. 6 = Saturday, as in the engine and the shifts table. */
	public static function weekday_index( string $day ): int {
		return (int) ( new \DateTimeImmutable( $day, new \DateTimeZone( 'UTC' ) ) )->format( 'w' );
	}

	public static function add_days( string $day, int $n ): string {
		return ( new \DateTimeImmutable( $day, new \DateTimeZone( 'UTC' ) ) )->modify( ( $n >= 0 ? '+' : '' ) . $n . ' day' )->format( 'Y-m-d' );
	}

	/**
	 * Whether a day-off entry applies to `$day` (handles repeat_yearly and spans across the new year).
	 *
	 * @param array{start_date:string,end_date:string,repeat_yearly:mixed} $off
	 */
	public static function covers_date( array $off, string $day ): bool {
		if ( empty( $off['repeat_yearly'] ) ) {
			return $off['start_date'] <= $day && $day <= $off['end_date'];
		}
		$utc    = new \DateTimeZone( 'UTC' );
		$start  = new \DateTimeImmutable( $off['start_date'], $utc );
		$length = (int) $start->diff( new \DateTimeImmutable( $off['end_date'], $utc ) )->days;
		$year   = (int) substr( $day, 0, 4 );
		foreach ( array( $year, $year - 1 ) as $y ) {
			$m = (int) $start->format( 'n' );
			$d = (int) $start->format( 'j' );
			if ( ! checkdate( $m, $d, $y ) ) { // 29 Feb in a non-leap year
				$d = 28;
			}
			$from = sprintf( '%04d-%02d-%02d', $y, $m, $d );
			if ( $from <= $day && $day <= self::add_days( $from, $length ) ) {
				return true;
			}
		}
		return false;
	}

	/** The local calendar date of a UTC instant. */
	public static function local_date( int $ts, \DateTimeZone $tz ): string {
		return ( new \DateTimeImmutable( '@' . $ts ) )->setTimezone( $tz )->format( 'Y-m-d' );
	}

	/** ISO 8601 with the site's offset, e.g. 2026-10-27T10:00:00+08:00. */
	public static function format( int $ts, \DateTimeZone $tz ): string {
		return ( new \DateTimeImmutable( '@' . $ts ) )->setTimezone( $tz )->format( DATE_ATOM );
	}

	/** 'H:i' -> 'H:i:s' so string comparison orders times correctly. */
	public static function hms( string $t ): string {
		return 5 === strlen( $t ) ? $t . ':00' : $t;
	}
}
