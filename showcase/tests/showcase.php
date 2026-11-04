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
use JustSimple\Showcase\Downloads;
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

$went = array( array( 0, 'open', '' ), array( 8, 'leave', '' ), array( 386, 'back', '' ), array( 405, 'hosted', '' ), array( 405, 'leave', '' ), array( 410, 'back', '' ), array( 417, 'leave', '' ) );
sc_check( 34 === Visits::on_screen( $went ), 'stayed counts the time on screen: 8 + 19 + 7 of 417 seconds', Visits::on_screen( $went ) );
$old = array( array( 0, 'open', '' ), array( 8, 'leave', '' ), array( 386, 'section', 'how' ), array( 405, 'leave', '' ) );
sc_check( 27 === Visits::on_screen( $old ), 'a visit logged before "came back" existed comes back at its next step', Visits::on_screen( $old ) );
$send( array( 'visit' => 'sctest0000007', 'steps' => array( array( 0, 'open', '' ), array( 8, 'leave', '' ), array( 20, 'back', '' ) ) ) );
sc_check( 3 === count( json_decode( $row( 'sctest0000007' )['steps'], true ) ), 'coming back is kept as a step' );

$totals = Visits::totals( time() - HOUR_IN_SECONDS );
sc_check( $totals['visits'] >= 5 && $totals['people'] >= 2 && $totals['booked'] >= 1 && $totals['downloaded'] >= 1, 'the totals add up', $totals );
require_once ABSPATH . 'wp-admin/includes/template.php';
wp_set_current_user( 1 );
ob_start();
JustSimple\Showcase\Admin::render();
$admin = ob_get_clean();
sc_check( false !== strpos( $admin, 'Visits to the marketing page' ) && false !== strpos( $admin, 'google.com' ) && false !== strpos( $admin, 'Booked at ' . DemoData::TITLES['workshop'] ), 'the admin page lists the visit and what it did' );
sc_check( false !== strpos( $admin, 'Came back' ) && false === strpos( $admin, 'Left the page' ), 'with the steps in plain words' );

$wpdb->update( $vt, array( 'started' => gmdate( 'Y-m-d H:i:s', time() - 91 * DAY_IN_SECONDS ) ), array( 'visit' => 'sctest0000002' ) );
do_action( Reset::HOOK );
sc_check( null === $row( 'sctest0000002' ), 'the nightly job removes visits older than ' . Visits::KEEP . ' days' );
sc_check( null !== $row( 'sctest0000001' ), 'and the reset leaves the others alone' );
$wpdb->query( "DELETE FROM $vt WHERE visit LIKE 'sctest%'" );

echo "== the plugin by emailed link\n";
$dt    = Downloads::table();
$site  = rtrim( $sc_args[2], '/' );
$sent  = array();
$fake  = static function ( $short, $atts ) use ( &$sent ) {
	$sent[] = $atts;
	return true; // stands in for a working mail service
};
add_filter( 'pre_wp_mail', $fake, 5, 2 );
$ask = static function ( $body ) {
	$_SERVER['HTTP_USER_AGENT'] = 'Mozilla/5.0 Chrome/130.0';
	$_SERVER['REMOTE_ADDR']     = '203.0.113.' . wp_rand( 10, 250 );
	$req                        = new WP_REST_Request( 'POST', '/justsimple-showcase/v1/download' );
	$req->set_header( 'content-type', 'application/json' );
	$req->set_body( wp_json_encode( $body ) );
	return rest_do_request( $req );
};
$email = 'sc-get-' . wp_rand( 100000, 999999 ) . '@example.test';
$drow  = static function ( $e ) use ( $wpdb, $dt ) {
	return $wpdb->get_row( $wpdb->prepare( "SELECT * FROM $dt WHERE email = %s", $e ), ARRAY_A );
};
wp_set_current_user( 0 );
$html = wp_remote_retrieve_body( wp_remote_get( $site . '/', array( 'timeout' => 20 ) ) );
sc_check( false !== strpos( $html, 'data-zip' ) && false === strpos( $html, '.zip" download' ) && false !== strpos( $html, '"get":"' ), 'the page\'s Download buttons no longer point at the zip' );
$zip = Downloads::file();
sc_check( '' !== $zip, 'the zip is in the private folder', $zip );
$pub = wp_remote_retrieve_response_code( wp_remote_get( plugins_url( 'private/' . basename( $zip ), JSS_FILE ), array( 'timeout' => 20 ) ) );
sc_check( 403 === $pub, 'and the web server refuses to serve it directly', $pub );
sc_check( 404 === wp_remote_retrieve_response_code( wp_remote_get( plugins_url( 'site/' . basename( $zip ), JSS_FILE ), array( 'timeout' => 20 ) ) ), 'the old public address is gone' );

sc_check( 400 === $ask( array( 'email' => 'not an email' ) )->get_status(), 'a bad address is refused' );
sc_check( 200 === $ask( array( 'email' => $email, 'website' => 'http://spam.test' ) )->get_status() && ! $sent && ! $drow( $email ), 'a robot filling the hidden field is told yes, and nothing is sent or kept' );
$res = $ask( array( 'email' => strtoupper( $email ) ) );
$got = $drow( $email );
sc_check( 200 === $res->get_status() && 1 === count( $sent ) && $email === $sent[0]['to'] && $got, 'an address gets one email and is kept in lower case', $res->get_data() );
sc_check( ! array_filter( MailCatcher::all(), static function ( $m ) use ( $email ) {
	return false !== strpos( $m['to'], $email );
} ), 'the download email is not caught, as booking emails are' );
preg_match( '/jss_download=([A-Za-z0-9]+)/', $sent[0]['message'] ?? '', $tok );
$link = $site . '/?jss_download=' . ( $tok[1] ?? '' );
sc_check( ! empty( $tok[1] ) && false === strpos( wp_json_encode( $got ), $tok[1] ), 'the email carries a private link, and only its hash is stored' );
$open = wp_remote_get( $link, array( 'timeout' => 30 ) );
sc_check( 200 === wp_remote_retrieve_response_code( $open ) && 'application/zip' === wp_remote_retrieve_header( $open, 'content-type' ) && strlen( wp_remote_retrieve_body( $open ) ) === filesize( $zip ) && 'PK' === substr( wp_remote_retrieve_body( $open ), 0, 2 ), 'the link downloads the whole zip', wp_remote_retrieve_headers( $open ) );
$got = $drow( $email );
sc_check( $got['opened'] && 1 === (int) $got['times'], 'and the address is marked opened' );
sc_check( 404 === wp_remote_retrieve_response_code( wp_remote_get( $site . '/?jss_download=nothing', array( 'timeout' => 20 ) ) ), 'a made-up link gets nothing' );
$wpdb->update( $dt, array( 'expires' => gmdate( 'Y-m-d H:i:s', time() - 60 ) ), array( 'email' => $email ) );
$late = wp_remote_get( $link, array( 'timeout' => 20 ) );
sc_check( 410 === wp_remote_retrieve_response_code( $late ) && 'PK' !== substr( wp_remote_retrieve_body( $late ), 0, 2 ), 'an old link has expired' );
$ask( array( 'email' => $email ) );
sc_check( 200 === wp_remote_retrieve_response_code( wp_remote_get( $site . '/?jss_download=' . ( preg_match( '/jss_download=([A-Za-z0-9]+)/', end( $sent )['message'], $t2 ) ? $t2[1] : '' ), array( 'timeout' => 30 ) ) ) && 404 === wp_remote_retrieve_response_code( wp_remote_get( $link, array( 'timeout' => 20 ) ) ), 'asking again gives a new link and ends the old one' );
$ask( array( 'email' => $email ) );
$before = count( $sent );
sc_check( 429 === $ask( array( 'email' => $email ) )->get_status() && count( $sent ) === $before, 'one address gets at most ' . Downloads::SENDS . ' emails a day' );
remove_filter( 'pre_wp_mail', $fake, 5 );
add_filter( 'pre_wp_mail', '__return_false', 99 ); // after any mail stub the test site has
$broken = 'sc-get-' . wp_rand( 100000, 999999 ) . '@example.test';
sc_check( 503 === $ask( array( 'email' => $broken ) )->get_status() && ! $drow( $broken ), 'if mail fails the visitor is told so, and nothing is kept' );
remove_filter( 'pre_wp_mail', '__return_false', 99 );

wp_set_current_user( 1 );
ob_start();
JustSimple\Showcase\Admin::render();
$admin = ob_get_clean();
sc_check( false !== strpos( $admin, $email ) && false !== strpos( $admin, 'Export as CSV' ) && false !== strpos( $admin, 'Send test' ), 'the admin page lists the address, with its export and a test email' );
$old = 'sc-get-old@example.test';
$wpdb->insert( $dt, array( 'email' => $old, 'token' => str_repeat( '0', 64 ), 'asked' => gmdate( 'Y-m-d H:i:s', time() - 31 * DAY_IN_SECONDS ), 'expires' => gmdate( 'Y-m-d H:i:s', time() - 24 * DAY_IN_SECONDS ) ) );
do_action( Reset::HOOK );
sc_check( ! $drow( $old ) && $drow( $email ), 'a reset keeps the list, and an address that never opened its link goes after ' . Downloads::FORGET . ' days' );
Downloads::remove( (int) $drow( $email )['id'] );
sc_check( ! $drow( $email ), 'the owner can delete an address' );
$wpdb->query( "DELETE FROM $dt WHERE email LIKE 'sc-get-%'" );

echo "\n" . ( $sc_fail ? "$sc_fail FAILED" : 'all passed' ) . "\n";
exit( $sc_fail ? 1 : 0 );
