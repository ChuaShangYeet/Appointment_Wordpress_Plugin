<?php
/**
 * One action against a real WordPress, meant to be started several times at once (each process has its own database connection):
 *
 *   php tests/concurrency.php <wordpress-root> upsell   <booking_id> <service_id>
 *   php tests/concurrency.php <wordpress-root> book     <module_id> <service_id> <staff_id> <iso-start>
 *   php tests/concurrency.php <wordpress-root> complete <booking_id>
 *
 * Prints one JSON line: { "action", "ok", "code"?, "extended"?, "added"? }. The fixture comes from tests/integration.php (SALONRACE).
 */

$jsb_args             = $argv; // read before WordPress loads; WordPress itself uses globals such as $action
$_SERVER['HTTP_HOST'] = 'localhost';
define( 'WP_DISABLE_FATAL_ERROR_HANDLER', true ); // a crash prints one line, not the HTML error page
require rtrim( $jsb_args[1], '/\\' ) . '/wp-load.php';
$argv   = $jsb_args;
$action = $jsb_args[2];

use JustSimple\Booking\Booking\BookingService;
use JustSimple\Booking\Booking\Upsell;
use JustSimple\Booking\Engine\EngineError;

wp_set_current_user( 1 );
try {
	switch ( $action ) {
		case 'upsell':
			$r   = Upsell::add_to_ticket( (int) $argv[3], array( (int) $argv[4] ), array() );
			$out = array( 'ok' => true, 'extended' => $r['extended'], 'added' => $r['added_minutes'], 'conflicts' => count( $r['conflicts'] ) );
			break;
		case 'book':
			$r   = BookingService::create( (int) $argv[3], array( 'service_ids' => array( (int) $argv[4] ), 'staff_id' => (int) $argv[5], 'start' => $argv[6], 'customer' => array( 'name' => 'Racer', 'phone' => '+601' . random_int( 10000000, 99999999 ) ) ) );
			$out = array( 'ok' => true, 'code' => $r['booking']['code'] );
			break;
		case 'book-table': // <module_id> <party> <iso-start> [table_type] [accepted_minimum_spend|-] [table_id|-]
			$r   = BookingService::create(
				(int) $argv[3],
				array(
					'party_size'             => (int) $argv[4],
					'start'                  => $argv[5],
					'table_type'             => $argv[6] ?? 'normal',
					'accepted_minimum_spend' => isset( $argv[7] ) && '-' !== $argv[7] ? $argv[7] : null,
					'table_id'               => isset( $argv[8] ) && '-' !== $argv[8] ? (int) $argv[8] : null,
					'customer'               => array( 'name' => 'Racer', 'phone' => '+601' . random_int( 10000000, 99999999 ) ),
				)
			);
			$out = array( 'ok' => true, 'tables' => array_column( $r['booking']['tables'], 'name' ), 'spend' => $r['booking']['minimum_spend_agreed'] );
			break;
		case 'book-job': // <module_id> <service_ids,comma-separated> <iso-start> <supported_vehicle_id>
			$r   = BookingService::create(
				(int) $argv[3],
				array(
					'service_ids' => array_map( 'intval', explode( ',', $argv[4] ) ),
					'start'       => $argv[5],
					'vehicle'     => array( 'plate_no' => 'R' . random_int( 10000, 99999 ), 'supported_vehicle_id' => (int) $argv[6] ),
					'customer'    => array( 'name' => 'Racer', 'phone' => '+601' . random_int( 10000000, 99999999 ) ),
				)
			);
			$out = array( 'ok' => true, 'staff' => array_column( $r['booking']['staff'], 'name' ), 'bays' => array_column( $r['booking']['bays'], 'name' ) );
			break;
		case 'set-parts': // <booking_id> <product_id:quantity,...>
			$lines = array();
			foreach ( explode( ',', $argv[4] ) as $pair ) {
				list( $pid, $qty ) = explode( ':', $pair );
				$lines[]           = array( 'product_id' => (int) $pid, 'quantity' => (int) $qty );
			}
			$r   = \JustSimple\Booking\Booking\Parts::set( (int) $argv[3], $lines );
			$out = array( 'ok' => true, 'parts' => array_map( static function ( $l ) {
				return $l['name'] . ' x' . $l['quantity'];
			}, $r ) );
			break;
		case 'book-field': // <module_id> <service_id> <iso-start> [latitude|-] [longitude|-] [staff_id|-]
			$in = array(
				'service_ids' => array( (int) $argv[4] ),
				'start'       => $argv[5],
				'staff_id'    => isset( $argv[8] ) && '-' !== $argv[8] ? (int) $argv[8] : null,
				'customer'    => array( 'name' => 'Racer', 'phone' => '+601' . random_int( 10000000, 99999999 ) ),
			);
			if ( isset( $argv[6] ) && '-' !== $argv[6] ) {
				$in['address'] = array( 'line1' => '1 Race Road', 'latitude' => (float) $argv[6], 'longitude' => (float) $argv[7] );
			}
			$r   = BookingService::create( (int) $argv[3], $in );
			$out = array( 'ok' => true, 'staff' => array_column( $r['booking']['staff'], 'name' ) );
			break;
		case 'overrun': // <booking_id> <minutes>
			$r   = \JustSimple\Booking\Booking\FieldService::record_actual_duration( (int) $argv[3], (int) $argv[4] );
			$out = array( 'ok' => true, 'conflicts' => $r['conflicting_booking_ids'], 'end' => $r['booking']['end'] );
			break;
		case 'complete':
			BookingService::transition( (int) $argv[3], 'completed', 'staff', 1 );
			$out = array( 'ok' => true );
			break;
		default:
			throw new InvalidArgumentException( "unknown action $action" );
	}
} catch ( EngineError $e ) {
	$out = array( 'ok' => false, 'code' => $e->error_code ) + ( isset( $e->data['minimum_spend'] ) ? array( 'minimum_spend' => $e->data['minimum_spend'] ) : array() );
}
echo wp_json_encode( array( 'action' => $action ) + $out ) . "\n";
