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
use JustSimple\Showcase\Visits;

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

echo "== the visit log\n";
$vt   = Visits::table();
$send = static function ( $body, $agent = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36' ) {
	$_SERVER['HTTP_USER_AGENT'] = $agent;
	$_SERVER['REMOTE_ADDR']     = '203.0.113.9';
	$req                        = new WP_REST_Request( 'POST', '/justsimple-showcase/v1/visit' );
	$req->set_header( 'content-type', 'application/json' );
	$req->set_body( is_string( $body ) ? $body : wp_json_encode( $body ) );
	return rest_do_request( $req )->get_status();
};
$row  = static function ( $visit ) use ( $wpdb, $vt ) {
	return $wpdb->get_row( $wpdb->prepare( "SELECT * FROM $vt WHERE visit = %s", $visit ), ARRAY_A );
};
$wpdb->query( "DELETE FROM $vt WHERE visit LIKE 'sctest%'" );
wp_set_current_user( 1 );
sc_check( '' === Visits::endpoint(), 'a site admin is not logged' );
wp_set_current_user( 0 );
sc_check( false !== strpos( Visits::endpoint(), '/justsimple-showcase/v1/visit' ), 'a visitor is' );
$front = wp_remote_get( rtrim( $sc_args[2], '/' ) . '/', array( 'timeout' => 20 ) );
$html  = wp_remote_retrieve_body( $front );
sc_check( false !== strpos( $html, '"track":"' ) && false !== strpos( $html, 'without cookies' ), 'the page is told where to send its visit, and says it counts without cookies' );
sc_check( ! wp_remote_retrieve_header( $front, 'set-cookie' ), 'the front page sets no cookie', wp_remote_retrieve_header( $front, 'set-cookie' ) );

$status = $send( array( 'visit' => 'sctest0000001', 'from' => 'https://www.google.com/search?q=private+words', 'steps' => array( array( 0, 'open', '' ), array( 3, 'section', 'try' ), array( 5, 'business', 'workshop' ) ) ) );
$v      = $row( 'sctest0000001' );
sc_check( 204 === $status && $v && 'google.com' === $v['came_from'], 'a visit is saved, with only the name of the site it came from', $v );
sc_check( 'Phone · Chrome · Android' === $v['device'] && 'try' === $v['reached'], 'with the kind of device and how far it got', $v );
sc_check( 16 === strlen( $v['person'] ) && false === strpos( wp_json_encode( $v ), '203.0.113.9' ), 'the IP address is not kept' );
$send( array( 'visit' => 'sctest0000001', 'steps' => array( array( 40, 'section', 'questions' ), array( 41, 'section', 'try' ), array( 60, 'booked', 'workshop' ), array( 70, 'download', '' ) ) ) );
$v2 = $row( 'sctest0000001' );
sc_check( 7 === count( json_decode( $v2['steps'], true ) ) && 'questions' === $v2['reached'] && 1 === (int) $v2['booked'] && 1 === (int) $v2['downloaded'], 'later steps join the same visit: the furthest section, a booking, a download', $v2 );
sc_check( 'google.com' === $v2['came_from'] && $v['person'] === $v2['person'], 'and leave where it came from alone' );

$send( array( 'visit' => 'sctest0000002', 'from' => home_url( '/x' ), 'steps' => array( array( 1, 'name', 'Ali' ), array( 2, 'business', '<script>' ), array( 'x', 'open', '' ) ) ) );
$v = $row( 'sctest0000002' );
sc_check( '[[2,"business",""],[0,"open",""]]' === $v['steps'], 'unknown events are dropped and unknown details blanked', $v['steps'] ?? null );
sc_check( '' === $v['came_from'], 'coming from this site counts as direct' );
$send( array( 'visit' => 'sctest0000003', 'from' => 'utm:Newsletter', 'steps' => array( array( 0, 'open', '' ) ) ) );
sc_check( 'campaign: newsletter' === $row( 'sctest0000003' )['came_from'], 'a campaign tag is kept' );
sc_check( 400 === $send( array( 'visit' => "x' OR 1", 'steps' => array() ) ) && 400 === $send( 'not json' ), 'a bad visit id or body is refused' );
$send( array( 'visit' => 'sctest0000004', 'steps' => array( array( 0, 'open', '' ) ) ), 'Mozilla/5.0 (compatible; Googlebot/2.1)' );
sc_check( null === $row( 'sctest0000004' ), 'bots are not logged' );
$many = array_fill( 0, 50, array( 9, 'section', 'try' ) );
for ( $i = 0; $i < 6; $i++ ) {
	$send( array( 'visit' => 'sctest0000005', 'steps' => $many ) );
}
sc_check( Visits::STEPS === count( json_decode( $row( 'sctest0000005' )['steps'], true ) ), 'one visit keeps at most ' . Visits::STEPS . ' steps' );

$same = $row( 'sctest0000002' )['person'] === $row( 'sctest0000003' )['person'];
delete_option( Visits::SALT );
$send( array( 'visit' => 'sctest0000006', 'steps' => array( array( 0, 'open', '' ) ) ) );
sc_check( $same && $row( 'sctest0000006' )['person'] !== $row( 'sctest0000003' )['person'], 'one person is one code all day, and a new code when the day\'s key changes' );

$totals = Visits::totals( time() - HOUR_IN_SECONDS );
sc_check( $totals['visits'] >= 5 && $totals['people'] >= 2 && $totals['booked'] >= 1 && $totals['downloaded'] >= 1, 'the totals add up', $totals );
require_once ABSPATH . 'wp-admin/includes/template.php';
wp_set_current_user( 1 );
ob_start();
JustSimple\Showcase\Admin::render();
$admin = ob_get_clean();
sc_check( false !== strpos( $admin, 'Visits to the marketing page' ) && false !== strpos( $admin, 'google.com' ) && false !== strpos( $admin, 'Booked at ' . DemoData::TITLES['workshop'] ), 'the admin page lists the visit and what it did' );

$wpdb->update( $vt, array( 'started' => gmdate( 'Y-m-d H:i:s', time() - 91 * DAY_IN_SECONDS ) ), array( 'visit' => 'sctest0000002' ) );
do_action( Reset::HOOK );
sc_check( null === $row( 'sctest0000002' ), 'the nightly job removes visits older than ' . Visits::KEEP . ' days' );
sc_check( null !== $row( 'sctest0000001' ), 'and the reset leaves the others alone' );
$wpdb->query( "DELETE FROM $vt WHERE visit LIKE 'sctest%'" );

echo "\n" . ( $sc_fail ? "$sc_fail FAILED" : 'all passed' ) . "\n";
exit( $sc_fail ? 1 : 0 );
