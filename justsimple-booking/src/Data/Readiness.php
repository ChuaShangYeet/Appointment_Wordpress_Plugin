<?php
namespace JustSimple\Booking\Data;

defined( 'ABSPATH' ) || exit;

/**
 * "Is this booking page ready to take bookings?" One list per kind of module, each item a thing the owner can do, in the order
 * they would do it. The checks ask the same questions the engine asks when it looks for free times (an active service, someone
 * qualified for it with working hours, the tables, the bays, a car model, the business location), so a ticked list means the
 * booking page can show times.
 */
final class Readiness {

	/**
	 * @return array<int,array{key:string,done:bool,label:string,section:string}>
	 */
	public static function check( array $m ): array {
		global $wpdb;
		$id       = (int) $m['id'];
		$source   = new WpScheduleSource();
		$services = $wpdb->get_results( $wpdb->prepare( 'SELECT id, location_type FROM ' . Schema::table( 'services' ) . ' WHERE module_id = %d AND active = 1 AND is_addon = 0', $id ), ARRAY_A );
		$worked   = array_map( 'intval', $wpdb->get_col( $wpdb->prepare( 'SELECT DISTINCT s.resource_id FROM ' . Schema::table( 'shifts' ) . ' s JOIN ' . Schema::table( 'resources' ) . " r ON r.id = s.resource_id WHERE r.module_id = %d AND r.type = 'staff' AND r.status = 'active'", $id ) ) );
		$count    = static function ( string $type ) use ( $wpdb, $id ) {
			return (int) $wpdb->get_var( $wpdb->prepare( 'SELECT COUNT(*) FROM ' . Schema::table( 'resources' ) . " WHERE module_id = %d AND type = %s AND status = 'active'", $id, $type ) );
		};
		$who      = array( 'staff' => 'someone', 'salon' => 'a stylist', 'workshop' => 'a mechanic', 'field' => 'someone' )[ $m['type'] ] ?? 'someone';
		$items    = array();

		if ( 'restaurant' === $m['type'] ) {
			$items[] = array( 'tables', $count( 'table' ) > 0, 'Add your tables and how many each seats', 'tables' );
			$items[] = array( 'seating', null !== $source->slot_settings( $id ), 'Set your seating times', 'seating' );
		} else {
			$items[] = array( 'services', (bool) $services, 'Add a service people can book', 'services' );
			$items[] = array( 'hours', (bool) $worked, sprintf( 'Give %s working hours', $who ), 'people' );
			// every service has at least one qualified person who works: otherwise it never shows a free time
			$uncovered = array_filter( $services, static function ( $s ) use ( $source, $id, $worked ) {
				return ! array_intersect( $source->qualified_resource_ids( $id, 'staff', array( (int) $s['id'] ), null ), $worked );
			} );
			$items[] = array( 'covered', $services && $worked && ! $uncovered, 'Make sure every service has someone who can do it', 'people' );
			if ( 'workshop' === $m['type'] ) {
				$items[] = array( 'bays', $count( 'bay' ) > 0, 'Add your bays', 'bays' );
				$items[] = array( 'vehicles', (bool) $wpdb->get_var( $wpdb->prepare( 'SELECT 1 FROM ' . Schema::table( 'supported_vehicles' ) . ' WHERE module_id = %d LIMIT 1', $id ) ), 'Add the car models you service', 'vehicles' );
			}
			if ( 'field' === $m['type'] && in_array( 'on_site', array_column( $services, 'location_type' ), true ) ) {
				$s       = $m['settings'];
				$items[] = array( 'location', null !== $s['latitude'] && null !== $s['longitude'] && null !== $s['service_radius_km'], 'Set where you are and how far you travel', 'location' );
			}
		}
		$items[] = array( 'on', 'active' === $m['status'], 'Switch the booking page on', 'basics' );
		$items[] = array( 'page', '' !== (string) $m['settings']['page_url'], 'Put the shortcode on a page of your site', 'basics' );

		return array_map( static function ( $i ) {
			return array( 'key' => $i[0], 'done' => (bool) $i[1], 'label' => $i[2], 'section' => $i[3] );
		}, $items );
	}
}
