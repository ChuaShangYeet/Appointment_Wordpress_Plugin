<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** A bookable start and the resources that can serve it. */
final class SlotOption {

	/** @var int */
	public $start;
	/** @var int */
	public $end;
	/** @var int[] sorted for staff (any of them may serve); in seating order for tables (all of them are used) */
	public $resource_ids;
	/** @var bool restaurant: the slot seats the party at proposed tables, not at a choice of staff */
	public $tables = false;
	/** @var int restaurant: minimum spend of the proposed tables, in cents */
	public $minimum_spend = 0;
	/** @var int[]|null workshop: the bays the job would use (resource_ids then holds its mechanics) */
	public $bay_ids = null;

	public function __construct( int $start, int $end, array $resource_ids ) {
		sort( $resource_ids );
		$this->start        = $start;
		$this->end          = $end;
		$this->resource_ids = $resource_ids;
	}

	/** @param int[] $table_ids */
	public static function seating( int $start, int $end, array $table_ids, int $minimum_spend ): self {
		$s                = new self( $start, $end, array() );
		$s->resource_ids  = array_values( $table_ids );
		$s->tables        = true;
		$s->minimum_spend = $minimum_spend;
		return $s;
	}

	/**
	 * A workshop job: the mechanics and bays it would use, in segment order.
	 *
	 * @param int[] $mechanic_ids
	 * @param int[] $bay_ids
	 */
	public static function job( int $start, int $end, array $mechanic_ids, array $bay_ids ): self {
		$s               = new self( $start, $end, array() );
		$s->resource_ids = array_values( $mechanic_ids );
		$s->bay_ids      = array_values( $bay_ids );
		return $s;
	}

	public function to_array( \DateTimeZone $tz ): array {
		$out = array(
			'start' => Clock::format( $this->start, $tz ),
			'end'   => Clock::format( $this->end, $tz ),
		);
		if ( null !== $this->bay_ids ) {
			return $out + array( 'staff_ids' => $this->resource_ids, 'bay_ids' => $this->bay_ids );
		}
		if ( ! $this->tables ) {
			return $out + array( 'staff_ids' => $this->resource_ids );
		}
		return $out + array(
			'table_ids'     => $this->resource_ids,
			'combined'      => count( $this->resource_ids ) > 1,
			'minimum_spend' => $this->minimum_spend > 0 ? MinimumSpend::format( $this->minimum_spend ) : null,
		);
	}
}
