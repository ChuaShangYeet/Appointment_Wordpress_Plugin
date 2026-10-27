<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** The extra reads a restaurant needs on top of ScheduleSource. */
interface RestaurantSource extends ScheduleSource {

	/**
	 * Every table of the module, active or not.
	 *
	 * @return array<int,array{id:int,name:string,capacity:int,status:string,is_vip:bool,minimum_spend:int}> minimum_spend in cents
	 */
	public function tables( int $module_id ): array;

	/**
	 * The module's seating-time setting, or null when not set up yet.
	 *
	 * @return array{booking_mode:string,slot_duration_minutes:int,slot_interval_minutes:int,first_slot_time:string,last_slot_time:string,buffer_minutes:int,allow_table_selection:bool,policy_id:?int}|null
	 */
	public function slot_settings( int $module_id ): ?array;

	/** @return array<int,array{weekday:?int,specific_date:?string,open_tables:int}> rules for one table class */
	public function open_rules( int $module_id, string $table_type ): array;
}
