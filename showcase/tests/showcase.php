<?php
/**
 * Integration checks for the JustSimple Showcase on a LOCAL test site (it resets demo data and takes over the front page).
 *
 *   php showcase/tests/showcase.php C:/xampp/htdocs/jsb-test http://localhost/jsb-test
 */
$sc_args = $argv; // before WordPress loads: none of its globals may be overwritten
if ( PHP_SAPI !== 'cli' || empty( $sc_args[2] ) ) {
	fwrite( STDERR, "usage: php showcase/tests/showcase.php <wordpress-root> <site-url>\n" );
	exit( 1 );
}
require rtrim( $sc_args[1], '/\\' ) . '/wp-load.php';
require_once ABSPATH . 'wp-admin/includes/plugin.php';

use JustSimple\Booking\Data\Schema;
use JustSimple\Showcase\DemoData;
use JustSimple\Showcase\Front;
use JustSimple\Showcase\MailCatcher;
use JustSimple\Showcase\Reset;

$sc_fail = 0;
function sc_check( bool $ok, string $what, $detail = null ) {
	global $sc_fail;
	echo ( $ok ? '  ok   ' : '  FAIL ' ), $what, ( $ok || null === $detail ? '' : "\n       " . wp_json_encode( $detail ) ), "\n";
	$sc_fail += $ok ? 0 : 1;
}
global $wpdb;

echo "== activation\n";
if ( ! is_plugin_active( 'justsimple-showcase/justsimple-showcase.php' ) ) {
	$r = activate_plugin( 'justsimple-showcase/justsimple-showcase.php' );
	sc_check( ! is_wp_error( $r ), 'the Showcase activates beside the booking plugin', is_wp_error( $r ) ? $r->get_error_message() : null );
	include_once WP_PLUGIN_DIR . '/justsimple-showcase/justsimple-showcase.php';
	do_action( 'plugins_loaded' );
}
$next = wp_next_scheduled( Reset::HOOK );
sc_check( $next && '03:00' === wp_date( 'H:i', $next ) && $next > time() && $next <= time() + DAY_IN_SECONDS, 'the nightly reset is scheduled for the next 03:00 in the site\'s clock', $next ? wp_date( 'c', $next ) : null );

echo "== load the demo\n";
$out = Reset::run();
sc_check( $out && 5 === count( $out['pages'] ) && ! $out['errors'], 'five demo booking pages, built through the booking routes without a refusal', $out );
$pages = (array) get_option( Reset::PAGES );
$types = array();
foreach ( $pages as $kind => $id ) {
	$types[ $kind ] = $wpdb->get_var( $wpdb->prepare( 'SELECT type FROM ' . Schema::table( 'modules' ) . " WHERE id = %d AND status = 'active'", $id ) );
}
sc_check( array( 'staff' => 'staff', 'salon' => 'salon', 'restaurant' => 'restaurant', 'workshop' => 'workshop', 'field' => 'field' ) === $types, 'each is the right kind, and switched on', $types );
$booked = (int) $wpdb->get_var( 'SELECT COUNT(*) FROM ' . Schema::table( 'bookings' ) . ' WHERE module_id IN (' . implode( ',', array_map( 'intval', $pages ) ) . ") AND status IN ('pending','confirmed')" );
sc_check( $booked >= 5, "the demo comes with bookings in the coming days ($booked)" );

echo "== a visitor books, then the night comes\n";
wp_set_current_user( 0 );
add_filter( 'jsb_rate_limit', '__return_zero' );
$sid   = $pages['clinic'] ?? $pages['staff'];
$svc   = (int) $wpdb->get_var( $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'services' ) . ' WHERE module_id = %d ORDER BY id LIMIT 1', $sid ) );
$day   = ( new DateTimeImmutable( 'today +3 days', wp_timezone() ) );
if ( 0 === (int) $day->format( 'w' ) ) {
	$day = $day->modify( '+1 day' ); // the clinic is closed on Sundays
}
$req = new WP_REST_Request( 'GET', "/justsimple/v1/modules/$sid/availability" );
$req->set_query_params( array( 'date' => $day->format( 'Y-m-d' ), 'services' => $svc ) );
$slot = rest_do_request( $req )->get_data()['slots'][0] ?? null;
$req  = new WP_REST_Request( 'POST', "/justsimple/v1/modules/$sid/bookings" );
$req->set_header( 'content-type', 'application/json' );
$req->set_body( wp_json_encode( array( 'service_ids' => array( $svc ), 'start' => $slot['start'] ?? '', 'customer' => array( 'name' => 'Visitor Tester', 'phone' => '+60 19-777 0001', 'email' => 'visitor@example.test' ) ) ) );
$res = rest_do_request( $req );
sc_check( 201 === $res->get_status(), 'a visitor books on the live demo', $res->get_data() );
$mail = MailCatcher::all();
sc_check( (bool) array_filter( $mail, static function ( $m ) {
	return false !== strpos( $m['to'], 'visitor@example.test' );
} ), 'the confirmation email was caught, not sent', array_slice( $mail, 0, 3 ) );
$cust = (int) $wpdb->get_var( $wpdb->prepare( 'SELECT COUNT(*) FROM ' . Schema::table( 'customers' ) . ' WHERE phone LIKE %s', '%197770001%' ) );
sc_check( 1 === $cust, 'the visitor is a customer record now' );
wp_set_current_user( 1 );
do_action( Reset::HOOK ); // what WP-Cron runs at 03:00
$cust = (int) $wpdb->get_var( $wpdb->prepare( 'SELECT COUNT(*) FROM ' . Schema::table( 'customers' ) . ' WHERE phone LIKE %s', '%197770001%' ) );
sc_check( 0 === $cust, 'after the nightly reset the visitor, their booking and their details are gone' );
$new = (array) get_option( Reset::PAGES );
sc_check( 5 === count( $new ) && ! array_intersect( $new, $pages ), 'and five fresh booking pages replace the old ones', array( $pages, $new ) );
sc_check( ! array_filter( MailCatcher::all(), static function ( $m ) {
	return false !== strpos( $m['to'], 'visitor@example.test' );
} ), 'the caught mail list forgets the visitor too' );
$old = (int) $wpdb->get_var( 'SELECT COUNT(*) FROM ' . Schema::table( 'modules' ) . ' WHERE id IN (' . implode( ',', array_map( 'intval', $pages ) ) . ')' );
sc_check( 0 === $old, 'the old demo pages are deleted, not left switched off' );
sc_check( null === get_transient( Reset::LOCK ) || false === get_transient( Reset::LOCK ), 'no reset lock is left behind' );

echo "== the front page\n";
$html = wp_remote_retrieve_body( wp_remote_get( rtrim( $sc_args[2], '/' ) . '/', array( 'timeout' => 20 ) ) );
sc_check( false !== strpos( $html, 'Book any' ) && false !== strpos( $html, 'window.jsbShowcase' ), 'the front page is the marketing page, with the live demo\'s address', substr( strip_tags( $html ), 0, 200 ) );
preg_match( '#window\.jsbShowcase = (\{.*?\});#', $html, $cfg );
$cfg = json_decode( $cfg[1] ?? 'null', true );
sc_check( $cfg && (array) $cfg['pages'] == array_map( 'intval', $new ) && false !== strpos( $cfg['api'], '/wp-json/justsimple/v1' ), 'it names the fresh demo pages and the API', $cfg );
sc_check( false === strpos( $html, '{{assets}}' ) && false !== strpos( $html, '/justsimple-showcase/site/page.js' ), 'its files are served from the Showcase' );
update_option( Front::ENABLED, '0' );
$html = wp_remote_retrieve_body( wp_remote_get( rtrim( $sc_args[2], '/' ) . '/', array( 'timeout' => 20 ) ) );
sc_check( false === strpos( $html, 'window.jsbShowcase' ), 'switched off, WordPress\'s own front page is back' );
update_option( Front::ENABLED, '1' );

echo "\n" . ( $sc_fail ? "$sc_fail FAILED" : 'all passed' ) . "\n";
exit( $sc_fail ? 1 : 0 );
