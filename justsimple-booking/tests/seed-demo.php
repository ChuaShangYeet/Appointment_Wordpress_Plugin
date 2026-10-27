<?php
/**
 * A believable salon day on a local test site, for looking at the admin screens (and 7c's interface pictures).
 *
 *   php tests/seed-demo.php C:/xampp/htdocs/jsb-test
 *
 * Everything goes through the plugin's own REST routes, so the engine allocates every booking: nothing is written around it.
 * Re-running replaces the previous demo module. A WordPress user "jsb-demo-stylist" is linked to the stylist Priya; its
 * password is generated once and saved to dev/local-test-site.env (gitignored), never printed.
 * Never run this against a real site.
 */

if ( PHP_SAPI !== 'cli' || empty( $argv[1] ) ) {
	fwrite( STDERR, "usage: php tests/seed-demo.php <wordpress-root>\n" );
	exit( 1 );
}
require rtrim( $argv[1], '/\\' ) . '/wp-load.php';

use JustSimple\Booking\Data\Schema;

add_filter( 'jsb_rate_limit', '__return_zero' );
const TITLE = 'Mei Hair Studio';

function call( string $method, string $path, ?array $body = null ) {
	$req = new WP_REST_Request( $method, '/justsimple/v1' . $path );
	if ( null !== $body ) {
		$req->set_header( 'content-type', 'application/json' );
		$req->set_body( wp_json_encode( $body ) );
	}
	$res = rest_do_request( $req );
	$out = json_decode( wp_json_encode( $res->get_data() ), true );
	if ( $res->get_status() >= 400 ) {
		fwrite( STDERR, "$method $path: " . $res->get_status() . ' ' . wp_json_encode( $out ) . "\n" );
	}
	return $out;
}

global $wpdb;
wp_set_current_user( 1 );
$tz = wp_timezone();

// the previous demo: cancel what is still upcoming, then delete it
foreach ( $wpdb->get_col( $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'modules' ) . ' WHERE title = %s', TITLE ) ) as $old ) {
	foreach ( $wpdb->get_col( $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'bookings' ) . " WHERE module_id = %d AND status IN ('pending','confirmed','in-progress','waiting-for-parts')", $old ) ) as $b ) {
		$wpdb->update( Schema::table( 'bookings' ), array( 'status' => 'cancelled' ), array( 'id' => $b ) );
	}
	call( 'DELETE', "/admin/modules/$old" );
}

$m   = call( 'POST', '/admin/modules', array( 'title' => TITLE, 'type' => 'salon' ) );
$mid = $m['id'];
call( 'PATCH', "/admin/modules/$mid", array( 'status' => 'active', 'settings' => array( 'phone' => '+60 3-7781 2290' ) ) );
call( 'PUT', "/admin/modules/$mid/hours", array( 'hours' => array_map( static function ( $d ) {
	return array( 'weekday' => $d, 'open_time' => '10:00', 'close_time' => '21:00' );
}, range( 0, 6 ) ) ) );
// walk-ins welcome: no notice needed, so the demo day can start now
call( 'PATCH', "/admin/policies/{$m['default_policy_id']}", array( 'min_notice_booking_minutes' => 0 ) );
$ask = call( 'POST', "/admin/modules/$mid/policies", array( 'name' => 'Colour: we confirm first', 'default_status' => 'pending', 'pending_timeout_minutes' => 180, 'min_notice_booking_minutes' => 0, 'min_notice_cancel_minutes' => 1440 ) );

$sv = array();
foreach ( array(
	'cut'    => array( 'Haircut', 45, '45.00', false, null ),
	'wash'   => array( 'Wash and blow-dry', 30, '30.00', false, null ),
	'colour' => array( 'Colour', 90, '180.00', false, $ask['id'] ),
	'gel'    => array( 'Gel finish', 15, '15.00', true, null ),
	'treat'  => array( 'Scalp treatment', 30, '60.00', true, null ),
) as $k => $x ) {
	$sv[ $k ] = call( 'POST', "/admin/modules/$mid/services", array( 'name' => $x[0], 'duration_minutes' => $x[1], 'price' => $x[2], 'is_addon' => $x[3], 'policy_id' => $x[4] ) )['id'];
}
$staff = array();
foreach ( array( 'Mei Ling' => array( '10:00', '19:00', '14:00', '14:45' ), 'Aishah' => array( '10:00', '19:00', '13:30', '14:15' ), 'Priya' => array( '12:00', '21:00', '16:00', '16:30' ), 'Jason' => array( '10:00', '18:00', null, null ) ) as $name => $sh ) {
	$r = call( 'POST', "/admin/modules/$mid/staff", array( 'name' => $name ) );
	call( 'PUT', "/admin/staff/{$r['id']}/shifts", array( 'shifts' => array_map( static function ( $d ) use ( $sh ) {
		return array( 'weekday' => $d, 'start_time' => $sh[0], 'end_time' => $sh[1], 'break_start' => $sh[2], 'break_end' => $sh[3] );
	}, range( 0, 6 ) ) ) );
	$staff[ $name ] = $r['id'];
}

// the day: today if most of it is still ahead, otherwise tomorrow
$t0  = (int) ( ceil( ( time() + 20 * 60 ) / 900 ) * 900 );
$day = ( new DateTimeImmutable( '@' . $t0 ) )->setTimezone( $tz );
if ( (int) $day->format( 'G' ) >= 17 ) {
	$day = ( new DateTimeImmutable( 'tomorrow 10:30', $tz ) );
	$t0  = $day->getTimestamp();
}
call( 'POST', "/admin/modules/$mid/days-off", array( 'name' => 'Annual leave', 'resource_id' => $staff['Jason'], 'start_date' => $day->format( 'Y-m-d' ), 'end_date' => $day->format( 'Y-m-d' ) ) );

$at = static function ( int $minutes ) use ( $t0, $tz ) {
	return ( new DateTimeImmutable( '@' . ( $t0 + $minutes * 60 ) ) )->setTimezone( $tz )->format( DATE_ATOM );
};
$book = static function ( array $svc, string $who, int $minutes, string $name, string $phone, string $request = '' ) use ( $mid, $sv, $staff, $at, $day ) {
	$ids = array_map( static function ( $k ) use ( $sv ) {
		return $sv[ $k ];
	}, $svc );
	// the first time the engine offers this stylist at or after the wanted one (breaks and other bookings move it on)
	$want  = $at( $minutes );
	$req   = new WP_REST_Request( 'GET', "/justsimple/v1/modules/$mid/availability" );
	$req->set_query_params( array( 'date' => $day->format( 'Y-m-d' ), 'services' => implode( ',', $ids ), 'staff' => $staff[ $who ] ) );
	$slots = rest_do_request( $req )->get_data()['slots'] ?? array();
	$slot  = current( array_filter( $slots, static function ( $s ) use ( $want ) {
		return strtotime( $s['start'] ) >= strtotime( $want );
	} ) );
	if ( ! $slot ) {
		fwrite( STDERR, "no time left for $name with $who\n" );
		return 0;
	}
	$r = call( 'POST', "/modules/$mid/bookings", array(
		'service_ids'     => $ids,
		'staff_id'        => $staff[ $who ],
		'start'           => $slot['start'],
		'customer'        => array( 'name' => $name, 'phone' => $phone, 'email' => strtolower( strtok( $name, ' ' ) ) . '@example.test' ),
		'special_request' => $request,
	) );
	return isset( $r['booking']['code'] ) ? (int) $GLOBALS['wpdb']->get_var( $GLOBALS['wpdb']->prepare( 'SELECT id FROM ' . Schema::table( 'bookings' ) . ' WHERE code = %s', $r['booking']['code'] ) ) : 0;
};
$status = static function ( int $id, string $to ) {
	if ( $id ) {
		call( 'PATCH', "/admin/bookings/$id/status", array( 'status' => $to ) );
	}
};

$a = $book( array( 'wash' ), 'Aishah', 0, 'Siti Rahmah', '+60 12-310 4471' );
$status( $a, 'completed' );
$b = $book( array( 'cut', 'gel' ), 'Mei Ling', 0, 'Nurul Huda Ismail', '+60 17-552 0193' );
$status( $b, 'in-progress' );
$book( array( 'cut' ), 'Priya', 15, 'Daniel Tan', '+60 16-221 8830' );
$book( array( 'colour' ), 'Aishah', 45, 'Farah Aziz', '+60 11-2645 7720', 'Going a shade lighter than last time' );
$book( array( 'cut', 'treat' ), 'Mei Ling', 75, 'Kavitha Raj', '+60 12-907 3315' );
$book( array( 'wash' ), 'Priya', 90, 'Lim Wei Jie', '+60 19-334 6012' );
$book( array( 'colour' ), 'Priya', 150, 'Amirah Zainal', '+60 13-480 2257' );
$book( array( 'cut' ), 'Mei Ling', 165, 'Hafiz Rahman', '+60 17-660 9184' );
$book( array( 'cut', 'gel' ), 'Aishah', 165, 'Grace Wong', '+60 12-118 5520' );
$c = $book( array( 'wash' ), 'Priya', 255, 'Sarah Ong', '+60 16-772 0418' );
$status( $c, 'cancelled' );

// the stylist login for "own appointments only"
$login = 'jsb-demo-stylist';
$user  = get_user_by( 'login', $login );
if ( ! $user ) {
	$pass = wp_generate_password( 20, false );
	$uid  = wp_insert_user( array( 'user_login' => $login, 'user_pass' => $pass, 'role' => 'editor', 'user_email' => 'priya@example.test', 'display_name' => 'Priya' ) );
	$env  = dirname( __DIR__, 2 ) . '/dev/local-test-site.env';
	file_put_contents( $env, "JSB_TEST_STYLIST_USER=$login\nJSB_TEST_STYLIST_PASS=$pass\n", FILE_APPEND );
} else {
	$uid = $user->ID;
}
call( 'PATCH', "/admin/staff/{$staff['Priya']}", array( 'wp_user_id' => $uid ) );

echo 'module ', $mid, ' on ', $day->format( 'Y-m-d' ), ', ', (int) $wpdb->get_var( $wpdb->prepare( 'SELECT COUNT(*) FROM ' . Schema::table( 'bookings' ) . ' WHERE module_id = %d', $mid ) ), " bookings\n";
