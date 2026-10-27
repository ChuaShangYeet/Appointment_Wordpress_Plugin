<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** The extra read a workshop needs on top of ScheduleSource. */
interface WorkshopSource extends ScheduleSource {

	/**
	 * Name and type of each resource asked for (missing ids are left out).
	 *
	 * @param int[] $ids
	 * @return array<int,array{name:string,type:string}>
	 */
	public function resources( array $ids ): array;
}
