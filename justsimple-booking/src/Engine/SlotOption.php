<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** A bookable start and the resources that can serve it. */
final class SlotOption {

	/** @var int */
	public $start;
	/** @var int */
	public $end;
	/** @var int[] sorted */
	public $resource_ids;

	public function __construct( int $start, int $end, array $resource_ids ) {
		sort( $resource_ids );
		$this->start        = $start;
		$this->end          = $end;
		$this->resource_ids = $resource_ids;
	}

	public function to_array( \DateTimeZone $tz ): array {
		return array(
			'start'     => Clock::format( $this->start, $tz ),
			'end'       => Clock::format( $this->end, $tz ),
			'staff_ids' => $this->resource_ids,
		);
	}
}
