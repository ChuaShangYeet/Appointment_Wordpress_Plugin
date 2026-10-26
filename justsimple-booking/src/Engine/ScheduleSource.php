<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * What the engine reads. The WordPress implementation is Data\WpScheduleSource; the tests use an in-memory one,
 * so the engine itself never touches WordPress or the database.
 */
interface ScheduleSource {

	/** @return array<int,array{resource_id:?int,start_date:string,end_date:string,repeat_yearly:mixed}> */
	public function days_off( int $module_id ): array;

	/** No rows means no opening-hours limit. @return array<int,array{weekday:int,open_time:string,close_time:string}> */
	public function operating_hours( int $module_id ): array;

	/**
	 * Active resources of `$type` in the module qualified for *every* service. A service with no links to resources of that
	 * type accepts any active one. `$only_id` restricts the pool to one resource.
	 *
	 * @param int[] $service_ids
	 * @return int[]
	 */
	public function qualified_resource_ids( int $module_id, string $type, array $service_ids, ?int $only_id ): array;

	/** @param int[] $resource_ids @return array<int,array{resource_id:int,start_time:string,end_time:string,break_start:?string,break_end:?string}> */
	public function shifts( array $resource_ids, int $weekday ): array;

	/**
	 * Held or active allocations overlapping [$lo, $hi) per resource. Held ones whose hold has expired at `$now` are ignored.
	 *
	 * @param int[] $resource_ids
	 * @return array<int,Interval[]>
	 */
	public function busy( array $resource_ids, int $lo, int $hi, int $now ): array;
}
