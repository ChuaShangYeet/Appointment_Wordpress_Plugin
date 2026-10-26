<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * Half-open [start, end) span of real time, as UTC Unix seconds.
 *
 * Integers rather than DateTime objects on purpose: comparing and subtracting them is real elapsed time whatever the
 * site's time zone does, which is what the engine's Interval guarantees by converting to UTC (backend/app/services/availability.py).
 * Convert to the site's zone only to show a time (Clock::format).
 */
final class Interval {

	/** @var int */
	public $start;
	/** @var int */
	public $end;

	public function __construct( int $start, int $end ) {
		if ( $end < $start ) {
			throw new \InvalidArgumentException( 'interval end before start' );
		}
		$this->start = $start;
		$this->end   = $end;
	}

	public function minutes(): float {
		return ( $this->end - $this->start ) / 60;
	}

	public function overlaps( Interval $other ): bool {
		return $this->start < $other->end && $other->start < $this->end;
	}

	/** Sort order: by start, then end (the Python dataclass's order=True). */
	public static function compare( Interval $a, Interval $b ): int {
		return array( $a->start, $a->end ) <=> array( $b->start, $b->end );
	}
}
