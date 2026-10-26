<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** Everything the availability pipeline needs, built once per request. */
final class AvailabilityRequest {

	/** @var int */
	public $module_id;
	/** @var \DateTimeZone */
	public $tz;
	/** @var array<int,array{id:int,duration_minutes:int,buffer_minutes:int}> services in the order chosen */
	public $services;
	/** @var Policy */
	public $policy;
	/** @var string business day, 'Y-m-d' in the site's zone */
	public $day;
	/** @var int|null a named staff member, or null for "any available" */
	public $resource_id;
	/** @var int UTC seconds */
	public $now;
	/** @var bool admin moves skip notice and window */
	public $ignore_notice = false;
	/** @var int seconds between offered starts */
	public $step = 900;

	public function __construct( int $module_id, \DateTimeZone $tz, array $services, Policy $policy, string $day, ?int $resource_id, int $now ) {
		$this->module_id   = $module_id;
		$this->tz          = $tz;
		$this->services    = $services;
		$this->policy      = $policy;
		$this->day         = $day;
		$this->resource_id = $resource_id;
		$this->now         = $now;
	}

	/** The moment business day `day` begins (midnight on the clock). */
	public function day_start(): int {
		return Clock::clock_on( $this->day, '00:00:00', $this->tz );
	}

	/** The end of the day after: a night shift that starts on `day` may run to then. */
	public function overnight_end(): int {
		return Clock::clock_on( Clock::add_days( $this->day, 2 ), '00:00:00', $this->tz );
	}

	/** Seconds reserved on the resource: all durations plus all buffers. */
	public function block(): int {
		$min = 0;
		foreach ( $this->services as $s ) {
			$min += (int) $s['duration_minutes'] + (int) $s['buffer_minutes'];
		}
		return 60 * $min;
	}

	public function service_ids(): array {
		return array_map( static function ( $s ) {
			return (int) $s['id'];
		}, $this->services );
	}

	public function with_day( string $day ): self {
		$copy      = clone $this;
		$copy->day = $day;
		return $copy;
	}
}
