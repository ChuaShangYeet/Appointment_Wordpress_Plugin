<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * Restaurant: table fitting and the separate-seating solver. Ported from allocators/restaurant.py (blueprint §7).
 *
 * - A party is seated at the smallest single table that fits or, only if the customer allows separate seating, at the
 *   fewest tables whose seats cover it (ties: fewest spare seats, then table names and ids).
 * - VIP and normal tables are separate pools and never combined.
 * - An open-table rule caps how many tables of a class may be in use at the same time; a date rule beats a weekday rule.
 * - Starts come from the seating-time setting (flexible or fixed); a table is held for duration + buffer.
 * - A customer may name one table only when the module allows table selection; it is never combined with others.
 *
 * The cap and the table choice are decided under BookingService's per-module lock, so concurrent requests are decided one
 * after the other against saved bookings and cannot go past the cap together.
 */
final class RestaurantAllocator implements Allocator {

	const MAX_COMBINED_TABLES = 6;

	public function find_slots( ScheduleSource $source, AvailabilityRequest $req ): array {
		$out = array();
		foreach ( $this->plan( self::source( $source ), $req ) as $p ) {
			$out[] = SlotOption::seating( $p[0], $p[1], array_column( $p[2], 'id' ), self::spend( $p[2] ) );
		}
		return $out;
	}

	public function allocate( ScheduleSource $source, AvailabilityRequest $req, int $start ): array {
		$plan = $this->plan( self::source( $source ), $req );
		foreach ( $plan as $p ) {
			if ( $p[0] === $start ) {
				return array_map( static function ( $t ) use ( $p ) {
					return new Assignment( (int) $t['id'], $p[0], $p[1], null, (int) $t['minimum_spend'] );
				}, $p[2] );
			}
		}
		throw new EngineError(
			EngineError::SLOT_UNAVAILABLE,
			'That time is not available for this party.',
			array( 'slots' => array_map( static function ( $p ) use ( $req ) {
				return SlotOption::seating( $p[0], $p[1], array_column( $p[2], 'id' ), self::spend( $p[2] ) )->to_array( $req->tz );
			}, $plan ) )
		);
	}

	/** The seating-time policy override, if any (TimeSlotSetting.policy_id). */
	public static function settings_or_fail( RestaurantSource $source, int $module_id ): array {
		$s = $source->slot_settings( $module_id );
		if ( null === $s ) {
			throw new EngineError( EngineError::VALIDATION, 'This restaurant has not set its seating times yet.' );
		}
		return $s;
	}

	// ------------------------------------------------------------------ pure logic

	/**
	 * Tables for `$party`, or null when impossible.
	 *
	 * 1. the smallest single table with enough seats;
	 * 2. if `$allow_separate`: the fewest tables (at most `$max_tables`) whose seats add up to at least the party; among equals the
	 *    fewest seats in total, then the lowest names/ids. A small subset-sum over reachable totals, keeping for each total the
	 *    combination with the fewest tables; a set that already covers the party is never extended.
	 *
	 * @param array<int,array{id:int,name:string,capacity:int}> $tables
	 */
	public static function solve_seating( array $tables, int $party, bool $allow_separate, int $max_tables = self::MAX_COMBINED_TABLES ): ?array {
		if ( $party < 1 ) {
			throw new \InvalidArgumentException( 'party must be >= 1' );
		}
		usort( $tables, static function ( $a, $b ) {
			return $a['capacity'] <=> $b['capacity'] ?: strcmp( $a['name'], $b['name'] ) ?: strcmp( (string) $a['id'], (string) $b['id'] );
		} );
		foreach ( $tables as $t ) {
			if ( $t['capacity'] >= $party ) {
				return array( $t );
			}
		}
		if ( ! $allow_separate ) {
			return null;
		}

		$states = array( 0 => array( 0, array() ) ); // total seats => [table count, tables]
		foreach ( $tables as $table ) {
			foreach ( $states as $total => $state ) { // PHP iterates a copy: tables are used at most once per combination
				if ( $total >= $party || $state[0] >= $max_tables ) {
					continue;
				}
				$new_total = $total + $table['capacity'];
				$new_state = array( $state[0] + 1, array_merge( $state[1], array( $table ) ) );
				if ( ! isset( $states[ $new_total ] ) || self::combo_cmp( $new_state, $states[ $new_total ] ) < 0 ) {
					$states[ $new_total ] = $new_state;
				}
			}
		}
		$best = null;
		foreach ( $states as $total => $state ) {
			if ( $total < $party ) {
				continue;
			}
			if ( null === $best || ( $state[0] <=> $best[1][0] ?: $total <=> $best[0] ?: self::ids_cmp( $state, $best[1] ) ) < 0 ) {
				$best = array( $total, $state );
			}
		}
		return null === $best ? null : $best[1][1];
	}

	/**
	 * The largest number of `$intervals` active at once anywhere inside `$window`. Ends are counted before starts at the same
	 * moment, so two bookings that only touch never count as overlapping.
	 *
	 * @param Interval[] $intervals
	 */
	public static function max_concurrency( array $intervals, Interval $window ): int {
		$events = array();
		foreach ( $intervals as $iv ) {
			$s = max( $iv->start, $window->start );
			$e = min( $iv->end, $window->end );
			if ( $s < $e ) {
				$events[] = array( $s, 1 );
				$events[] = array( $e, -1 );
			}
		}
		usort( $events, static function ( $a, $b ) {
			return $a[0] <=> $b[0] ?: $a[1] <=> $b[1];
		} );
		$current = 0;
		$peak    = 0;
		foreach ( $events as $ev ) {
			$current += $ev[1];
			$peak     = max( $peak, $current );
		}
		return $peak;
	}

	/**
	 * Reservation starts for one business day (blueprint §7.3), sorted and unique.
	 *
	 * - flexible: from each opening window's start, every `$interval`, while the seating (`$duration`) fits before closing;
	 * - fixed: from `$first` every `$interval` through `$last` (the next day if `$last` is before `$first`: a service past midnight),
	 *   keeping only starts whose seating fits inside an opening window.
	 *
	 * @param Interval[] $windows
	 * @return int[]
	 */
	public static function candidate_starts( string $mode, array $windows, string $day, \DateTimeZone $tz, int $duration, int $interval, string $first, string $last ): array {
		$starts = array();
		if ( 'flexible' === $mode ) {
			foreach ( $windows as $w ) {
				for ( $t = $w->start; $t + $duration <= $w->end; $t += $interval ) {
					$starts[] = $t;
				}
			}
		} else {
			$stop = Clock::clock_on( Clock::hms( $last ) < Clock::hms( $first ) ? Clock::add_days( $day, 1 ) : $day, $last, $tz );
			for ( $t = Clock::clock_on( $day, $first, $tz ); $t <= $stop; $t += $interval ) {
				foreach ( $windows as $w ) {
					if ( $w->start <= $t && $t + $duration <= $w->end ) {
						$starts[] = $t;
						break;
					}
				}
			}
		}
		$starts = array_values( array_unique( $starts ) );
		sort( $starts );
		return $starts;
	}

	// ------------------------------------------------------------------ the plan

	/** @return array<int,array{0:int,1:int,2:array}> [start, end, tables] for every offered start, sorted by start */
	private function plan( RestaurantSource $source, AvailabilityRequest $req ): array {
		if ( null === $req->party_size || $req->party_size < 1 ) {
			throw new EngineError( EngineError::VALIDATION, 'Give the party size (at least 1).', array( 'field' => 'party_size' ) );
		}
		if ( ! in_array( $req->table_type, array( 'normal', 'vip' ), true ) ) {
			throw new EngineError( EngineError::VALIDATION, "table_type must be 'normal' or 'vip'.", array( 'field' => 'table_type' ) );
		}
		$setting = self::settings_or_fail( $source, $req->module_id );
		if ( null !== $req->resource_id ) {
			if ( empty( $setting['allow_table_selection'] ) ) {
				throw new EngineError( EngineError::VALIDATION, 'Tables are assigned automatically at this restaurant; a chosen table is not accepted.', array( 'field' => 'table_id' ) );
			}
			if ( $req->allow_separate_seating ) {
				throw new EngineError( EngineError::VALIDATION, 'A chosen table cannot be combined with others; turn separate seating off.', array( 'field' => 'allow_separate_seating' ) );
			}
		}

		$offs = $source->days_off( $req->module_id );
		foreach ( $offs as $o ) {
			if ( null === $o['resource_id'] && Clock::covers_date( $o, $req->day ) ) {
				return array();
			}
		}
		$windows = Availability::operating_windows( $source, $req );
		if ( ! $windows ) { // closed that day, or no opening hours set at all: a restaurant needs them
			return array();
		}

		$vip  = 'vip' === $req->table_type;
		$rows = array_values( array_filter( $source->tables( $req->module_id ), static function ( $t ) {
			return 'active' === $t['status'];
		} ) );
		$pool = array_values( array_filter( $rows, static function ( $t ) use ( $vip ) {
			return (bool) $t['is_vip'] === $vip && $t['capacity'] > 0;
		} ) );
		$on_leave = array();
		foreach ( $offs as $o ) {
			if ( null !== $o['resource_id'] && Clock::covers_date( $o, $req->day ) ) {
				$on_leave[ (int) $o['resource_id'] ] = true;
			}
		}
		$tables = array_values( array_filter( $pool, static function ( $t ) use ( $on_leave ) {
			return ! isset( $on_leave[ $t['id'] ] );
		} ) );
		if ( null !== $req->resource_id ) {
			$chosen = null;
			foreach ( $rows as $t ) {
				if ( $t['id'] === $req->resource_id ) {
					$chosen = $t;
				}
			}
			if ( null === $chosen ) {
				throw new EngineError( EngineError::NOT_FOUND, 'Table not found.', array( 'field' => 'table_id' ) );
			}
			if ( (bool) $chosen['is_vip'] !== $vip ) {
				throw new EngineError( EngineError::VALIDATION, 'That table is in the other seating class; set table_type to match.', array( 'field' => 'table_type' ) );
			}
			$tables = array_values( array_filter( $tables, static function ( $t ) use ( $req ) {
				return $t['id'] === $req->resource_id;
			} ) ); // empty when the table is out of service that day
		}
		if ( ! $tables ) {
			return array();
		}

		$duration  = 60 * (int) $setting['slot_duration_minutes'];
		$occupancy = $duration + 60 * (int) $setting['buffer_minutes'];
		$busy      = $source->busy( array_column( $pool, 'id' ), $req->day_start(), $req->overnight_end(), $req->now );
		$all_busy  = $busy ? array_merge( ...array_values( $busy ) ) : array();
		$cap       = self::open_cap( $source, $req );
		list( $earliest, $latest ) = $req->policy->bounds( $req->now, $req->ignore_notice );

		$plan = array();
		foreach ( self::candidate_starts( $setting['booking_mode'], $windows, $req->day, $req->tz, $duration, 60 * (int) $setting['slot_interval_minutes'], $setting['first_slot_time'], $setting['last_slot_time'] ) as $start ) {
			if ( $start < $earliest || $start > $latest ) {
				continue;
			}
			$slot = new Interval( $start, $start + $occupancy );
			$free = array_values( array_filter( $tables, static function ( $t ) use ( $busy, $slot ) {
				foreach ( $busy[ $t['id'] ] ?? array() as $iv ) {
					if ( $iv->overlaps( $slot ) ) {
						return false;
					}
				}
				return true;
			} ) );
			$chosen = self::solve_seating( $free, $req->party_size, $req->allow_separate_seating );
			if ( null === $chosen ) {
				continue;
			}
			if ( null !== $cap && self::max_concurrency( $all_busy, $slot ) + count( $chosen ) > $cap ) {
				continue;
			}
			$plan[] = array( $slot->start, $slot->end, $chosen );
		}
		return $plan;
	}

	private static function open_cap( RestaurantSource $source, AvailabilityRequest $req ): ?int {
		$by_date    = null;
		$by_weekday = null;
		$wd         = Clock::weekday_index( $req->day );
		foreach ( $source->open_rules( $req->module_id, $req->table_type ) as $r ) {
			if ( null !== $r['specific_date'] && $r['specific_date'] === $req->day ) {
				$by_date = $r;
			} elseif ( null !== $r['weekday'] && (int) $r['weekday'] === $wd ) {
				$by_weekday = $r;
			}
		}
		$rule = $by_date ?? $by_weekday;
		return null === $rule ? null : (int) $rule['open_tables'];
	}

	private static function spend( array $tables ): int {
		return (int) array_sum( array_column( $tables, 'minimum_spend' ) );
	}

	private static function source( ScheduleSource $source ): RestaurantSource {
		if ( ! $source instanceof RestaurantSource ) {
			throw new \LogicException( 'A restaurant needs a RestaurantSource.' );
		}
		return $source;
	}

	/** Combination order: fewer tables, then names and ids in seating order. */
	private static function combo_cmp( array $a, array $b ): int {
		return $a[0] <=> $b[0] ?: self::ids_cmp( $a, $b );
	}

	private static function ids_cmp( array $a, array $b ): int {
		$ka = array_map( static function ( $t ) {
			return $t['name'] . '|' . $t['id'];
		}, $a[1] );
		$kb = array_map( static function ( $t ) {
			return $t['name'] . '|' . $t['id'];
		}, $b[1] );
		for ( $i = 0, $n = min( count( $ka ), count( $kb ) ); $i < $n; $i++ ) {
			$c = strcmp( $ka[ $i ], $kb[ $i ] );
			if ( 0 !== $c ) {
				return $c;
			}
		}
		return count( $ka ) <=> count( $kb );
	}
}
