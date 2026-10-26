<?php
/**
 * End-to-end check against a real WordPress + MySQL/MariaDB, through the REST routes (rest_do_request).
 *
 *   php tests/integration.php <wordpress-root>            full run; prints a race-test fixture as the last line
 *
 * It creates its own modules and leaves them in place (they are the race test's fixture). Never point it at a live site.
 */

if ( empty( $argv[1] ) || ! is_file( rtrim( $argv[1], '/\\' ) . '/wp-load.php' ) ) {
	fwrite( STDERR, "usage: php tests/integration.php <wordpress-root>\n" );
	exit( 2 );
}
$_SERVER['HTTP_HOST'] = 'localhost';
require rtrim( $argv[1], '/\\' ) . '/wp-load.php';

use JustSimple\Booking\Data\Schema;

// Public routes are rate limited per IP; every CLI request comes from the same "unknown" address. The limit itself is
// checked by the race script over real HTTP.
add_filter( 'jsb_rate_limit', '__return_zero' );

$failures = 0;
function check( bool $cond, string $what, $detail = null ) {
	global $failures;
	echo ( $cond ? '  ok   ' : '  FAIL ' ) . $what . ( $cond || null === $detail ? '' : "\n       " . wp_json_encode( $detail ) ) . "\n";
	if ( ! $cond ) {
		$failures++;
	}
}
/** @return array{0:int,1:mixed} */
function call( string $method, string $path, ?array $body = null, array $query = array() ) {
	$req = new WP_REST_Request( $method, '/justsimple/v1' . $path );
	if ( null !== $body ) {
		$req->set_header( 'content-type', 'application/json' );
		$req->set_body( wp_json_encode( $body ) );
	}
	$req->set_query_params( $query );
	$res = rest_do_request( $req );
	return array( $res->get_status(), json_decode( wp_json_encode( $res->get_data() ), true ) );
}
function as_user( int $id ) {
	wp_set_current_user( $id );
}
function mails(): array {
	$f = WP_CONTENT_DIR . '/jsb-mail.log';
	return is_file( $f ) ? array_map( 'json_decode', file( $f, FILE_IGNORE_NEW_LINES ) ) : array();
}

global $wpdb;
update_option( 'timezone_string', 'Asia/Kuala_Lumpur' );
@unlink( WP_CONTENT_DIR . '/jsb-mail.log' );
$tz       = wp_timezone();
$tomorrow = ( new DateTimeImmutable( 'today +2 days', $tz ) )->format( 'Y-m-d' ); // two days out: the 24-hour cancel notice is still open
$at       = static function ( string $hm ) use ( $tomorrow, $tz ) {
	return ( new DateTimeImmutable( "$tomorrow $hm", $tz ) )->format( DATE_ATOM );
};

echo "== setup through the admin routes\n";
as_user( 0 );
list( $s ) = call( 'GET', '/admin/modules' );
check( 401 === $s, 'logged out: admin routes refused (401)', $s );

as_user( 1 );
list( $s, $d ) = call( 'POST', '/admin/modules', array( 'title' => 'Dinner', 'type' => 'restaurant' ) );
check( 422 === $s, 'restaurant modules refused in this release', $d );
list( $s, $m ) = call( 'POST', '/admin/modules', array( 'title' => 'Test Spa', 'type' => 'staff' ) );
check( 201 === $s && 'draft' === $m['status'] && $m['default_policy_id'], 'module created as draft with a default policy', $m );
$mid = $m['id'];
list( $s ) = call( 'GET', "/modules/$mid" );
check( 404 === $s, 'a draft module is not public' );
list( $s, $m ) = call( 'PATCH', "/admin/modules/$mid", array( 'status' => 'active', 'settings' => array( 'notify_email' => 'owner@example.test' ) ) );
check( 200 === $s && 'active' === $m['status'] && 'owner@example.test' === $m['settings']['notify_email'], 'module activated', $m );

list( $s, $svc ) = call( 'POST', "/admin/modules/$mid/services", array( 'name' => 'Massage', 'duration_minutes' => 60, 'price' => '120' ) );
check( 201 === $s && '120.00' === $svc['price'], 'service created', $svc );
list( $s, $d ) = call( 'POST', "/admin/modules/$mid/services", array( 'name' => 'Bad', 'duration_minutes' => 2 ) );
check( 422 === $s && 'duration_minutes' === $d['data']['field'], 'service validation names the field', $d );

$staff = array();
foreach ( array( 'Mei', 'Lin' ) as $name ) {
	list( $s, $r ) = call( 'POST', "/admin/modules/$mid/staff", array( 'name' => $name ) );
	$staff[ $name ] = $r['id'];
	$week           = array_map( static function ( $d ) {
		return array( 'weekday' => $d, 'start_time' => '09:00', 'end_time' => '18:00', 'break_start' => '13:00', 'break_end' => '14:00' );
	}, range( 0, 6 ) );
	list( $s, $r ) = call( 'PUT', "/admin/staff/{$staff[$name]}/shifts", array( 'shifts' => $week ) );
	check( 200 === $s && 7 === count( $r ), "$name: staff and a 7-day week of shifts", $r );
}
list( $s, $d ) = call( 'PUT', "/admin/staff/{$staff['Mei']}/shifts", array( 'shifts' => array( array( 'weekday' => 1, 'start_time' => '09:00', 'end_time' => '12:00', 'break_start' => '13:00', 'break_end' => '14:00' ) ) ) );
check( 422 === $s, 'a break outside the shift is refused', $d );
list( , $r ) = call( 'GET', "/admin/staff/{$staff['Mei']}/shifts" );
check( 7 === count( $r ), 'a refused week leaves the old shifts alone' );

list( $s, $pol ) = call( 'POST', "/admin/modules/$mid/policies", array( 'name' => 'Approval', 'default_status' => 'pending', 'pending_timeout_minutes' => 30, 'min_notice_booking_minutes' => 0, 'min_notice_cancel_minutes' => 0, 'booking_window_days' => 30 ) );
list( $s, $consult ) = call( 'POST', "/admin/modules/$mid/services", array( 'name' => 'Consultation', 'duration_minutes' => 30, 'policy_id' => $pol['id'] ) );
check( 201 === $s && $consult['policy_id'] === $pol['id'], 'service with its own approval policy', $consult );

echo "== public reads\n";
as_user( 0 );
list( $s, $pub ) = call( 'GET', "/modules/$mid" );
check( 200 === $s && 2 === count( $pub['services'] ) && 2 === count( $pub['staff'] ) && ! isset( $pub['staff'][0]['contact_email'] ), 'module info: services and staff, no contact details', $pub );
list( $s, $av ) = call( 'GET', "/modules/$mid/availability", null, array( 'date' => $tomorrow, 'services' => (string) $svc['id'] ) );
$first = $av['slots'][0]['start'] ?? null;
check( 200 === $s && $first === $at( '09:00' ) && $av['slots'][0]['staff_ids'] === array( $staff['Mei'], $staff['Lin'] ), 'tomorrow starts 09:00 with both staff', $av['slots'][0] ?? $av );
$starts = wp_list_pluck( $av['slots'], 'start' );
check( ! in_array( $at( '12:30' ), $starts, true ) && in_array( $at( '14:00' ), $starts, true ), 'lunch break respected' );
list( $s, $mo ) = call( 'GET', "/modules/$mid/availability/month", null, array( 'month' => substr( $tomorrow, 0, 7 ), 'services' => (string) $svc['id'] ) );
check( 200 === $s && in_array( $tomorrow, $mo['dates'], true ), 'month view lists tomorrow', $mo );
list( $s ) = call( 'GET', "/modules/$mid/availability", null, array( 'date' => '2026-02-30', 'services' => (string) $svc['id'] ) );
check( 422 === $s, 'impossible date refused' );

echo "== booking\n";
$body = static function ( $start, $staff_id = null, $phone = '+60123456701', array $extra = array() ) use ( $svc ) {
	return array_merge( array(
		'service_ids' => array( $svc['id'] ),
		'staff_id'    => $staff_id,
		'start'       => $start,
		'customer'    => array( 'name' => 'Aisha', 'phone' => $phone, 'email' => 'aisha@example.test' ),
	), $extra );
};
list( $s, $b1 ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '10:00' ), $staff['Mei'] ) );
check( 201 === $s && 'confirmed' === $b1['booking']['status'] && 40 === strlen( $b1['manage_token'] ) && 'Mei' === $b1['booking']['staff'][0]['name'], 'booked Mei at 10:00, confirmed', $b1 );
check( ! isset( $b1['booking']['customer']['phone'] ), 'public response has no phone number' );
list( $s, $d ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '10:00' ), $staff['Mei'], '+60123456702' ) );
check( 409 === $s && 'slot_unavailable' === $d['code'] && ! empty( $d['data']['slots'] ), 'Mei at 10:00 again: 409 with fresh slots', $d );
list( $s, $b2 ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '10:00' ), null, '+60123456703' ) );
check( 201 === $s && 'Lin' === $b2['booking']['staff'][0]['name'], 'any available at 10:00 goes to Lin', $b2 );
list( $s, $d ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '10:00' ), null, '+60123456704' ) );
check( 409 === $s, 'third booking at 10:00: nobody left', $d );

$key = 'test-' . wp_generate_password( 12, false );
list( $s, $i1 ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '15:00' ), null, '+60123456705', array( 'idempotency_key' => $key ) ) );
list( $s2, $i2 ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '15:00' ), null, '+60123456705', array( 'idempotency_key' => $key ) ) );
check( 201 === $s && 200 === $s2 && $i1['booking']['code'] === $i2['booking']['code'] && $i2['replayed'] && $i1['manage_token'] === $i2['manage_token'], 'same key, same request: the first booking again (200)', array( $s, $s2 ) );
list( $s, $d ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '16:00' ), null, '+60123456705', array( 'idempotency_key' => $key ) ) );
check( 409 === $s && 'idempotency_reused' === $d['code'], 'same key, different request: 409', $d );

list( $s, $d ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '11:00' ), null, '+60123456706', array( 'health_notes' => 'asthma' ) ) );
check( 422 === $s && 'health_notes' === $d['data']['field'], 'health notes refused until encryption exists', $d );
list( $s ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '11:00' ), null, '+60123456706', array( 'website' => 'http://spam' ) ) );
check( 422 === $s, 'honeypot filled: refused' );
list( $s, $d ) = call( 'POST', "/modules/$mid/bookings", $body( $tomorrow . ' 11:00', null, '+60123456706' ) );
check( 422 === $s && 'start' === $d['data']['field'], 'start without an offset refused', $d );
$soon = ( new DateTimeImmutable( '+30 minutes', $tz ) )->format( DATE_ATOM );
list( $s, $d ) = call( 'POST', "/modules/$mid/bookings", $body( $soon, null, '+60123456706' ) );
check( 422 === $s && 'outside_window' === $d['code'], 'inside the 60-minute notice: refused', $d );
list( $s, $d ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '11:00' ), null, '12' ) );
check( 422 === $s && 'customer.phone' === $d['data']['field'], 'short phone refused', $d );

echo "== customer records\n";
list( $s, $b3 ) = call( 'POST', "/modules/$mid/bookings", $body( $at( '11:00' ), null, '+60 12-345 6701', array( 'customer' => array( 'name' => 'Aisha B', 'phone' => '+60 12-345 6701', 'email' => 'someone-else@example.test' ) ) ) );
$cust = $wpdb->get_row( $wpdb->prepare( 'SELECT * FROM ' . Schema::table( 'customers' ) . ' WHERE phone = %s', '+60123456701' ), ARRAY_A );
check( 201 === $s && 'aisha@example.test' === $cust['email'] && 'Aisha B' === $cust['name'], 'same phone: name updated, email on record kept', $cust );
$contact = $wpdb->get_var( $wpdb->prepare( 'SELECT contact_email FROM ' . Schema::table( 'bookings' ) . ' WHERE code = %s', $b3['booking']['code'] ) );
check( 'someone-else@example.test' === $contact, 'the new email stays on the booking only', $contact );

echo "== manage link\n";
$code = $b1['booking']['code'];
list( $s, $v ) = call( 'GET', "/bookings/$code", null, array( 'token' => $b1['manage_token'] ) );
check( 200 === $s && $v['can_cancel'] && $v['cancel_deadline'], 'view with the token', $v );
list( $s ) = call( 'GET', "/bookings/$code", null, array( 'token' => str_repeat( 'a', 40 ) ) );
check( 404 === $s, 'wrong token: 404, same as a wrong code' );
list( $s, $v ) = call( 'POST', "/bookings/$code/cancel", array( 'token' => $b1['manage_token'] ) );
check( 200 === $s && 'cancelled' === $v['status'], 'cancelled through the link', $v );
list( , $av ) = call( 'GET', "/modules/$mid/availability", null, array( 'date' => $tomorrow, 'services' => (string) $svc['id'], 'staff' => (string) $staff['Mei'] ) );
check( in_array( $at( '10:00' ), wp_list_pluck( $av['slots'], 'start' ), true ), 'Mei is free at 10:00 again' );
list( $s, $d ) = call( 'POST', "/bookings/$code/cancel", array( 'token' => $b1['manage_token'] ) );
check( 409 === $s, 'cancelling twice: 409', $d );

echo "== approval, holds and the desk\n";
$cbody = static function ( $start, $phone ) use ( $consult ) {
	return array( 'service_ids' => array( $consult['id'] ), 'start' => $start, 'customer' => array( 'name' => 'Ben', 'phone' => $phone, 'email' => 'ben@example.test' ) );
};
list( $s, $p1 ) = call( 'POST', "/modules/$mid/bookings", $cbody( $at( '16:00' ), '+60129990001' ) );
check( 201 === $s && 'pending' === $p1['booking']['status'] && $p1['booking']['hold_expires'], 'approval service: pending with a hold', $p1 );
$state = $wpdb->get_var( $wpdb->prepare( 'SELECT a.state FROM ' . Schema::table( 'booking_allocations' ) . ' a JOIN ' . Schema::table( 'bookings' ) . ' b ON b.id = a.booking_id WHERE b.code = %s', $p1['booking']['code'] ) );
check( 'held' === $state, 'its allocation is held', $state );

as_user( 1 );
$p1id = (int) $wpdb->get_var( $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'bookings' ) . ' WHERE code = %s', $p1['booking']['code'] ) );
list( $s, $d ) = call( 'PATCH', "/admin/bookings/$p1id/status", array( 'status' => 'no-show' ) );
check( 409 === $s, 'pending -> no-show refused', $d );
list( $s, $d ) = call( 'PATCH', "/admin/bookings/$p1id/status", array( 'status' => 'confirmed', 'note' => 'See you then' ) );
check( 200 === $s && 'confirmed' === $d['status'] && null === $d['hold_expires'], 'confirmed by the desk', $d );
$state = $wpdb->get_var( $wpdb->prepare( 'SELECT state FROM ' . Schema::table( 'booking_allocations' ) . ' WHERE booking_id = %d', $p1id ) );
check( 'active' === $state, 'allocation now active', $state );
list( $s, $d ) = call( 'PATCH', "/admin/bookings/$p1id/status", array( 'status' => 'completed' ) );
check( 409 === $s, 'completing tomorrow\'s booking today: refused', $d );
list( $s, $h ) = call( 'GET', "/admin/bookings/$p1id/history" );
check( 200 === $s && 2 === count( $h ) && 'confirmed' === $h[1]['to'] && 'See you then' === $h[1]['note'] && $h[1]['by'], 'history: booked, then confirmed by a named user', $h );

as_user( 0 );
list( $s, $p2 ) = call( 'POST', "/modules/$mid/bookings", $cbody( $at( '17:00' ), '+60129990002' ) );
$wpdb->update( Schema::table( 'bookings' ), array( 'hold_expires_utc' => gmdate( 'Y-m-d H:i:s', time() - 60 ) ), array( 'code' => $p2['booking']['code'] ) );
list( , $av ) = call( 'GET', "/modules/$mid/availability", null, array( 'date' => $tomorrow, 'services' => (string) $consult['id'] ) );
$row = $wpdb->get_row( $wpdb->prepare( 'SELECT b.id, b.status, a.state FROM ' . Schema::table( 'bookings' ) . ' b JOIN ' . Schema::table( 'booking_allocations' ) . ' a ON a.booking_id = b.id WHERE b.code = %s', $p2['booking']['code'] ), ARRAY_A );
check( 'rejected' === $row['status'] && 'released' === $row['state'], 'expired hold swept on the next availability read', $row );
$slot = array_values( array_filter( $av['slots'], static function ( $x ) use ( $at ) {
	return $x['start'] === $at( '17:00' );
} ) );
check( $slot && 2 === count( $slot[0]['staff_ids'] ), 'both staff free at 17:00 again', $slot );

as_user( 1 );
list( $s, $list ) = call( 'GET', "/admin/modules/$mid/bookings", null, array( 'from' => $tomorrow, 'to' => $tomorrow ) );
check( 200 === $s && count( $list ) >= 6 && isset( $list[0]['customer']['phone'] ), 'desk list for tomorrow, with phone numbers', count( $list ) );
list( , $only ) = call( 'GET', "/admin/modules/$mid/bookings", null, array( 'from' => $tomorrow, 'to' => $tomorrow, 'status' => 'pending' ) );
check( array() === array_values( array_filter( $only, static function ( $x ) {
	return 'pending' !== $x['status'];
} ) ), 'status filter' );

echo "== roles and deletion\n";
$editor = get_user_by( 'login', 'jsb-editor' );
$eid    = $editor ? $editor->ID : wp_insert_user( array( 'user_login' => 'jsb-editor', 'user_pass' => wp_generate_password( 24 ), 'role' => 'editor', 'user_email' => 'editor@example.test' ) );
as_user( $eid );
list( $s ) = call( 'GET', "/admin/modules/$mid/bookings" );
check( 200 === $s, 'editor can work bookings' );
list( $s ) = call( 'POST', "/admin/modules/$mid/services", array( 'name' => 'X', 'duration_minutes' => 30 ) );
check( 403 === $s, 'editor cannot change setup (403)', $s );
as_user( 1 );
list( $s, $d ) = call( 'DELETE', "/admin/modules/$mid" );
check( 422 === $s, 'module with upcoming bookings cannot be deleted', $d );
list( $s, $d ) = call( 'DELETE', "/admin/services/{$svc['id']}" );
check( 200 === $s && $d['deactivated'], 'a booked service is deactivated, not deleted', $d );
call( 'PATCH', "/admin/services/{$svc['id']}", array( 'active' => true ) );
list( $s, $d ) = call( 'DELETE', "/admin/policies/{$m['default_policy_id']}" );
check( 422 === $s, 'the default policy cannot be deleted', $d );

echo "== emails\n";
$log      = mails();
$subjects = array_map( static function ( $x ) {
	return $x->subject;
}, $log );
check( count( array_filter( $log, static function ( $x ) {
	return 'owner@example.test' === $x->to;
} ) ) >= 6, 'owner told of each new booking', count( $log ) );
check( (bool) preg_grep( '/Booking cancelled/', $subjects ) && (bool) preg_grep( '/Booking confirmed/', $subjects ) && (bool) preg_grep( '/Booking rejected/', $subjects ), 'customer told of cancelled, confirmed and rejected', $subjects );

echo "== salon: setup\n";
as_user( 1 );
list( $s, $d ) = call( 'POST', "/admin/modules/$mid/services", array( 'name' => 'Add-on on a spa', 'duration_minutes' => 15, 'is_addon' => true ) );
check( 422 === $s && 'is_addon' === $d['data']['field'], 'add-ons refused on a staff module', $d );
list( , $sm ) = call( 'POST', '/admin/modules', array( 'title' => 'Test Salon', 'type' => 'salon' ) );
$sid = $sm['id'];
call( 'PATCH', "/admin/modules/$sid", array( 'status' => 'active' ) );
call( 'PATCH', "/admin/policies/{$sm['default_policy_id']}", array( 'min_notice_booking_minutes' => 0 ) ); // upsell scenarios book within the hour
$sv = array();
foreach ( array( 'cut' => array( 'Haircut', 45, 0, false ), 'color' => array( 'Colour', 90, 0, false ), 'gel' => array( 'Gel finish', 30, 0, true ), 'treat' => array( 'Treatment', 20, 5, true ) ) as $k => $x ) {
	list( $s, $r ) = call( 'POST', "/admin/modules/$sid/services", array( 'name' => $x[0], 'duration_minutes' => $x[1], 'buffer_minutes' => $x[2], 'is_addon' => $x[3] ) );
	$sv[ $k ] = $r['id'];
}
check( 4 === count( array_filter( $sv ) ), 'salon services, two of them add-ons' );
$pr = array();
foreach ( array( 'shampoo' => array( 'Shampoo', 5, '30' ), 'wax' => array( 'Wax', 1, '20' ) ) as $k => $x ) {
	list( $s, $r ) = call( 'POST', "/admin/modules/$sid/products", array( 'name' => $x[0], 'stock_qty' => $x[1], 'price' => $x[2], 'sku' => 'SKU-' . $k ) );
	$pr[ $k ] = $r['id'];
}
check( 201 === $s && '20.00' === $r['price'] && 1 === $r['stock_qty'], 'products created', $r );
// S1..S8: stylists working round the clock, so the upsell scenarios can start within the hour whatever the time
$st = array();
for ( $i = 1; $i <= 8; $i++ ) {
	list( , $r ) = call( 'POST', "/admin/modules/$sid/staff", array( 'name' => "S$i" ) );
	call( 'PUT', "/admin/staff/{$r['id']}/shifts", array( 'shifts' => array_map( static function ( $d ) {
		return array( 'weekday' => $d, 'start_time' => '00:00', 'end_time' => '23:45' );
	}, range( 0, 6 ) ) ) );
	$st[ $i ] = $r['id'];
}
$book = static function ( array $services, ?int $staff_id, string $start, string $name = 'Aisha', string $phone = '+60177000001' ) use ( $sid, $sv ) {
	return call( 'POST', "/modules/$sid/bookings", array(
		'service_ids' => array_map( static function ( $k ) use ( $sv ) {
			return $sv[ $k ];
		}, $services ),
		'staff_id'    => $staff_id,
		'start'       => $start,
		'customer'    => array( 'name' => $name, 'phone' => $phone ),
	) );
};
$bid = static function ( array $created ) use ( $wpdb ) {
	return (int) $wpdb->get_var( $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'bookings' ) . ' WHERE code = %s', $created['booking']['code'] ) );
};
$alloc = static function ( int $booking_id ) use ( $wpdb, $tz ) {
	$r = $wpdb->get_row( $wpdb->prepare( 'SELECT start_utc, end_utc FROM ' . Schema::table( 'booking_allocations' ) . " WHERE booking_id = %d AND state IN ('held','active')", $booking_id ), ARRAY_A );
	$f = static function ( $u ) use ( $tz ) {
		return ( new DateTimeImmutable( $u, new DateTimeZone( 'UTC' ) ) )->setTimezone( $tz )->format( 'H:i' );
	};
	return $r ? array( $f( $r['start_utc'] ), $f( $r['end_utc'] ) ) : null;
};
$T   = (int) ( ceil( ( time() + 300 ) / 900 ) * 900 ); // the next quarter hour at least 5 minutes away
$iso = static function ( int $ts ) use ( $tz ) {
	return ( new DateTimeImmutable( '@' . $ts ) )->setTimezone( $tz )->format( DATE_ATOM );
};
$hm  = static function ( int $ts ) use ( $tz ) {
	return ( new DateTimeImmutable( '@' . $ts ) )->setTimezone( $tz )->format( 'H:i' );
};
$progress = static function ( int $booking_id ) {
	return call( 'PATCH', "/admin/bookings/$booking_id/status", array( 'status' => 'in-progress' ) );
};
$items = static function ( int $booking_id, array $services = array(), array $products = array() ) use ( $sv, $pr ) {
	return call( 'POST', "/admin/bookings/$booking_id/items", array(
		'service_ids' => array_map( static function ( $k ) use ( $sv ) {
			return $sv[ $k ];
		}, $services ),
		'products'    => array_map( static function ( $k, $q ) use ( $pr ) {
			return array( 'product_id' => $pr[ $k ] ?? $k, 'quantity' => $q );
		}, array_keys( $products ), $products ),
	) );
};

echo "== salon: booking with add-ons\n";
as_user( 0 );
list( $s, $pub ) = call( 'GET', "/modules/$sid" );
$flags = array_column( $pub['services'], 'is_addon', 'name' );
check( 200 === $s && true === $flags['Gel finish'] && false === $flags['Haircut'], 'module info marks add-ons', $flags );
call( 'PUT', "/admin/staff/{$st[1]}/services", array( 'service_ids' => array() ) ); // (no-op as a visitor: 401)
list( , $av ) = call( 'GET', "/modules/$sid/availability", null, array( 'date' => $tomorrow, 'services' => "{$sv['cut']},{$sv['gel']}", 'staff' => (string) $st[1] ) );
$last = end( $av['slots'] );
check( 75 * 60 === strtotime( $av['slots'][0]['end'] ) - strtotime( $av['slots'][0]['start'] ) && $at( '22:30' ) === $last['start'], 'cut + gel is 75 minutes; the last start still ends by 23:45', $last );
list( $s, $d ) = $book( array( 'gel' ), null, $at( '10:00' ) );
check( 422 === $s && false !== strpos( $d['message'], 'base service' ), 'an add-on on its own: 422', $d );
list( $s ) = $book( array( 'cut', 'color' ), null, $at( '10:00' ) );
check( 422 === $s, 'two base services: 422' );
list( $s ) = call( 'GET', "/modules/$sid/availability", null, array( 'date' => $tomorrow, 'services' => (string) $sv['gel'] ) );
check( 422 === $s, 'availability for an add-on alone: 422' );
list( $s, $b ) = $book( array( 'cut', 'gel', 'treat' ), $st[1], $at( '10:00' ) );
check( 201 === $s && $at( '11:40' ) === $b['booking']['end'] && 3 === count( $b['booking']['services'] ) && ! in_array( true, array_column( $b['booking']['services'], 'is_upsell' ), true ), '10:00 cut + gel + treatment runs to 11:40 (45 + 30 + 20 + 5)', $b );

as_user( 1 );
call( 'PUT', "/admin/staff/{$st[1]}/services", array( 'service_ids' => array( $sv['cut'] ) ) );
call( 'PUT', "/admin/staff/{$st[2]}/services", array( 'service_ids' => array( $sv['gel'] ) ) );
as_user( 0 );
list( , $av ) = call( 'GET', "/modules/$sid/availability", null, array( 'date' => $tomorrow, 'services' => "{$sv['cut']},{$sv['gel']}" ) );
check( $av['slots'] && array( array( $st[1] ) ) === array_values( array_unique( array_column( $av['slots'], 'staff_ids' ), SORT_REGULAR ) ), 'qualification follows the base service only (S1 cuts; S2 linked to the add-on is not needed)' );
as_user( 1 );
call( 'PUT', "/admin/staff/{$st[1]}/services", array( 'service_ids' => array() ) );
call( 'PUT', "/admin/staff/{$st[2]}/services", array( 'service_ids' => array() ) );
as_user( 0 );
list( $s, $b ) = $book( array( 'cut', 'gel' ), null, $at( '15:00' ) );
check( 201 === $s && 'S1' !== $b['booking']['staff'][0]['name'], 'no stylist chosen: not S1, who already has a booking that day', $b['booking']['staff'] ?? $b );

echo "== salon: upsell\n";
as_user( 1 );
// A: an upsell that would reach the next appointment
list( , $first ) = $book( array( 'cut' ), $st[1], $iso( $T ) );
list( $s, $next ) = $book( array( 'color' ), $st[1], $iso( $T + 45 * 60 ), 'Next Customer', '+60111222333' );
$first_id = $bid( $first );
$next_id  = $bid( $next );
check( 201 === $s, 'S1: cut, then the next customer straight after', $next );
list( $s ) = $progress( $first_id );
check( 200 === $s, 'marked in progress' );
list( $s, $u ) = $items( $first_id, array( 'gel' ) );
check( 200 === $s && false === $u['extended'] && 30 === $u['added_minutes'], 'gel would run into the next appointment: not extended', $u );
$c = $u['conflicts'][0] ?? array();
check( 1 === count( $u['conflicts'] ) && $next_id === $c['booking_id'] && 'confirmed' === $c['status'] && 'Next Customer' === $c['customer_name'] && array( 'Colour' ) === $c['service_names'] && $iso( $T + 45 * 60 ) === $c['start'], 'the alert names the next appointment', $c );
check( array( $hm( $T ), $hm( $T + 45 * 60 ) ) === $alloc( $first_id ) && array( $hm( $T + 45 * 60 ), $hm( $T + 135 * 60 ) ) === $alloc( $next_id ), 'neither reservation moved' );
check( in_array( 'Gel finish', array_column( $u['booking']['services'], 'name' ), true ) && true === end( $u['booking']['services'] )['is_upsell'], 'the customer still gets the add-on' );
list( , $flagged ) = call( 'GET', "/admin/modules/$sid/bookings", null, array( 'from' => wp_date( 'Y-m-d', $T ), 'to' => $tomorrow, 'schedule_conflict' => '1' ) );
check( array( $first_id, $next_id ) === array_column( $flagged, 'id' ), 'both appointments flagged', array_column( $flagged, 'id' ) );
list( $s, $d ) = call( 'PATCH', "/admin/bookings/$next_id", array( 'schedule_conflict' => false ) );
check( 200 === $s && false === $d['schedule_conflict'], 'the flag can be cleared', $d );

// B: an upsell ending exactly when the next appointment starts
list( , $b1 ) = $book( array( 'cut' ), $st[2], $iso( $T ) );
$book( array( 'cut' ), $st[2], $iso( $T + 75 * 60 ), 'Later', '+60111222334' );
$progress( $bid( $b1 ) );
list( $s, $u ) = $items( $bid( $b1 ), array( 'gel' ) );
check( 200 === $s && true === $u['extended'] && array() === $u['conflicts'] && $iso( $T + 75 * 60 ) === $u['booking']['end'], 'ending exactly at the next start: extended', $u );

// C: the rules
list( , $c1 ) = $book( array( 'cut' ), $st[3], $iso( $T ) );
$c1 = $bid( $c1 );
list( $s, $d ) = $items( $c1, array( 'gel' ) );
check( 409 === $s && false !== strpos( $d['message'], 'in progress' ), 'before the appointment starts: 409', $d );
$progress( $c1 );
list( $s ) = $items( $c1, array( 'color' ) );
check( 422 === $s, 'a base service is not an add-on: 422' );
list( $s ) = $items( $c1 );
check( 422 === $s, 'nothing to add: 422' );
list( $s ) = call( 'POST', "/admin/bookings/$c1/items", array( 'service_ids' => array( 999999 ) ) );
check( 404 === $s, 'unknown service: 404' );
list( $s ) = call( 'POST', "/admin/bookings/$p1id/items", array( 'service_ids' => array( $sv['gel'] ) ) );
check( 409 === $s, 'a spa booking: upsell does not apply (409)' );

echo "== own appointments only\n";
$stylist = get_user_by( 'login', 'jsb-stylist' );
$uid     = $stylist ? $stylist->ID : wp_insert_user( array( 'user_login' => 'jsb-stylist', 'user_pass' => wp_generate_password( 24 ), 'role' => 'editor', 'user_email' => 'stylist@example.test' ) );
call( 'PATCH', "/admin/staff/{$st[3]}", array( 'wp_user_id' => $uid ) );
list( , $c2 ) = $book( array( 'cut' ), $st[4], $iso( $T ) );
$c2 = $bid( $c2 );
as_user( $uid );
list( $s ) = $items( $c1, array( 'gel' ) );
check( 200 === $s, 'a stylist can add to their own appointment' );
list( $s, $d ) = $progress( $c2 );
check( 403 === $s && 'forbidden' === $d['code'], 'but cannot change the status of another stylist\'s appointment (403)', $d );
list( $s ) = $items( $c2, array( 'gel' ) );
check( 403 === $s, 'nor add to it (403)' );
list( $s ) = call( 'GET', "/admin/modules/$sid/bookings" );
check( 200 === $s, 'they can still read the desk list' );
as_user( $eid );
list( $s ) = $progress( $c2 );
check( 200 === $s, 'a desk user linked to no stylist works every appointment' );
as_user( 1 );

echo "== salon: products and stock\n";
list( , $d1 ) = $book( array( 'cut' ), $st[5], $iso( $T ) );
$d1 = $bid( $d1 );
$progress( $d1 );
list( $s ) = $items( $d1, array(), array( 'shampoo' => 2 ) );
list( $s2 ) = $items( $d1, array(), array( 'shampoo' => 1 ) );
list( , $v ) = call( 'GET', "/admin/bookings/$d1" );
check( 200 === $s && 200 === $s2 && array( array( 'product_id' => $pr['shampoo'], 'name' => 'Shampoo', 'quantity' => 3, 'unit_price' => '30.00' ) ) === $v['products'], 'same product twice: quantities add up at today\'s price', $v['products'] );
list( $s, $d ) = $items( $d1, array(), array( 'shampoo' => 3 ) );
check( 409 === $s && 'insufficient_stock' === $d['code'] && 5 === $d['data']['in_stock'], '3 on the ticket + 3 > 5 on the shelf: 409', $d );
list( , $v ) = call( 'GET', "/admin/bookings/$d1" );
check( 3 === $v['products'][0]['quantity'], 'the refused request changed nothing' );
call( 'PATCH', "/admin/products/{$pr['shampoo']}", array( 'price' => '99' ) );
list( , $v ) = call( 'GET', "/admin/bookings/$d1" );
check( '30.00' === $v['products'][0]['unit_price'], 'the ticket keeps the price it was sold at' );
list( $s ) = $items( $d1, array(), array( 999999 => 1 ) );
check( 404 === $s, 'unknown product: 404' );
list( , $serum ) = call( 'POST', "/admin/modules/$sid/products", array( 'name' => 'Serum', 'stock_qty' => 0, 'stock_status' => 'out_of_stock' ) );
list( $s ) = $items( $d1, array(), array( $serum['id'] => 1 ) );
check( 409 === $s, 'out of stock: 409' );

list( , $e1 ) = $book( array( 'cut' ), $st[6], $iso( $T ) );
$e1 = $bid( $e1 );
$progress( $e1 );
$items( $e1, array( 'gel' ), array( 'shampoo' => 2, 'wax' => 1 ) );
$stock = static function ( int $id ) use ( $wpdb ) {
	$r = $wpdb->get_row( $wpdb->prepare( 'SELECT stock_qty, stock_status FROM ' . Schema::table( 'products' ) . ' WHERE id = %d', $id ), ARRAY_A );
	return array( (int) $r['stock_qty'], $r['stock_status'] );
};
check( array( 5, 'in_stock' ) === $stock( $pr['shampoo'] ), 'nothing leaves the shelf while the appointment runs' );
list( $s, $v ) = call( 'PATCH', "/admin/bookings/$e1/status", array( 'status' => 'completed' ) );
check( 200 === $s && 75 === $v['actual_duration_minutes'], 'completed: duration locked at 45 + 30', $v['actual_duration_minutes'] ?? $v );
check( array( 3, 'in_stock' ) === $stock( $pr['shampoo'] ) && array( 0, 'out_of_stock' ) === $stock( $pr['wax'] ), 'stock deducted; the last wax went, so it shows out of stock' );
as_user( 0 );
list( , $cat ) = call( 'GET', "/modules/$sid/products" );
$by = array_column( $cat, 'stock_status', 'name' );
check( 'out_of_stock' === $by['Wax'] && ! isset( $cat[0]['stock_qty'] ) && ! isset( $cat[0]['sku'] ), 'public catalog: status, never counts or SKUs', $cat );
as_user( 1 );

list( , $tube ) = call( 'POST', "/admin/modules/$sid/products", array( 'name' => 'Colour tube', 'stock_qty' => 1, 'stock_status' => 'available_on_order', 'estimated_lead_time_days' => 3 ) );
$tickets = array();
foreach ( array( 7, 8 ) as $n ) {
	list( , $x ) = $book( array( 'cut' ), $st[ $n ], $iso( $T ) );
	$tickets[] = $bid( $x );
}
foreach ( $tickets as $t ) {
	$progress( $t );
	list( $s ) = $items( $t, array(), array( $tube['id'] => 1 ) );
	check( 200 === $s, "ticket $t: the last tube goes on it (both see one on the shelf)" );
}
foreach ( $tickets as $t ) {
	call( 'PATCH', "/admin/bookings/$t/status", array( 'status' => 'completed' ) );
}
check( array( 0, 'available_on_order' ) === $stock( $tube['id'] ), 'stock clamps at 0, and on-order keeps its status', $stock( $tube['id'] ) );
list( $s ) = call( 'DELETE', "/admin/products/{$pr['shampoo']}" );
check( 422 === $s, 'a product on a ticket cannot be deleted' );

echo "== salon race fixture\n";
list( , $rsm ) = call( 'POST', '/admin/modules', array( 'title' => 'Salon race', 'type' => 'salon' ) );
call( 'PATCH', "/admin/modules/{$rsm['id']}", array( 'status' => 'active' ) );
call( 'PATCH', "/admin/policies/{$rsm['default_policy_id']}", array( 'min_notice_booking_minutes' => 0 ) );
$race = array( 'module' => $rsm['id'], 'T' => $iso( $T ) );
foreach ( array( 'cut' => array( 45, false ), 'a30' => array( 30, true ), 'a20' => array( 20, true ), 'a15' => array( 15, true ) ) as $k => $x ) {
	list( , $r ) = call( 'POST', "/admin/modules/{$rsm['id']}/services", array( 'name' => $k, 'duration_minutes' => $x[0], 'is_addon' => $x[1] ) );
	$race[ $k ] = $r['id'];
}
list( , $r ) = call( 'POST', "/admin/modules/{$rsm['id']}/products", array( 'name' => 'Last unit', 'stock_qty' => 1 ) );
$race['product'] = $r['id'];
foreach ( array( 'r1', 'r2', 'r3', 'r4' ) as $n ) {
	list( , $r ) = call( 'POST', "/admin/modules/{$rsm['id']}/staff", array( 'name' => $n ) );
	call( 'PUT', "/admin/staff/{$r['id']}/shifts", array( 'shifts' => array_map( static function ( $d ) {
		return array( 'weekday' => $d, 'start_time' => '00:00', 'end_time' => '23:45' );
	}, range( 0, 6 ) ) ) );
	list( , $b ) = call( 'POST', "/modules/{$rsm['id']}/bookings", array( 'service_ids' => array( $race['cut'] ), 'staff_id' => $r['id'], 'start' => $iso( $T ), 'customer' => array( 'name' => "Race $n", 'phone' => '+6019000000' . substr( $n, 1 ) ) ) );
	$race[ $n ] = array( 'staff' => $r['id'], 'booking' => $bid( $b ) );
	$progress( $race[ $n ]['booking'] );
}
foreach ( array( 'r3', 'r4' ) as $n ) {
	$items( $race[ $n ]['booking'], array(), array( $race['product'] => 1 ) );
}

echo "== race fixture\n";
as_user( 1 );
list( , $rm ) = call( 'POST', '/admin/modules', array( 'title' => 'Race', 'type' => 'staff' ) );
call( 'PATCH', "/admin/modules/{$rm['id']}", array( 'status' => 'active' ) );
list( , $rs ) = call( 'POST', "/admin/modules/{$rm['id']}/services", array( 'name' => 'Cut', 'duration_minutes' => 60 ) );
$race_staff = array();
foreach ( array( 'A', 'B', 'C' ) as $n ) {
	list( , $r ) = call( 'POST', "/admin/modules/{$rm['id']}/staff", array( 'name' => $n ) );
	call( 'PUT', "/admin/staff/{$r['id']}/shifts", array( 'shifts' => array_map( static function ( $d ) {
		return array( 'weekday' => $d, 'start_time' => '09:00', 'end_time' => '18:00' );
	}, range( 0, 6 ) ) ) );
	$race_staff[] = $r['id'];
}

echo "\n" . ( $failures ? "$failures FAILED\n" : "all passed\n" );
echo 'SALONRACE ' . wp_json_encode( $race ) . "\n";
echo 'RACE ' . wp_json_encode( array( 'module' => $rm['id'], 'service' => $rs['id'], 'staff' => $race_staff, 'start10' => $at( '10:00' ), 'start11' => $at( '11:00' ) ) ) . "\n";
exit( $failures ? 1 : 0 );
