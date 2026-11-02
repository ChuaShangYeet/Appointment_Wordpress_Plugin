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
call( 'PATCH', "/admin/modules/$mid", array( 'status' => 'active', 'settings' => array( 'phone' => '+60 3-7781 2290', 'accent' => '#8a4458' ) ) );
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

// ------------------------------------------------------------------ the other four kinds, for the marketing page's demo

/** A fresh demo page of this title: the old one's live bookings are cancelled and it is deleted first. */
function demo_page( string $title, string $type, array $settings = array() ): array {
	global $wpdb;
	foreach ( $wpdb->get_col( $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'modules' ) . ' WHERE title = %s', $title ) ) as $old ) {
		$wpdb->query( $wpdb->prepare( 'UPDATE ' . Schema::table( 'bookings' ) . " SET status = 'cancelled' WHERE module_id = %d AND status IN ('pending','confirmed','in-progress','waiting-for-parts')", $old ) );
		call( 'DELETE', "/admin/modules/$old" );
	}
	$m = call( 'POST', '/admin/modules', array( 'title' => $title, 'type' => $type ) );
	call( 'PATCH', "/admin/modules/{$m['id']}", array( 'status' => 'active', 'settings' => $settings ) );
	return $m;
}
function hours( int $id, string $open, string $close, array $days = array( 0, 1, 2, 3, 4, 5, 6 ) ) {
	call( 'PUT', "/admin/modules/$id/hours", array( 'hours' => array_map( static function ( $d ) use ( $open, $close ) {
		return array( 'weekday' => $d, 'open_time' => $open, 'close_time' => $close );
	}, $days ) ) );
}
function person( int $id, string $name, array $sh, array $days = array( 1, 2, 3, 4, 5, 6 ), array $extra = array() ): int {
	$r = call( 'POST', "/admin/modules/$id/staff", array( 'name' => $name ) + $extra );
	call( 'PUT', "/admin/staff/{$r['id']}/shifts", array( 'shifts' => array_map( static function ( $d ) use ( $sh ) {
		return array( 'weekday' => $d, 'start_time' => $sh[0], 'end_time' => $sh[1], 'break_start' => $sh[2] ?? null, 'break_end' => $sh[3] ?? null );
	}, $days ) ) );
	return $r['id'];
}

// the week the marketing demo replays: the Monday two weeks ahead, kept so tests/record-demo.php captures the same days
$ref = new DateTimeImmutable( 'monday next week', $tz );
$ref = $ref->modify( '+7 days' );
update_option( 'jsb_demo_ref_monday', $ref->format( 'Y-m-d' ) );
$on  = static function ( int $day, string $hm ) use ( $ref ) {
	return $ref->modify( "+$day days" )->format( 'Y-m-d' ) . 'T' . $hm . ':00' . $ref->format( 'P' );
};
$book_at = static function ( int $mid, array $body ) {
	call( 'POST', "/modules/$mid/bookings", $body + array( 'customer' => array( 'name' => 'Demo guest', 'phone' => '+60 12-000 0000' ) ) );
};

// appointments: a clinic
$k  = demo_page( 'Klinik Dr Wong', 'staff', array( 'phone' => '+60 3-2142 8810', 'accent' => '#4f6238' ) );
hours( $k['id'], '09:00', '18:00', array( 1, 2, 3, 4, 5, 6 ) );
$ks = array();
foreach ( array( array( 'General consultation', 15, '45.00' ), array( 'Follow-up visit', 10, '30.00' ), array( 'Vaccination', 15, '80.00' ) ) as $x ) {
	$ks[] = call( 'POST', "/admin/modules/{$k['id']}/services", array( 'name' => $x[0], 'duration_minutes' => $x[1], 'price' => $x[2] ) )['id'];
}
$wong = person( $k['id'], 'Dr Wong', array( '09:00', '18:00', '13:00', '14:00' ) );
person( $k['id'], 'Dr Aisyah', array( '09:00', '13:00' ) );
$book_at( $k['id'], array( 'service_ids' => array( $ks[0] ), 'staff_id' => $wong, 'start' => $on( 1, '09:30' ) ) );
$book_at( $k['id'], array( 'service_ids' => array( $ks[0] ), 'staff_id' => $wong, 'start' => $on( 1, '10:15' ) ) );

// a restaurant with a VIP room
$r = demo_page( 'Rumah Makan Seri', 'restaurant', array( 'phone' => '+60 3-9284 1177', 'accent' => '#a63d2a' ) );
hours( $r['id'], '11:00', '22:30' );
foreach ( array( array( 'A1', 2 ), array( 'A2', 2 ), array( 'A3', 2 ), array( 'B1', 4 ), array( 'B2', 4 ), array( 'B3', 4 ), array( 'C1', 6 ), array( 'C2', 6 ) ) as $t ) {
	call( 'POST', "/admin/modules/{$r['id']}/tables", array( 'name' => $t[0], 'capacity' => $t[1] ) );
}
call( 'POST', "/admin/modules/{$r['id']}/tables", array( 'name' => 'Teratai', 'capacity' => 10, 'is_vip' => true, 'minimum_spend' => '800.00' ) );
call( 'PUT', "/admin/modules/{$r['id']}/seating", array( 'booking_mode' => 'flexible', 'slot_duration_minutes' => 90, 'slot_interval_minutes' => 30, 'first_slot_time' => '11:30', 'last_slot_time' => '21:00', 'buffer_minutes' => 15 ) );
$book_at( $r['id'], array( 'party_size' => 6, 'table_type' => 'normal', 'start' => $on( 4, '20:00' ) ) );
$book_at( $r['id'], array( 'party_size' => 6, 'table_type' => 'normal', 'start' => $on( 4, '19:30' ) ) );

// a workshop: mechanics, bays, the cars it services, a part on order
$w  = demo_page( 'Bengkel Ah Kow', 'workshop', array( 'phone' => '+60 3-7956 4402', 'accent' => '#3c5a73' ) );
hours( $w['id'], '08:30', '18:00', array( 1, 2, 3, 4, 5, 6 ) );
$ws = array();
foreach ( array( array( 'Oil and filter change', 45, '120.00' ), array( 'Brake pads, front', 90, '260.00' ), array( 'Aircon gas top-up', 60, '150.00' ) ) as $x ) {
	$ws[] = call( 'POST', "/admin/modules/{$w['id']}/services", array( 'name' => $x[0], 'duration_minutes' => $x[1], 'price' => $x[2] ) )['id'];
}
person( $w['id'], 'Ah Kow', array( '08:30', '18:00', '12:30', '13:30' ) );
person( $w['id'], 'Ravi', array( '08:30', '17:00' ) );
call( 'POST', "/admin/modules/{$w['id']}/bays", array( 'name' => 'Bay 1' ) );
call( 'POST', "/admin/modules/{$w['id']}/bays", array( 'name' => 'Bay 2' ) );
$cars = array();
foreach ( array( array( 'Perodua', 'Myvi', 2011, 2024 ), array( 'Perodua', 'Axia', 2014, 2024 ), array( 'Proton', 'Saga', 2016, 2024 ), array( 'Honda', 'City', 2014, 2024 ), array( 'Toyota', 'Vios', 2013, 2024 ) ) as $v ) {
	$cars[ $v[1] ] = call( 'POST', "/admin/modules/{$w['id']}/supported-vehicles", array( 'make' => $v[0], 'model' => $v[1], 'year_from' => $v[2], 'year_to' => $v[3] ) )['id'];
}
$pads = call( 'POST', "/admin/modules/{$w['id']}/products", array( 'name' => 'Brake pads (front set)', 'stock_status' => 'available_on_order', 'estimated_lead_time_days' => 3, 'price' => '140.00', 'stock_qty' => 0 ) );
call( 'PUT', "/admin/services/{$ws[1]}/parts", array( 'product_ids' => array( $pads['id'] ) ) );
$book_at( $w['id'], array( 'service_ids' => array( $ws[0] ), 'start' => $on( 2, '09:00' ), 'vehicle' => array( 'plate_no' => 'WXY 1234', 'supported_vehicle_id' => $cars['Myvi'], 'year' => 2019 ) ) );

// home services: visits at the customer's place, online calls
$f  = demo_page( 'CoolAir Services', 'field', array( 'phone' => '+60 12-388 4120', 'accent' => '#2c6e73', 'latitude' => 3.139, 'longitude' => 101.6869, 'service_radius_km' => 25 ) );
hours( $f['id'], '09:00', '18:00', array( 1, 2, 3, 4, 5, 6 ) );
call( 'POST', "/admin/modules/{$f['id']}/services", array( 'name' => 'Aircon check at your place', 'duration_minutes' => 45, 'price' => '60.00', 'location_type' => 'on_site', 'service_category' => 'inspection', 'travel_buffer_minutes' => 30 ) );
call( 'POST', "/admin/modules/{$f['id']}/services", array( 'name' => 'Aircon servicing', 'duration_minutes' => 90, 'price' => '150.00', 'location_type' => 'on_site', 'travel_buffer_minutes' => 30 ) );
call( 'POST', "/admin/modules/{$f['id']}/services", array( 'name' => 'Video call: is it worth fixing?', 'duration_minutes' => 20, 'price' => '0.00', 'location_type' => 'virtual' ) );
person( $f['id'], 'Faizal', array( '09:00', '18:00', '13:00', '14:00' ), array( 1, 2, 3, 4, 5, 6 ), array( 'meeting_link' => 'https://meet.example.test/faizal' ) );
person( $f['id'], 'Mei Yee', array( '10:00', '18:00' ), array( 1, 2, 3, 4, 5 ) );

echo 'demo week from ', $ref->format( 'Y-m-d' ), ": clinic {$k['id']}, restaurant {$r['id']}, workshop {$w['id']}, home services {$f['id']}\n";

