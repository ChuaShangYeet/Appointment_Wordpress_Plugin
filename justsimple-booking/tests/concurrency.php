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
		case 'complete':
			BookingService::transition( (int) $argv[3], 'completed', 'staff', 1 );
			$out = array( 'ok' => true );
			break;
		default:
			throw new InvalidArgumentException( "unknown action $action" );
	}
} catch ( EngineError $e ) {
	$out = array( 'ok' => false, 'code' => $e->error_code );
}
echo wp_json_encode( array( 'action' => $action ) + $out ) . "\n";
