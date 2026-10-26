<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** Pure interval maths, ported one-to-one from backend/app/services/availability.py. */
final class IntervalMath {

	/**
	 * `$windows` with every `$busy` span cut out (results sorted, non-empty).
	 *
	 * @param Interval[] $windows
	 * @param Interval[] $busy
	 * @return Interval[]
	 */
	public static function subtract( array $windows, array $busy ): array {
		usort( $windows, array( Interval::class, 'compare' ) );
		usort( $busy, array( Interval::class, 'compare' ) );
		$result = array();
		foreach ( $windows as $w ) {
			$cursor = $w->start;
			foreach ( $busy as $b ) {
				if ( $b->end <= $cursor || $b->start >= $w->end ) {
					continue;
				}
				if ( $b->start > $cursor ) {
					$result[] = new Interval( $cursor, $b->start );
				}
				$cursor = max( $cursor, $b->end );
				if ( $cursor >= $w->end ) {
					break;
				}
			}
			if ( $cursor < $w->end ) {
				$result[] = new Interval( $cursor, $w->end );
			}
		}
		return $result;
	}

	/**
	 * Pairwise intersection of two interval lists.
	 *
	 * @param Interval[] $a
	 * @param Interval[] $b
	 * @return Interval[]
	 */
	public static function intersect( array $a, array $b ): array {
		$out = array();
		foreach ( $a as $x ) {
			foreach ( $b as $y ) {
				$lo = max( $x->start, $y->start );
				$hi = min( $x->end, $y->end );
				if ( $lo < $hi ) {
					$out[] = new Interval( $lo, $hi );
				}
			}
		}
		usort( $out, array( Interval::class, 'compare' ) );
		return $out;
	}

	/**
	 * Discrete `$block`-second slots inside `$free`.
	 *
	 * Starts are aligned to `$step` counted from `$anchor` (local midnight of the business day) so every resource offers the
	 * same grid, and are limited to [$earliest, $latest]. All in real elapsed seconds: no start falls in an hour the clocks
	 * skipped, and both passes of a repeated hour are offered.
	 *
	 * @param Interval[] $free
	 * @return Interval[]
	 */
	public static function generate_slots( array $free, int $block, int $step, int $anchor, int $earliest, int $latest ): array {
		$slots = array();
		foreach ( $free as $f ) {
			$first  = max( $f->start, $earliest );
			$offset = ( ( $first - $anchor ) % $step + $step ) % $step; // PHP's % keeps the sign of the left side
			$t      = 0 === $offset ? $first : $first + ( $step - $offset );
			while ( $t + $block <= $f->end && $t <= $latest ) {
				$slots[] = new Interval( $t, $t + $block );
				$t      += $step;
			}
		}
		return $slots;
	}

	/** Whether `$segment` lies entirely inside one of the `$free` intervals. */
	public static function fits( array $free, Interval $segment ): bool {
		foreach ( $free as $f ) {
			if ( $f->start <= $segment->start && $segment->end <= $f->end ) {
				return true;
			}
		}
		return false;
	}
}
