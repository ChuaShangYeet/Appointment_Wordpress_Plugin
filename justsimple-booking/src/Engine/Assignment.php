<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** One resource reserved over [start, end). */
final class Assignment {

	/** @var int */
	public $resource_id;
	/** @var int */
	public $start;
	/** @var int */
	public $end;
	/** @var int|null the service this reservation is for, when a booking is made of segments (workshop) */
	public $service_id;
	/** @var int minimum spend attached to this resource (VIP tables), in cents */
	public $minimum_spend;
	/** @var int seconds held before the customer-visible window (on-site travel) */
	public $pad_before;
	/** @var int seconds held after it */
	public $pad_after;

	public function __construct( int $resource_id, int $start, int $end, ?int $service_id = null, int $minimum_spend = 0, int $pad_before = 0, int $pad_after = 0 ) {
		$this->resource_id   = $resource_id;
		$this->start         = $start;
		$this->end           = $end;
		$this->service_id    = $service_id;
		$this->minimum_spend = $minimum_spend;
		$this->pad_before    = $pad_before;
		$this->pad_after     = $pad_after;
	}

	/** The customer-visible window: the allocation without its travel padding. */
	public function service_start(): int {
		return $this->start + $this->pad_before;
	}

	public function service_end(): int {
		return $this->end - $this->pad_after;
	}
}
