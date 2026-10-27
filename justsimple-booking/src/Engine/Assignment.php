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

	public function __construct( int $resource_id, int $start, int $end, ?int $service_id = null, int $minimum_spend = 0 ) {
		$this->resource_id   = $resource_id;
		$this->start         = $start;
		$this->end           = $end;
		$this->service_id    = $service_id;
		$this->minimum_spend = $minimum_spend;
	}
}
