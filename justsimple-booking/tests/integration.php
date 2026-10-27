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
list( $s, $d ) = call( 'POST', '/admin/modules', array( 'title' => 'Odd', 'type' => 'spaceship' ) );
check( 422 === $s && 'type' === $d['data']['field'], 'an unknown module type is refused', $d );
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

echo "== restaurant: setup\n";
as_user( 1 );
list( $s, $d ) = call( 'POST', "/admin/modules/$sid/tables", array( 'name' => 'T1', 'capacity' => 2 ) );
check( 422 === $s && 'tables' === $d['data']['field'], 'tables refused on a salon module', $d );
list( , $rm2 ) = call( 'POST', '/admin/modules', array( 'title' => 'Test Bistro', 'type' => 'restaurant' ) );
$rid = $rm2['id'];
call( 'PATCH', "/admin/modules/$rid", array( 'status' => 'active' ) );
call( 'PATCH', "/admin/policies/{$rm2['default_policy_id']}", array( 'min_notice_cancel_minutes' => 120 ) );
call( 'PUT', "/admin/modules/$rid/hours", array( 'hours' => array_map( static function ( $d ) {
	return array( 'weekday' => $d, 'open_time' => '11:00', 'close_time' => '22:00' );
}, range( 0, 6 ) ) ) );
list( $s ) = call( 'GET', "/modules/$rid/availability", null, array( 'date' => $tomorrow, 'party_size' => 2 ) );
check( 422 === $s, 'no seating times yet: 422' );
$seating = array( 'booking_mode' => 'flexible', 'slot_duration_minutes' => 90, 'slot_interval_minutes' => 30, 'first_slot_time' => '11:00', 'last_slot_time' => '20:30', 'buffer_minutes' => 15 );
list( $s, $d ) = call( 'PUT', "/admin/modules/$rid/seating", $seating );
check( 200 === $s && 'flexible' === $d['booking_mode'] && false === $d['allow_table_selection'] && '20:30' === $d['last_slot_time'], 'seating times saved; table choice off by default', $d );
list( $s ) = call( 'PUT', "/admin/modules/$mid/seating", $seating );
check( 422 === $s, 'seating times refused on a staff module' );
$tb = array();
foreach ( array( array( 'a2', 2, false, null ), array( 'b2', 2, false, null ), array( 'c4', 4, false, null ), array( 'd6', 6, false, null ), array( 'v4', 4, true, '300' ), array( 'v2', 2, true, '200' ) ) as $x ) {
	list( $s, $t ) = call( 'POST', "/admin/modules/$rid/tables", array( 'name' => $x[0], 'capacity' => $x[1], 'is_vip' => $x[2], 'minimum_spend' => $x[3] ?? 0, 'location' => 'indoor' ) );
	$tb[ $x[0] ] = $t['id'];
}
check( 201 === $s && true === $t['is_vip'] && '200.00' === $t['minimum_spend'] && 2 === $t['capacity'] && 'indoor' === $t['location'], 'tables created with class, minimum spend and location', $t );
list( $s, $d ) = call( 'POST', "/admin/modules/$rid/tables", array( 'name' => 'x', 'capacity' => 2, 'minimum_spend' => 50 ) );
check( 422 === $s && 'minimum_spend' === $d['data']['field'], 'minimum spend only on VIP tables', $d );
list( $s, $d ) = call( 'POST', "/admin/modules/$rid/tables", array( 'name' => 'x' ) );
check( 422 === $s && 'capacity' === $d['data']['field'], 'a table needs a capacity', $d );
list( $s, $d ) = call( 'PATCH', "/admin/tables/{$tb['v2']}", array( 'location' => 'terrace' ) );
check( 200 === $s && 'terrace' === $d['location'] && '200.00' === $d['minimum_spend'] && true === $d['is_vip'], 'patching one attribute keeps the others', $d );

$wd   = (int) wp_date( 'w', strtotime( $tomorrow . ' 12:00' ) );
$rule = static function ( array $rules ) use ( $rid ) {
	return call( 'PUT', "/admin/modules/$rid/table-rules", array( 'rules' => $rules ) );
};
check( 422 === $rule( array( array( 'weekday' => 1, 'table_type' => 'normal', 'open_tables' => 5 ) ) )[0], 'open_tables above the 4 normal tables: 422' );
check( 422 === $rule( array( array( 'weekday' => 1, 'table_type' => 'vip', 'open_tables' => 3 ) ) )[0], 'open_tables above the 2 VIP tables: 422' );
check( 422 === $rule( array( array( 'table_type' => 'normal', 'open_tables' => 1 ) ) )[0], 'neither weekday nor date: 422' );
check( 422 === $rule( array( array( 'weekday' => 1, 'specific_date' => '2031-01-01', 'open_tables' => 1 ) ) )[0], 'both weekday and date: 422' );
$dup = array( 'weekday' => 1, 'table_type' => 'normal', 'open_tables' => 1 );
check( 422 === $rule( array( $dup, $dup ) )[0], 'duplicate rules: 422' );
list( $s, $d ) = $rule( array( array( 'weekday' => 1, 'table_type' => 'normal', 'open_tables' => 4 ) ) );
check( 200 === $s && 1 === count( $d ), 'a valid rule is saved', $d );
$rule( array() );

echo "== restaurant: public reads and booking\n";
as_user( 0 );
list( $s, $info ) = call( 'GET', "/modules/$rid" );
check( 200 === $s && 6 === $info['restaurant']['normal']['largest_table'] && 14 === $info['restaurant']['normal']['largest_separate_party'] && 6 === $info['restaurant']['vip']['largest_separate_party'] && ! isset( $info['restaurant']['tables'] ), 'module info: what a party can book, no table list while choice is off', $info['restaurant'] ?? $info );
list( $s, $d ) = call( 'GET', "/modules/$rid/availability", null, array( 'date' => $tomorrow ) );
check( 422 === $s && 'party_size' === $d['data']['field'], 'party size is required', $d );
list( , $av ) = call( 'GET', "/modules/$rid/availability", null, array( 'date' => $tomorrow, 'party_size' => 2 ) );
$sl = $av['slots'];
check( $at( '11:00' ) === $sl[0]['start'] && $at( '20:30' ) === end( $sl )['start'] && 105 * 60 === strtotime( $sl[0]['end'] ) - strtotime( $sl[0]['start'] ) && array( $tb['a2'] ) === $sl[0]['table_ids'], 'grid 11:00-20:30, 105 minutes held, the smallest 2-top first', $sl[0] );
list( , $mo ) = call( 'GET', "/modules/$rid/availability/month", null, array( 'month' => substr( $tomorrow, 0, 7 ), 'party_size' => 2 ) );
check( in_array( $tomorrow, $mo['dates'], true ), 'month view works with a party', $mo );

$rbook = static function ( int $party, string $hm, array $extra = array(), string $phone = null ) use ( $rid, $at ) {
	return call( 'POST', "/modules/$rid/bookings", array_merge( array(
		'party_size' => $party,
		'start'      => $at( $hm ),
		'customer'   => array( 'name' => 'Diner', 'phone' => $phone ?? '+6013' . random_int( 1000000, 9999999 ), 'email' => 'diner@example.test' ),
	), $extra ) );
};
list( $s, $b ) = $rbook( 3, '19:00', array( 'special_request' => 'Window seat, birthday' ) );
check( 201 === $s && array( 'c4' ) === array_column( $b['booking']['tables'], 'name' ) && 3 === $b['booking']['party_size'] && 'normal' === $b['booking']['table_type'] && ! isset( $b['booking']['staff'] ) && null === $b['booking']['minimum_spend_agreed'], 'party of 3 at 19:00: the 4-top, no minimum spend', $b );
$first_rest = $b;
list( $s, $d ) = $rbook( 8, '19:00' );
check( 409 === $s && 'slot_unavailable' === $d['code'], '8 without separate seating: 409', $d );
list( $s, $b ) = $rbook( 8, '19:00', array( 'allow_separate_seating' => true ) );
check( 201 === $s && array( 'a2', 'd6' ) === array_column( $b['booking']['tables'], 'name' ) || ( 201 === $s && array( 'd6', 'a2' ) === array_column( $b['booking']['tables'], 'name' ) ), '8 with separate seating: 6 + 2', $b['booking']['tables'] ?? $b );

list( $s, $d ) = $rbook( 3, '19:00', array( 'table_type' => 'vip' ) );
check( 422 === $s && 'minimum_spend_not_accepted' === $d['code'] && '300.00' === $d['data']['minimum_spend'], 'VIP without accepting: 422 with the amount', $d );
list( $s, $d ) = $rbook( 3, '19:00', array( 'table_type' => 'vip', 'accepted_minimum_spend' => 'lots' ) );
check( 422 === $s && 'accepted_minimum_spend' === $d['data']['field'], 'the accepted amount must be a real amount', $d );
list( $s, $b ) = $rbook( 3, '19:00', array( 'table_type' => 'vip', 'accept_minimum_spend' => true ) );
check( 201 === $s && '300.00' === $b['booking']['minimum_spend_agreed'] && array( 'v4' ) === array_column( $b['booking']['tables'], 'name' ), 'VIP accepted: v4 at 300.00', $b );
list( $s, $d ) = $rbook( 2, '19:00', array( 'table_type' => 'vip', 'accepted_minimum_spend' => '150' ) );
check( 422 === $s && '200.00' === $d['data']['minimum_spend'] && '150.00' === $d['data']['accepted_minimum_spend'], 'an amount below the table\'s: refused with both amounts', $d );
list( $s, $b ) = $rbook( 2, '19:00', array( 'table_type' => 'vip', 'accepted_minimum_spend' => '500' ) );
check( 201 === $s && '200.00' === $b['booking']['minimum_spend_agreed'], 'a cheaper table than the amount shown is fine and records its own amount', $b );

as_user( 1 );
list( , $vipdesk ) = call( 'GET', "/admin/modules/$rid/bookings", null, array( 'from' => $tomorrow, 'to' => $tomorrow, 'table_type' => 'vip' ) );
check( 2 === count( $vipdesk ) && array( 'vip' ) === array_values( array_unique( array_column( $vipdesk, 'table_type' ) ) ), 'desk list filtered to VIP', count( $vipdesk ) );
list( , $normdesk ) = call( 'GET', "/admin/modules/$rid/bookings", null, array( 'from' => $tomorrow, 'to' => $tomorrow, 'table_type' => 'normal' ) );
$withreq = array_values( array_filter( $normdesk, static function ( $x ) {
	return 'Window seat, birthday' === $x['special_request'];
} ) );
check( 1 === count( $withreq ) && 3 === $withreq[0]['party_size'], 'the special request and party size reach the desk' );
as_user( 0 );

list( $s, $d ) = call( 'POST', "/bookings/{$first_rest['booking']['code']}/cancel", array( 'token' => $first_rest['manage_token'] ) );
list( , $av ) = call( 'GET', "/modules/$rid/availability", null, array( 'date' => $tomorrow, 'party_size' => 3 ) );
$at19 = array_values( array_filter( $av['slots'], static function ( $x ) use ( $at ) {
	return $x['start'] === $at( '19:00' );
} ) );
check( 200 === $s && $at19 && array( $tb['c4'] ) === $at19[0]['table_ids'], 'cancelling frees the 4-top at 19:00', $at19 );
$mail = mails();
check( (bool) array_filter( $mail, static function ( $x ) {
	return false !== strpos( $x->message, 'Table for 3' ) && false !== strpos( $x->message, 'Table: c4' );
} ), 'the email names the party and the table' );

echo "== restaurant: caps, holds and closures\n";
as_user( 1 );
$rule( array( array( 'weekday' => $wd, 'table_type' => 'normal', 'open_tables' => 2 ) ) );
// 19:00 already holds d6 + a2 (the party of 8): two tables in use, so the cap of 2 is reached then
list( $s ) = $rbook( 2, '19:00' );
check( 409 === $s, 'cap of 2 reached by the combined party: 409' );
list( $s ) = $rbook( 2, '12:00' );
list( $s2 ) = $rbook( 2, '12:00' );
list( $s3 ) = $rbook( 2, '12:00' );
check( 201 === $s && 201 === $s2 && 409 === $s3, 'cap of 2 at noon: third party refused', array( $s, $s2, $s3 ) );
list( $s ) = $rbook( 2, '14:00' );
check( 201 === $s, 'a seating after both have finished is fine' );
$rule( array( array( 'weekday' => $wd, 'table_type' => 'normal', 'open_tables' => 4 ), array( 'specific_date' => $tomorrow, 'table_type' => 'normal', 'open_tables' => 0 ) ) );
list( , $av ) = call( 'GET', "/modules/$rid/availability", null, array( 'date' => $tomorrow, 'party_size' => 2 ) );
check( array() === $av['slots'], 'a date rule of 0 beats the weekday rule and closes online booking' );
$rule( array() );
list( , $leave ) = call( 'POST', "/admin/modules/$rid/days-off", array( 'name' => 'Repair', 'resource_id' => $tb['d6'], 'start_date' => $tomorrow, 'end_date' => $tomorrow ) );
list( , $av ) = call( 'GET', "/modules/$rid/availability", null, array( 'date' => $tomorrow, 'party_size' => 5 ) );
check( array() === $av['slots'], 'the only 5-seat table on leave: no slots for 5' );
call( 'DELETE', "/admin/days-off/{$leave['id']}" );

list( , $pol ) = call( 'POST', "/admin/modules/$rid/policies", array( 'name' => 'Approval', 'default_status' => 'pending', 'pending_timeout_minutes' => 30 ) );
$seating['policy_id'] = $pol['id'];
call( 'PUT', "/admin/modules/$rid/seating", $seating );
as_user( 0 );
// 11:00, because the 6-top is busy 19:00-20:45 with the party of 8
list( $s, $b ) = $rbook( 6, '11:00' );
list( $s2, $d ) = $rbook( 6, '11:00' );
check( 201 === $s && 'pending' === $b['booking']['status'] && 409 === $s2, 'the seating\'s own policy applies: pending, and the hold keeps the 6-top', array( $s, $s2 ) );
as_user( 1 );
$seating['policy_id'] = null;
call( 'PUT', "/admin/modules/$rid/seating", $seating );

echo "== restaurant: choosing a table\n";
as_user( 0 );
list( $s, $d ) = $rbook( 2, '17:00', array( 'table_id' => $tb['c4'] ) );
check( 422 === $s && 'table_id' === $d['data']['field'], 'choice off: a named table is refused, not ignored', $d );
as_user( 1 );
call( 'PUT', "/admin/modules/$rid/seating", array_merge( $seating, array( 'allow_table_selection' => true ) ) );
as_user( 0 );
list( , $info ) = call( 'GET', "/modules/$rid" );
check( true === $info['restaurant']['table_selection'] && 6 === count( $info['restaurant']['tables'] ) && '200.00' === array_column( $info['restaurant']['tables'], 'minimum_spend', 'name' )['v2'], 'choice on: the tables are listed with class and minimum spend', $info['restaurant'] );
list( $s, $b ) = $rbook( 2, '17:00', array( 'table_id' => $tb['c4'] ) );
check( 201 === $s && array( 'c4' ) === array_column( $b['booking']['tables'], 'name' ), 'the customer gets the table they chose', $b );
list( $s ) = $rbook( 2, '17:00', array( 'table_id' => $tb['c4'] ) );
list( $s2 ) = $rbook( 2, '17:00' );
check( 409 === $s && 201 === $s2, 'a chosen table that is taken: 409, while the others stay bookable' );
list( $s ) = $rbook( 2, '17:00', array( 'table_id' => $tb['v2'] ) );
check( 422 === $s, 'a VIP table for a normal party: 422' );
list( $s ) = $rbook( 2, '17:00', array( 'table_id' => $tb['d6'], 'allow_separate_seating' => true ) );
check( 422 === $s, 'a chosen table is never combined: 422' );
as_user( 1 );
call( 'PUT', "/admin/modules/$rid/seating", $seating );
list( $s, $d ) = call( 'DELETE', "/admin/tables/{$tb['c4']}" );
check( 200 === $s && true === $d['deactivated'], 'a booked table is deactivated, not deleted', $d );
call( 'PATCH', "/admin/tables/{$tb['c4']}", array( 'status' => 'active' ) );

echo "== restaurant race fixture\n";
list( , $rr ) = call( 'POST', '/admin/modules', array( 'title' => 'Restaurant race', 'type' => 'restaurant' ) );
call( 'PATCH', "/admin/modules/{$rr['id']}", array( 'status' => 'active' ) );
call( 'PUT', "/admin/modules/{$rr['id']}/hours", array( 'hours' => array_map( static function ( $d ) {
	return array( 'weekday' => $d, 'open_time' => '11:00', 'close_time' => '22:00' );
}, range( 0, 6 ) ) ) );
call( 'PUT', "/admin/modules/{$rr['id']}/seating", array_merge( $seating, array( 'allow_table_selection' => true ) ) );
$rrace = array( 'module' => $rr['id'] );
foreach ( array( array( 'a2', 2, false, 0 ), array( 'b2', 2, false, 0 ), array( 'c4', 4, false, 0 ), array( 'd6', 6, false, 0 ), array( 'v4', 4, true, 300 ), array( 'v2', 2, true, 200 ) ) as $x ) {
	list( , $t ) = call( 'POST', "/admin/modules/{$rr['id']}/tables", array( 'name' => $x[0], 'capacity' => $x[1], 'is_vip' => $x[2], 'minimum_spend' => $x[3] ) );
	$rrace[ $x[0] ] = $t['id'];
}
$days = array();
for ( $k = 3; $k <= 6; $k++ ) {
	$days[ $k ] = ( new DateTimeImmutable( "today +$k days", $tz ) )->format( 'Y-m-d' );
}
call( 'PUT', "/admin/modules/{$rr['id']}/table-rules", array( 'rules' => array(
	array( 'specific_date' => $days[3], 'table_type' => 'normal', 'open_tables' => 2 ),
	array( 'specific_date' => $days[4], 'table_type' => 'normal', 'open_tables' => 1 ),
) ) );
foreach ( $days as $k => $day ) {
	$rrace[ "day$k" ] = ( new DateTimeImmutable( "$day 19:00", $tz ) )->format( DATE_ATOM );
}

echo "== workshop: setup\n";
as_user( 1 );
list( $s, $d ) = call( 'POST', "/admin/modules/$mid/bays", array( 'name' => 'Bay X' ) );
check( 422 === $s && 'bays' === $d['data']['field'], 'bays refused on a staff module', $d );
$garage = static function ( string $title, array $mechs, array $bays, ?array $hours, array $shift ) {
	list( , $g ) = call( 'POST', '/admin/modules', array( 'title' => $title, 'type' => 'workshop' ) );
	call( 'PATCH', "/admin/modules/{$g['id']}", array( 'status' => 'active' ) );
	if ( null !== $hours ) {
		call( 'PUT', "/admin/modules/{$g['id']}/hours", array( 'hours' => array_map( static function ( $d ) use ( $hours ) {
			return array( 'weekday' => $d, 'open_time' => $hours[0], 'close_time' => $hours[1] );
		}, range( 0, 6 ) ) ) );
	}
	$out = array( 'id' => $g['id'], 'policy' => $g['default_policy_id'], 'mech' => array(), 'bay' => array(), 'svc' => array(), 'car' => array() );
	foreach ( $mechs as $n ) {
		list( , $r ) = call( 'POST', "/admin/modules/{$g['id']}/staff", array( 'name' => $n ) );
		call( 'PUT', "/admin/staff/{$r['id']}/shifts", array( 'shifts' => array_map( static function ( $d ) use ( $shift ) {
			return array( 'weekday' => $d ) + $shift;
		}, range( 0, 6 ) ) ) );
		$out['mech'][ $n ] = $r['id'];
	}
	foreach ( $bays as $n ) {
		list( , $r ) = call( 'POST', "/admin/modules/{$g['id']}/bays", array( 'name' => $n ) );
		$out['bay'][ $n ] = $r['id'];
	}
	foreach ( array( 'oil' => 60, 'brake' => 120 ) as $k => $min ) {
		list( , $r ) = call( 'POST', "/admin/modules/{$g['id']}/services", array( 'name' => $k, 'duration_minutes' => $min ) );
		$out['svc'][ $k ] = $r['id'];
	}
	foreach ( array( 'Vios' => array( 'Toyota', null, null ), 'City' => array( 'Honda', 2015, 2022 ) ) as $model => $x ) {
		list( , $r ) = call( 'POST', "/admin/modules/{$g['id']}/supported-vehicles", array( 'make' => $x[0], 'model' => $model, 'year_from' => $x[1], 'year_to' => $x[2] ) );
		$out['car'][ $model ] = $r['id'];
	}
	return $out;
};
$day_shift = array( 'start_time' => '09:00', 'end_time' => '18:00', 'break_start' => '13:00', 'break_end' => '14:00' );
$g         = $garage( 'Test Garage', array( 'Ali', 'Bala' ), array( 'Bay 1', 'Bay 2' ), array( '08:00', '18:00' ), $day_shift );
check( 2 === count( $g['bay'] ) && 2 === count( array_filter( $g['car'] ) ), 'workshop with two mechanics, two bays and two supported models' );
list( $s, $d ) = call( 'POST', "/admin/modules/{$g['id']}/supported-vehicles", array( 'make' => 'X', 'model' => 'Y', 'year_from' => 2020, 'year_to' => 2010 ) );
check( 422 === $s && 'year_to' === $d['data']['field'], 'a year range must run forwards', $d );
list( $s ) = call( 'POST', "/admin/modules/$rid/supported-vehicles", array( 'make' => 'X', 'model' => 'Y' ) );
check( 422 === $s, 'supported vehicles refused on a restaurant' );
list( $s, $d ) = call( 'POST', "/admin/modules/{$g['id']}/products", array( 'name' => 'X', 'stock_status' => 'available_on_order' ) );
check( 422 === $s && 'estimated_lead_time_days' === $d['data']['field'], 'a part on order needs a lead time', $d );

$day3 = ( new DateTimeImmutable( 'today +3 days', $tz ) )->format( 'Y-m-d' );
$gat  = static function ( string $day, string $hm ) use ( $tz ) {
	return ( new DateTimeImmutable( "$day $hm", $tz ) )->format( DATE_ATOM );
};
$gbook = static function ( array $gg, array $svcs, string $start, array $extra = array() ) {
	return call( 'POST', "/modules/{$gg['id']}/bookings", array_merge( array(
		'service_ids' => array_map( static function ( $k ) use ( $gg ) {
			return $gg['svc'][ $k ];
		}, $svcs ),
		'start'       => $start,
		'customer'    => array( 'name' => 'Driver', 'phone' => '+6014' . random_int( 1000000, 9999999 ), 'email' => 'driver@example.test' ),
		'vehicle'     => array( 'plate_no' => 'W' . random_int( 10000, 99999 ), 'supported_vehicle_id' => $gg['car']['Vios'] ),
	), $extra ) );
};
$rows = static function ( int $booking_id ) use ( $wpdb, $tz ) {
	$f = static function ( $u ) use ( $tz ) {
		return ( new DateTimeImmutable( $u, new DateTimeZone( 'UTC' ) ) )->setTimezone( $tz )->format( 'H:i' );
	};
	return array_map( static function ( $x ) use ( $f ) {
		return array( $x['type'], $x['name'], $f( $x['start_utc'] ), $f( $x['end_utc'] ), $x['state'] );
	}, $wpdb->get_results( $wpdb->prepare( 'SELECT r.type, r.name, a.start_utc, a.end_utc, a.state FROM ' . Schema::table( 'booking_allocations' ) . ' a JOIN ' . Schema::table( 'resources' ) . ' r ON r.id = a.resource_id WHERE a.booking_id = %d ORDER BY a.start_utc, r.type', $booking_id ), ARRAY_A ) );
};
$code_id = static function ( array $created ) use ( $wpdb ) {
	return (int) $wpdb->get_var( $wpdb->prepare( 'SELECT id FROM ' . Schema::table( 'bookings' ) . ' WHERE code = %s', $created['booking']['code'] ) );
};
$bookings_in = static function ( int $module ) use ( $wpdb ) {
	return (int) $wpdb->get_var( $wpdb->prepare( 'SELECT COUNT(*) FROM ' . Schema::table( 'bookings' ) . ' WHERE module_id = %d', $module ) );
};

echo "== workshop: vehicles\n";
as_user( 0 );
list( $s, $list ) = call( 'GET', "/modules/{$g['id']}/supported-vehicles" );
check( 200 === $s && array( 'Honda City', 'Toyota Vios' ) === array_map( static function ( $v ) {
	return $v['make'] . ' ' . $v['model'];
}, $list ) && 2015 === $list[0]['year_from'], 'the supported models are public', $list );
list( $s, $d ) = $gbook( $g, array( 'oil' ), $gat( $tomorrow, '10:00' ), array( 'vehicle' => null ) );
check( 422 === $s && 'vehicle' === $d['data']['field'], 'a workshop booking needs a vehicle', $d );
list( $s, $d ) = $gbook( $g, array( 'oil' ), $gat( $tomorrow, '10:00' ), array( 'vehicle_id' => 1 ) );
check( 422 === $s && 'vehicle_id' === $d['data']['field'], 'a saved vehicle id is not taken from the public (no sign-in yet)', $d );
list( $s, $d ) = $gbook( $g, array( 'oil' ), $gat( $tomorrow, '10:00' ), array( 'vehicle' => array( 'plate_no' => 'XYZ1', 'supported_vehicle_id' => 999999 ) ) );
check( 422 === $s && 'incompatible_vehicle' === $d['code'] && 0 === $bookings_in( $g['id'] ), 'an unsupported vehicle is refused and nothing is booked', $d );
foreach ( array( 2010, 2023, null ) as $y ) {
	list( $s, $d ) = $gbook( $g, array( 'oil' ), $gat( $tomorrow, '10:00' ), array( 'vehicle' => array( 'plate_no' => 'C1', 'supported_vehicle_id' => $g['car']['City'], 'year' => $y ) ) );
	check( 422 === $s && 'incompatible_vehicle' === $d['code'], 'Honda City ' . var_export( $y, true ) . ': outside 2015-2022', $d );
}
list( $s, $b ) = $gbook( $g, array( 'oil' ), $gat( $tomorrow, '15:00' ), array( 'vehicle' => array( 'plate_no' => 'C1', 'supported_vehicle_id' => $g['car']['City'], 'year' => 2018 ) ) );
check( 201 === $s && 'Honda' === $b['booking']['vehicle']['make'] && 2018 === $b['booking']['vehicle']['year'], 'a 2018 City is booked', $b );
$same = array( 'name' => 'Aisha', 'phone' => '+60123456789' );
list( $s ) = $gbook( $g, array( 'oil' ), $gat( $tomorrow, '16:00' ), array( 'customer' => $same, 'vehicle' => array( 'plate_no' => 'abc 123', 'supported_vehicle_id' => $g['car']['Vios'] ) ) );
list( $s2, $b ) = $gbook( $g, array( 'oil' ), $gat( $tomorrow, '17:00' ), array( 'customer' => $same, 'vehicle' => array( 'plate_no' => 'ABC-123', 'supported_vehicle_id' => $g['car']['Vios'] ) ) );
$vehicles = (int) $wpdb->get_var( $wpdb->prepare( 'SELECT COUNT(*) FROM ' . Schema::table( 'vehicles' ) . ' v JOIN ' . Schema::table( 'customers' ) . ' c ON c.id = v.customer_id WHERE c.phone = %s', '+60123456789' ) );
check( 201 === $s && 201 === $s2 && 1 === $vehicles && 'ABC123' === $b['booking']['vehicle']['plate_no'], '"abc 123" and "ABC-123" from one phone are one vehicle', array( $s, $s2, $vehicles ) );
list( $s, $d ) = call( 'POST', "/modules/$mid/bookings", array( 'service_ids' => array( $svc['id'] ), 'start' => $at( '12:00' ), 'customer' => array( 'name' => 'A', 'phone' => '+60175550001' ), 'vehicle' => array( 'plate_no' => 'A1', 'supported_vehicle_id' => $g['car']['Vios'] ) ) );
check( 422 === $s && 'vehicle' === $d['data']['field'], 'a vehicle on a spa booking: 422', $d );

echo "== workshop: mechanic and bay\n";
list( , $av ) = call( 'GET', "/modules/{$g['id']}/availability", null, array( 'date' => $day3, 'services' => "{$g['svc']['oil']},{$g['svc']['brake']}" ) );
$first = $av['slots'][0];
check( $gat( $day3, '09:00' ) === $first['start'] && $gat( $day3, '12:00' ) === $first['end'] && isset( $first['bay_ids'] ) && array() === $av['lead_time_notice'], 'oil + brake: 09:00-12:00, with mechanics, bays and an empty lead-time notice', $first );
$starts = wp_list_pluck( $av['slots'], 'start' );
check( in_array( $gat( $day3, '10:00' ), $starts, true ) && ! in_array( $gat( $day3, '10:15' ), $starts, true ) && in_array( $gat( $day3, '15:00' ), $starts, true ) && ! in_array( $gat( $day3, '15:15' ), $starts, true ), 'a segment may not cross the 13:00 break, and a 3-hour job ends by 18:00' );
list( $s, $b ) = $gbook( $g, array( 'oil', 'brake' ), $gat( $day3, '09:00' ) );
$jid = $code_id( $b );
check( 201 === $s && array( array( 'bay', 'Bay 1', '09:00', '10:00', 'active' ), array( 'staff', 'Ali', '09:00', '10:00', 'active' ), array( 'bay', 'Bay 1', '10:00', '12:00', 'active' ), array( 'staff', 'Ali', '10:00', '12:00', 'active' ) ) === $rows( $jid ) && $gat( $day3, '12:00' ) === $b['booking']['end'], 'stacked job: two rows per segment, same mechanic and bay throughout', $rows( $jid ) );
check( array( 'Ali' ) === array_column( $b['booking']['staff'], 'name' ) && array( 'Bay 1' ) === array_column( $b['booking']['bays'], 'name' ), 'the booking names its mechanic and bay' );
list( $s, $b ) = $gbook( $g, array( 'oil' ), $gat( $day3, '09:00' ), array( 'staff_id' => $g['mech']['Ali'] ) );
check( 409 === $s, 'naming the busy mechanic: 409' );
as_user( 1 );
call( 'PUT', "/admin/bays/{$g['bay']['Bay 2']}/services", array( 'service_ids' => array( $g['svc']['oil'] ) ) );
list( , $bs ) = call( 'GET', "/admin/bays/{$g['bay']['Bay 2']}/services" );
check( array( $g['svc']['oil'] ) === $bs['service_ids'], 'bay qualifications saved' );
as_user( 0 );
list( $s, $b ) = $gbook( $g, array( 'oil' ), $gat( $day3, '09:00' ) );
check( 201 === $s && array( 'Bala' ) === array_column( $b['booking']['staff'], 'name' ) && array( 'Bay 2' ) === array_column( $b['booking']['bays'], 'name' ), 'the second mechanic and the only other bay', $b['booking'] ?? $b );
list( $s ) = $gbook( $g, array( 'oil' ), $gat( $day3, '09:00' ) );
check( 409 === $s, 'both mechanics and bays busy at 09:00: 409' );

echo "== workshop: parts lead time\n";
as_user( 1 );
list( , $pads ) = call( 'POST', "/admin/modules/{$g['id']}/products", array( 'name' => 'Brake pads', 'stock_status' => 'available_on_order', 'estimated_lead_time_days' => 5 ) );
list( $s, $d ) = call( 'PUT', "/admin/services/{$g['svc']['brake']}/parts", array( 'product_ids' => array( $pads['id'] ) ) );
check( 200 === $s && array( $pads['id'] ) === $d['product_ids'], 'brake service needs brake pads', $d );
list( $s ) = call( 'PUT', "/admin/services/{$g['svc']['oil']}/parts", array( 'product_ids' => array( 999999 ) ) );
check( 404 === $s, 'an unknown part on a service: 404' );
as_user( 0 );
list( , $av ) = call( 'GET', "/modules/{$g['id']}/availability", null, array( 'date' => $day3, 'services' => (string) $g['svc']['brake'] ) );
list( , $av2 ) = call( 'GET', "/modules/{$g['id']}/availability", null, array( 'date' => $day3, 'services' => (string) $g['svc']['oil'] ) );
check( array( array( 'name' => 'Brake pads', 'estimated_lead_time_days' => 5 ) ) === $av['lead_time_notice'] && array() === $av2['lead_time_notice'], 'availability lists the on-order part for brakes only', $av['lead_time_notice'] );
$before = $bookings_in( $g['id'] );
list( $s, $d ) = $gbook( $g, array( 'brake' ), $gat( $day3, '14:00' ) );
check( 422 === $s && 'lead_time_not_acknowledged' === $d['code'] && 'Brake pads' === $d['data']['parts'][0]['name'] && $before === $bookings_in( $g['id'] ), 'booking without acknowledging: 422, nothing written', $d );
list( $s, $b ) = $gbook( $g, array( 'brake' ), $gat( $day3, '14:00' ), array( 'acknowledge_lead_time' => true ) );
check( 201 === $s && true === $b['booking']['lead_time_acknowledged'], 'acknowledged: booked and recorded', $b );
as_user( 1 );
call( 'PATCH', "/admin/products/{$pads['id']}", array( 'stock_status' => 'in_stock', 'stock_qty' => 4 ) );
as_user( 0 );
list( $s ) = $gbook( $g, array( 'brake' ), $gat( $day3, '15:00' ) );
check( 201 === $s, 'the part arrived: no acknowledgement needed' );

echo "== workshop: waiting for parts and resume\n";
as_user( 1 );
$ng = $garage( 'Night Garage', array( 'M1', 'M2', 'M3', 'M4', 'M5', 'M6' ), array( 'B1', 'B2', 'B3', 'B4', 'B5', 'B6' ), null, array( 'start_time' => '00:00', 'end_time' => '23:45' ) );
call( 'PATCH', "/admin/policies/{$ng['policy']}", array( 'min_notice_booking_minutes' => 0 ) );
$T2   = (int) ( ceil( ( time() + 300 ) / 900 ) * 900 );
$hm2  = static function ( int $ts ) use ( $tz ) {
	return ( new DateTimeImmutable( '@' . $ts ) )->setTimezone( $tz )->format( 'H:i' );
};
$soonjob = static function ( array $svcs, string $mech, array $extra = array() ) use ( $gbook, $ng, $T2, $code_id ) {
	list( $s, $b ) = $gbook( $ng, $svcs, ( new DateTimeImmutable( '@' . $T2 ) )->setTimezone( wp_timezone() )->format( DATE_ATOM ), array_merge( array( 'staff_id' => $ng['mech'][ $mech ] ), $extra ) );
	return 201 === $s ? $code_id( $b ) : 0;
};
$st = static function ( int $id, string $to ) {
	return call( 'PATCH', "/admin/bookings/$id/status", array( 'status' => $to ) );
};
$j1 = $soonjob( array( 'brake' ), 'M1' );
list( $s ) = $st( $j1, 'waiting-for-parts' );
check( $j1 > 0 && 409 === $s, 'waiting for parts needs the job in progress first' );
$st( $j1, 'in-progress' );
list( $s, $d ) = $st( $j1, 'waiting-for-parts' );
check( 200 === $s && 'waiting-for-parts' === $d['status'] && array( 'released', 'released' ) === array_column( $rows( $j1 ), 4 ), 'not started yet: mechanic and bay released', $rows( $j1 ) );
list( $s, $b ) = $gbook( $ng, array( 'oil' ), ( new DateTimeImmutable( '@' . $T2 ) )->setTimezone( $tz )->format( DATE_ATOM ), array( 'staff_id' => $ng['mech']['M1'] ) );
check( 201 === $s, 'the freed mechanic can be booked again' );
list( $s, $d ) = $st( $j1, 'confirmed' );
check( 409 === $s && false !== strpos( $d['message'], 'rescheduled' ), 'a waiting job is resumed, not confirmed', $d );
list( $s ) = $st( $j1, 'completed' );
check( 409 === $s, 'a waiting job cannot be completed' );
list( $s ) = call( 'PATCH', "/admin/bookings/$p1id/status", array( 'status' => 'waiting-for-parts' ) );
check( 409 === $s, 'a spa booking never waits for parts' );

$j2 = $soonjob( array( 'oil', 'brake' ), 'M2' ); // T2 .. T2+60 oil, T2+60 .. T2+180 brake
$st( $j2, 'in-progress' );
JustSimple\Booking\Booking\BookingService::transition( $j2, 'waiting-for-parts', 'staff', 1, null, $T2 + 90 * 60 ); // as if 90 minutes in
$cut = $rows( $j2 );
check( array( array( 'bay', 'B2', $hm2( $T2 ), $hm2( $T2 + 3600 ), 'active' ), array( 'staff', 'M2', $hm2( $T2 ), $hm2( $T2 + 3600 ), 'active' ), array( 'bay', 'B2', $hm2( $T2 + 3600 ), $hm2( $T2 + 5400 ), 'active' ), array( 'staff', 'M2', $hm2( $T2 + 3600 ), $hm2( $T2 + 5400 ), 'active' ) ) === $cut, 'running job: finished segment kept, running segment cut at now', $cut );

$later = ( new DateTimeImmutable( 'today +4 days', $tz ) )->format( 'Y-m-d' );
list( $s, $d ) = call( 'POST', "/admin/bookings/$j1/resume", array( 'start' => $gat( $later, '10:00' ) ) );
check( 200 === $s && 'confirmed' === $d['status'] && $gat( $later, '12:00' ) === $d['end'], 'resumed: confirmed, the job moves to the new time', $d );
$live = array_values( array_filter( $rows( $j1 ), static function ( $x ) {
	return 'active' === $x[4];
} ) );
check( 2 === count( $live ) && '10:00' === $live[0][2], 'new mechanic and bay rows at 10:00', $live );
list( $s ) = call( 'POST', "/admin/bookings/$j1/resume", array( 'start' => $gat( $later, '14:00' ) ) );
check( 409 === $s, 'resuming a job that is no longer waiting: 409' );
list( $s, $d ) = call( 'POST', "/admin/bookings/$j2/resume", array( 'start' => $gat( $later, '09:00' ), 'service_ids' => array( $ng['svc']['brake'] ) ) );
check( 200 === $s && $gat( $later, '11:00' ) === $d['end'], 'resume only the remaining brake service: 2 hours', $d );

$j3 = $soonjob( array( 'oil' ), 'M3' );
$st( $j3, 'in-progress' );
$st( $j3, 'waiting-for-parts' );
list( $s ) = call( 'POST', "/admin/bookings/$j3/resume", array( 'start' => ( new DateTimeImmutable( '-1 hour', $tz ) )->format( DATE_ATOM ) ) );
check( 422 === $s, 'resume in the past: 422' );
list( $s ) = call( 'POST', "/admin/bookings/$j3/resume", array( 'start' => $gat( $later, '15:00' ), 'service_ids' => array( $ng['svc']['brake'] ) ) );
check( 422 === $s, 'resume a service not on the job: 422' );
list( $s, $d ) = call( 'POST', "/admin/bookings/$j3/resume", array( 'start' => $gat( $later, '10:00' ), 'staff_id' => $ng['mech']['M1'] ) );
check( 409 === $s && 'slot_unavailable' === $d['code'] && isset( $d['data']['slots'] ), 'resume onto a busy mechanic: 409 with the free slots', $d );
$far = ( new DateTimeImmutable( 'today +70 days', $tz ) )->format( 'Y-m-d' );
list( $s ) = call( 'POST', "/admin/bookings/$j3/resume", array( 'start' => $gat( $far, '10:00' ) ) );
check( 200 === $s, 'an admin resume may go beyond the 60-day booking window' );

echo "== workshop: the job's parts\n";
list( , $filter ) = call( 'POST', "/admin/modules/{$ng['id']}/products", array( 'name' => 'Oil filter', 'stock_qty' => 5, 'price' => '25' ) );
list( , $plug )   = call( 'POST', "/admin/modules/{$ng['id']}/products", array( 'name' => 'Spark plug', 'stock_qty' => 1, 'price' => '10' ) );
$parts = static function ( int $id, array $lines ) {
	return call( 'PUT', "/admin/bookings/$id/parts", array( 'parts' => array_map( static function ( $pid, $q ) {
		return array( 'product_id' => $pid, 'quantity' => $q );
	}, array_keys( $lines ), $lines ) ) );
};
$stock = static function ( int $id ) use ( $wpdb ) {
	$r = $wpdb->get_row( $wpdb->prepare( 'SELECT stock_qty, stock_status FROM ' . Schema::table( 'products' ) . ' WHERE id = %d', $id ), ARRAY_A );
	return array( (int) $r['stock_qty'], $r['stock_status'] );
};
$j4 = $soonjob( array( 'oil' ), 'M4' );
list( $s, $d ) = $parts( $j4, array( $filter['id'] => 2 ) );
check( 200 === $s && '25.00' === $d['parts'][0]['unit_price'] && false === $d['parts'][0]['needs_ordering'], 'parts go on the job at today\'s price', $d );
call( 'PATCH', "/admin/products/{$filter['id']}", array( 'price' => '30' ) );
list( $s, $d ) = $parts( $j4, array( $filter['id'] => 3, $plug['id'] => 2 ) );
$by = array_column( $d['parts'], null, 'name' );
check( 200 === $s && '25.00' === $by['Oil filter']['unit_price'] && 3 === $by['Oil filter']['quantity'] && '10.00' === $by['Spark plug']['unit_price'] && true === $by['Spark plug']['needs_ordering'], 'the list is replaced; old lines keep their price; 2 plugs > 1 on the shelf needs ordering', $d );
list( $s ) = $parts( $j4, array( $filter['id'] => 1, 999999 => 1 ) );
list( , $now_list ) = call( 'GET', "/admin/bookings/$j4/parts" );
check( 404 === $s && 2 === count( $now_list['parts'] ), 'a bad list changes nothing' );
list( $s ) = call( 'PUT', "/admin/bookings/$j4/parts", array( 'parts' => array( array( 'product_id' => $filter['id'], 'quantity' => 1 ), array( 'product_id' => $filter['id'], 'quantity' => 1 ) ) ) );
check( 422 === $s, 'a part listed twice: 422' );
$many = array();
for ( $i = 0; $i < 51; $i++ ) {
	$many[] = array( 'product_id' => $filter['id'] + $i, 'quantity' => 1 );
}
list( $s ) = call( 'PUT', "/admin/bookings/$j4/parts", array( 'parts' => $many ) );
check( 422 === $s, 'more than 50 lines: 422' );
list( $s ) = call( 'PUT', "/admin/bookings/$p1id/parts", array( 'parts' => array() ) );
check( 422 === $s, 'a spa booking has no parts list' );

$mech_user = get_user_by( 'login', 'jsb-mechanic' );
$muid      = $mech_user ? $mech_user->ID : wp_insert_user( array( 'user_login' => 'jsb-mechanic', 'user_pass' => wp_generate_password( 24 ), 'role' => 'editor', 'user_email' => 'mechanic@example.test' ) );
call( 'PATCH', "/admin/staff/{$ng['mech']['M5']}", array( 'wp_user_id' => $muid ) );
$j5 = $soonjob( array( 'oil' ), 'M5' );
as_user( $muid );
list( $s ) = $parts( $j4, array( $filter['id'] => 1 ) );
list( $s2 ) = $parts( $j5, array( $filter['id'] => 1 ) );
list( $s3 ) = call( 'POST', "/admin/bookings/$j4/resume", array( 'start' => $gat( $later, '16:00' ) ) );
check( 403 === $s && 200 === $s2 && 403 === $s3, 'a mechanic edits parts on their own job only; resume too', array( $s, $s2, $s3 ) );
as_user( 1 );

$st( $j4, 'in-progress' );
check( array( 5, 'in_stock' ) === $stock( $filter['id'] ), 'nothing leaves the shelf while the job runs' );
list( $s, $d ) = $st( $j4, 'completed' );
check( 200 === $s && array( 2, 'in_stock' ) === $stock( $filter['id'] ) && array( 0, 'out_of_stock' ) === $stock( $plug['id'] ), 'completed: 3 filters and the 1 plug taken (never below 0)', array( $stock( $filter['id'] ), $stock( $plug['id'] ) ) );
list( $s ) = $parts( $j4, array() );
check( 409 === $s, 'a completed job\'s parts can no longer change' );
$st( $j5, 'cancelled' );
check( array( 2, 'in_stock' ) === $stock( $filter['id'] ), 'a job that never completes takes nothing' );
list( , $gasket ) = call( 'POST', "/admin/modules/{$ng['id']}/products", array( 'name' => 'Gasket', 'stock_qty' => 1, 'stock_status' => 'available_on_order', 'estimated_lead_time_days' => 2 ) );
$j6 = $soonjob( array( 'oil' ), 'M6' );
$j7 = $soonjob( array( 'oil' ), 'M5' ); // M5's job was cancelled, so M5 is free again (M1 still has the rebooked oil change)
foreach ( array( $j6, $j7 ) as $j ) {
	$st( $j, 'in-progress' );
	$parts( $j, array( $gasket['id'] => 1 ) );
}
$st( $j6, 'completed' );
$st( $j7, 'completed' );
check( $j6 && $j7 && array( 0, 'available_on_order' ) === $stock( $gasket['id'] ), 'two jobs on the last gasket: 0, and the on-order label stays', $stock( $gasket['id'] ) );

echo "== workshop: the parts-waiting flow end to end\n";
$j8 = $soonjob( array( 'oil' ), 'M4' ); // M4's job was completed; M2 still holds its finished first segment
$st( $j8, 'in-progress' );
list( , $d ) = $parts( $j8, array( $plug['id'] => 2 ) );
$flag = $d['parts'][0]['needs_ordering'];
$st( $j8, 'waiting-for-parts' );
call( 'PATCH', "/admin/products/{$plug['id']}", array( 'stock_qty' => 4, 'stock_status' => 'in_stock' ) );
list( , $d ) = call( 'GET', "/admin/bookings/$j8/parts" );
$after = $d['parts'][0]['needs_ordering'];
list( $s ) = call( 'POST', "/admin/bookings/$j8/resume", array( 'start' => ( new DateTimeImmutable( '@' . ( $T2 + 15 * 60 ) ) )->setTimezone( $tz )->format( DATE_ATOM ) ) );
$st( $j8, 'in-progress' );
list( $s2, $d ) = $st( $j8, 'completed' );
check( true === $flag && false === $after && 200 === $s && 200 === $s2 && array( 2, 'in_stock' ) === $stock( $plug['id'] ), 'needs ordering -> waiting -> stock arrives -> resumed -> completed, parts taken', array( $flag, $after, $s, $s2, $stock( $plug['id'] ) ) );
$mail = mails();
check( (bool) array_filter( $mail, static function ( $x ) {
	return false !== strpos( $x->message, 'Vehicle: ' );
} ), 'the emails name the vehicle' );

echo "== workshop race fixture\n";
$wsrace = array();
foreach ( array( '3x1' => array( 3, 1 ), '1x3' => array( 1, 3 ), '2x2' => array( 2, 2 ) ) as $k => $n ) {
	$x = $garage( "Race $k", array_map( static function ( $i ) {
		return "M$i";
	}, range( 1, $n[0] ) ), array_map( static function ( $i ) {
		return "B$i";
	}, range( 1, $n[1] ) ), array( '08:00', '18:00' ), $day_shift );
	$wsrace[ $k ] = array( 'module' => $x['id'], 'oil' => $x['svc']['oil'], 'brake' => $x['svc']['brake'], 'car' => $x['car']['Vios'] );
}
$wsrace['day']  = $day3;
$jp             = $soonjob( array( 'oil' ), 'M3' );
$st( $jp, 'in-progress' );
list( , $pa ) = call( 'POST', "/admin/modules/{$ng['id']}/products", array( 'name' => 'Race part A', 'stock_qty' => 10 ) );
list( , $pb ) = call( 'POST', "/admin/modules/{$ng['id']}/products", array( 'name' => 'Race part B', 'stock_qty' => 10 ) );
$jc = $soonjob( array( 'oil' ), 'M4' );
$st( $jc, 'in-progress' );
$parts( $jc, array( $pa['id'] => 1 ) );
$wsrace['parts'] = array( 'job' => $jp, 'complete_job' => $jc, 'a' => $pa['id'], 'b' => $pb['id'] );

echo "== field: setup\n";
as_user( 1 );
foreach ( array( array( 'location_type' => 'on_site' ), array( 'service_category' => 'inspection' ), array( 'travel_buffer_minutes' => 15 ) ) as $extra ) {
	list( $s, $d ) = call( 'POST', "/admin/modules/$mid/services", array( 'name' => 'X', 'duration_minutes' => 30 ) + $extra );
	check( 422 === $s && key( $extra ) === $d['data']['field'], key( $extra ) . ' refused on a staff module', $d );
}
const KLP      = array( 3.1390, 101.6869 );
const NEARP    = array( 3.1200, 101.6700 );
const SUBANGP  = array( 3.0738, 101.5183 );
const SEREMBANP = array( 2.7297, 101.9381 );
$practice = static function ( string $title, array $pros, ?array $hours, array $shift, ?float $radius = 25.0 ) {
	list( , $m ) = call( 'POST', '/admin/modules', array( 'title' => $title, 'type' => 'field' ) );
	$settings    = null === $radius ? array() : array( 'latitude' => KLP[0], 'longitude' => KLP[1], 'service_radius_km' => $radius );
	call( 'PATCH', "/admin/modules/{$m['id']}", array( 'status' => 'active', 'settings' => $settings ) );
	if ( null !== $hours ) {
		call( 'PUT', "/admin/modules/{$m['id']}/hours", array( 'hours' => array_map( static function ( $d ) use ( $hours ) {
			return array( 'weekday' => $d, 'open_time' => $hours[0], 'close_time' => $hours[1] );
		}, range( 0, 6 ) ) ) );
	}
	$out = array( 'id' => $m['id'], 'policy' => $m['default_policy_id'], 'pro' => array(), 'svc' => array() );
	foreach ( $pros as $n ) {
		list( , $r ) = call( 'POST', "/admin/modules/{$m['id']}/staff", array( 'name' => $n ) );
		call( 'PUT', "/admin/staff/{$r['id']}/shifts", array( 'shifts' => array_map( static function ( $d ) use ( $shift ) {
			return array( 'weekday' => $d ) + $shift;
		}, range( 0, 6 ) ) ) );
		$out['pro'][ $n ] = $r['id'];
	}
	foreach ( array(
		'site'   => array( 'Site inspection', 60, 'inspection', 'on_site', 30 ),
		'work'   => array( 'Repair work', 120, 'standard', 'on_site', 30 ),
		'office' => array( 'Office consult', 60, 'inspection', 'in_office', 30 ),
		'video'  => array( 'Video call', 30, 'inspection', 'virtual', 0 ),
	) as $k => $x ) {
		list( , $r ) = call( 'POST', "/admin/modules/{$m['id']}/services", array( 'name' => $x[0], 'duration_minutes' => $x[1], 'service_category' => $x[2], 'location_type' => $x[3], 'travel_buffer_minutes' => $x[4] ) );
		$out['svc'][ $k ] = $r['id'];
	}
	return $out;
};
$fp = $practice( 'Test Practice', array( 'Dr Lim' ), array( '08:00', '18:00' ), $day_shift );
list( , $fm ) = call( 'GET', "/admin/modules/{$fp['id']}" );
check( 25.0 === (float) $fm['settings']['service_radius_km'] && KLP[0] === (float) $fm['settings']['latitude'], 'the business location is saved in the module settings', $fm['settings'] );
foreach ( array( array( 'latitude' => 91 ), array( 'longitude' => 181 ), array( 'service_radius_km' => 0 ) ) as $bad ) {
	list( $s, $d ) = call( 'PATCH', "/admin/modules/{$fp['id']}", array( 'settings' => $bad ) );
	check( 422 === $s, 'settings.' . key( $bad ) . ' out of range: 422', $d );
}
foreach ( array( array( 'location_type' => 'moon' ), array( 'service_category' => 'weird' ), array( 'travel_buffer_minutes' => -5 ) ) as $bad ) {
	list( $s ) = call( 'POST', "/admin/modules/{$fp['id']}/services", array( 'name' => 'X', 'duration_minutes' => 30 ) + $bad );
	check( 422 === $s, key( $bad ) . ' must be a known value' );
}
list( $s, $d ) = call( 'PATCH', "/admin/staff/{$fp['pro']['Dr Lim']}", array( 'meeting_link' => 'javascript:alert(1)' ) );
check( 422 === $s && 'meeting_link' === $d['data']['field'], 'a meeting room must be an http(s) link', $d );
list( $s, $d ) = call( 'PATCH', "/admin/staff/{$fp['pro']['Dr Lim']}", array( 'meeting_link' => 'https://meet.example/dr-lim' ) );
check( 200 === $s && 'https://meet.example/dr-lim' === $d['meeting_link'], 'the professional\'s room saved', $d );

echo "== field: radius and travel\n";
as_user( 0 );
list( , $pub ) = call( 'GET', "/modules/{$fp['id']}" );
$site6 = array_column( $pub['services'], null, 'name' )['Site inspection'];
check( ! isset( $pub['staff'][0]['meeting_link'] ) && array( 'on_site', 'inspection', 30 ) === array( $site6['location_type'], $site6['service_category'], $site6['travel_buffer_minutes'] ), 'public page: where and how each service happens, and no meeting rooms', $site6 );
$fav = static function ( string $svc, ?array $pin, string $day ) use ( $fp ) {
	$q = array( 'date' => $day, 'services' => (string) $fp['svc'][ $svc ] );
	if ( $pin ) {
		$q += array( 'latitude' => $pin[0], 'longitude' => $pin[1] );
	}
	return call( 'GET', "/modules/{$fp['id']}/availability", null, $q );
};
$fstarts = static function ( array $resp ) use ( $tz ) {
	return array_map( static function ( $x ) use ( $tz ) {
		return ( new DateTimeImmutable( $x['start'] ) )->setTimezone( $tz )->format( 'H:i' );
	}, $resp['slots'] ?? array() );
};
list( $s, $d ) = $fav( 'site', null, $tomorrow );
check( 422 === $s && 'latitude' === $d['data']['field'], 'on-site availability needs coordinates', $d );
list( $s, $d ) = $fav( 'site', NEARP, $tomorrow );
$st6 = $fstarts( $d );
check( 200 === $s && '09:30' === $st6[0] && '16:30' === end( $st6 ), 'near: 09:30 to 16:30 (the drive fits the shift)', $st6 );
list( $s ) = $fav( 'site', SUBANGP, $tomorrow );
check( 200 === $s, '~20 km is inside the 25 km radius' );
list( $s, $d ) = $fav( 'site', SEREMBANP, $tomorrow );
check( 422 === $s && 'outside_service_area' === $d['code'] && 25.0 === (float) $d['data']['service_radius_km'] && $d['data']['distance_km'] > 50, 'Seremban: outside the service area, with the distance', $d );
list( , $d ) = $fav( 'office', null, $tomorrow );
$st6 = $fstarts( $d );
check( '09:00' === $st6[0] && '17:00' === end( $st6 ), 'in office: no travel, 09:00 to 17:00', $st6 );

$fbook = static function ( string $svc, string $start, array $extra = array(), array $pp = null ) use ( $fp ) {
	$pp = $pp ?? $fp;
	return call( 'POST', "/modules/{$pp['id']}/bookings", array_merge( array(
		'service_ids' => array( $pp['svc'][ $svc ] ),
		'start'       => $start,
		'customer'    => array( 'name' => 'Client', 'phone' => '+6016' . random_int( 1000000, 9999999 ), 'email' => 'client@example.test' ),
	), $extra ) );
};
$pin = static function ( array $p ) {
	return array( 'line1' => '1 Jalan Test', 'city' => 'Kuala Lumpur', 'postcode' => '50450', 'latitude' => $p[0], 'longitude' => $p[1] );
};
$before = $bookings_in( $fp['id'] );
list( $s, $d ) = $fbook( 'site', $gat( $tomorrow, '10:00' ), array( 'address' => $pin( SEREMBANP ) ) );
check( 422 === $s && 'outside_service_area' === $d['code'] && $before === $bookings_in( $fp['id'] ), 'booking outside the radius: refused, nothing written', $d );
list( $s, $d ) = $fbook( 'site', $gat( $tomorrow, '10:00' ) );
check( 422 === $s && 'address' === $d['data']['field'], 'on site needs an address', $d );
list( $s, $d ) = $fbook( 'site', $gat( $tomorrow, '10:00' ), array( 'address' => array( 'line1' => '1 Jalan Test' ) ) );
check( 422 === $s && 'address.latitude' === $d['data']['field'], 'an address without a map pin: 422 (no geocoding)', $d );
list( $s, $d ) = $fbook( 'site', $gat( $tomorrow, '10:00' ), array( 'address_id' => 1 ) );
check( 422 === $s && 'address_id' === $d['data']['field'], 'a saved address id is not taken from the public', $d );
list( $s, $d ) = $fbook( 'office', $gat( $tomorrow, '10:00' ), array( 'address' => $pin( NEARP ) ) );
check( 422 === $s && 'address' === $d['data']['field'], 'an address on an office visit: 422', $d );
list( $s, $d ) = call( 'POST', "/modules/{$fp['id']}/bookings", array( 'service_ids' => array( $fp['svc']['office'], $fp['svc']['video'] ), 'start' => $gat( $tomorrow, '10:00' ), 'customer' => array( 'name' => 'C', 'phone' => '+60160000001' ) ) );
check( 422 === $s, 'one service per booking' );
list( $s, $b ) = $fbook( 'site', $gat( $tomorrow, '10:00' ), array( 'address' => $pin( NEARP ) ) );
$sid1 = $code_id( $b );
check( 201 === $s && $gat( $tomorrow, '10:00' ) === $b['booking']['start'] && $gat( $tomorrow, '11:00' ) === $b['booking']['end'] && array( array( 'staff', 'Dr Lim', '09:30', '11:30', 'active' ) ) === $rows( $sid1 ), 'booked 10:00-11:00; the professional is held 09:30-11:30', $rows( $sid1 ) );
check( '1 Jalan Test' === $b['booking']['address']['line1'] && ! isset( $b['booking']['address']['latitude'] ), 'the booking shows the address, not the pin', $b['booking']['address'] );
list( , $d ) = $fav( 'site', NEARP, $tomorrow );
$st6 = $fstarts( $d );
check( '14:30' === $st6[0] && ! in_array( '11:30', $st6, true ), 'travel shapes the next visit: first fit is 14:30', $st6 );
list( , $d ) = $fav( 'office', null, $tomorrow );
$st6 = $fstarts( $d );
check( in_array( '11:30', $st6, true ) && ! in_array( '11:15', $st6, true ), 'an office visit only waits for the drive back', $st6 );
as_user( 1 );
$nop = $practice( 'No Location Practice', array( 'Dr X' ), array( '08:00', '18:00' ), $day_shift, null );
as_user( 0 );
list( $s, $d ) = call( 'GET', "/modules/{$nop['id']}/availability", null, array( 'date' => $tomorrow, 'services' => (string) $nop['svc']['site'], 'latitude' => NEARP[0], 'longitude' => NEARP[1] ) );
list( $s2 ) = call( 'GET', "/modules/{$nop['id']}/availability", null, array( 'date' => $tomorrow, 'services' => (string) $nop['svc']['office'] ) );
check( 422 === $s && false !== strpos( $d['message'], 'service radius' ) && 200 === $s2, 'no location set: on-site refused clearly, office unaffected', $d );

echo "== field: meeting links and price after inspection\n";
list( $s, $b ) = $fbook( 'video', $gat( $tomorrow, '15:00' ) );
check( 201 === $s && 'https://meet.example/dr-lim' === $b['booking']['meeting_link'], 'a confirmed online appointment gets the professional\'s room', $b['booking'] );
list( $s, $b ) = $fbook( 'office', $gat( $tomorrow, '16:00' ) );
check( 201 === $s && null === $b['booking']['meeting_link'], 'an office visit gets no link' );
as_user( 1 );
list( , $apol ) = call( 'POST', "/admin/modules/{$fp['id']}/policies", array( 'name' => 'Approval', 'default_status' => 'pending' ) );
list( , $vsvc ) = call( 'POST', "/admin/modules/{$fp['id']}/services", array( 'name' => 'Video (approval)', 'duration_minutes' => 30, 'location_type' => 'virtual', 'service_category' => 'inspection', 'policy_id' => $apol['id'] ) );
as_user( 0 );
list( $s, $b ) = call( 'POST', "/modules/{$fp['id']}/bookings", array( 'service_ids' => array( $vsvc['id'] ), 'start' => $gat( $tomorrow, '17:00' ), 'customer' => array( 'name' => 'Pending', 'phone' => '+60160000002', 'email' => 'pending@example.test' ) ) );
$pv = $code_id( $b );
check( 201 === $s && 'pending' === $b['booking']['status'] && null === $b['booking']['meeting_link'], 'pending: no link yet', $b['booking'] );
as_user( 1 );
list( $s, $d ) = call( 'PATCH', "/admin/bookings/$pv/status", array( 'status' => 'confirmed' ) );
check( 200 === $s && 'https://meet.example/dr-lim' === $d['meeting_link'], 'approved: the room is handed out', $d );
$mails_before = count( mails() );
list( $s ) = call( 'PATCH', "/admin/bookings/$pv/meeting-link", array( 'url' => 'javascript:alert(1)' ) );
check( 422 === $s, 'a link set by hand must be http(s)' );
list( $s, $d ) = call( 'PATCH', "/admin/bookings/$pv/meeting-link", array( 'url' => 'https://meet.example/special' ) );
call( 'PATCH', "/admin/bookings/$pv/meeting-link", array( 'url' => 'https://meet.example/special' ) );
$sent = array_values( array_filter( array_slice( mails(), $mails_before ), static function ( $x ) {
	return false !== strpos( $x->subject, 'meeting link' );
} ) );
check( 200 === $s && 'https://meet.example/special' === $d['meeting_link'] && 1 === count( $sent ) && false !== strpos( $sent[0]->message, 'https://meet.example/special' ), 'a link set by hand wins and is emailed once', count( $sent ) );
list( $s ) = call( 'PATCH', "/admin/bookings/$jid/meeting-link", array( 'url' => 'https://x.example' ) );
check( 409 === $s, 'meeting links are for field bookings only' );
list( , $psvc ) = call( 'POST', "/admin/modules/{$fp['id']}/services", array( 'name' => 'Roof repair', 'duration_minutes' => 60, 'price' => '0', 'price_after_inspection' => true ) );
as_user( 0 );
list( , $pub ) = call( 'GET', "/modules/{$fp['id']}" );
check( true === array_column( $pub['services'], null, 'name' )['Roof repair']['price_after_inspection'], 'customers see the price comes after the inspection' );
call( 'POST', "/modules/{$fp['id']}/bookings", array( 'service_ids' => array( $psvc['id'] ), 'start' => $gat( $day3, '09:00' ), 'customer' => array( 'name' => 'Roof', 'phone' => '+60160000003', 'email' => 'roof@example.test' ) ) );
check( (bool) array_filter( mails(), static function ( $x ) {
	return false !== strpos( $x->message, '(price given after inspection)' );
} ), 'the email says the price comes after the inspection' );

echo "== field: follow-ups and overruns\n";
as_user( 1 );
$np = $practice( 'Night Practice', array( 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8' ), null, array( 'start_time' => '00:00', 'end_time' => '23:45' ) );
call( 'PATCH', "/admin/policies/{$np['policy']}", array( 'min_notice_booking_minutes' => 0 ) );
$T3   = (int) ( ceil( ( time() + 45 * 60 ) / 900 ) * 900 ); // far enough ahead for a 30-minute drive before the visit
$iso3 = static function ( int $ts ) use ( $tz ) {
	return ( new DateTimeImmutable( '@' . $ts ) )->setTimezone( $tz )->format( DATE_ATOM );
};
$nbook = static function ( string $svc, string $pro, int $ts, array $extra = array() ) use ( $fbook, $np, $iso3, $code_id ) {
	list( $s, $b ) = $fbook( $svc, $iso3( $ts ), array_merge( array( 'staff_id' => $np['pro'][ $pro ] ), $extra ), $np );
	return 201 === $s ? $code_id( $b ) : 0;
};
$fst  = static function ( int $id, string $to ) {
	return call( 'PATCH', "/admin/bookings/$id/status", array( 'status' => $to ) );
};
$fu   = static function ( int $parent, string $svc, string $start ) use ( $np ) {
	return call( 'POST', "/admin/bookings/$parent/follow-up", array( 'service_id' => $np['svc'][ $svc ], 'start' => $start ) );
};
$insp  = $nbook( 'site', 'P1', $T3, array( 'address' => $pin( NEARP ) ) );
$later = ( new DateTimeImmutable( 'today +5 days', $tz ) )->format( 'Y-m-d' );
list( $s ) = $fu( $insp, 'work', $gat( $later, '10:00' ) );
check( $insp > 0 && 409 === $s, 'a follow-up waits for the inspection to start' );
$fst( $insp, 'in-progress' );
list( $s, $child ) = $fu( $insp, 'work', $gat( $later, '10:00' ) );
$cid = $child['id'] ?? 0;
$parent = JustSimple\Booking\Booking\BookingService::view( $insp, true );
check( 201 === $s && $insp === $child['follow_up_of'] && 'confirmed' === $child['status'] && 'admin' === $child['source'] && $parent['customer']['id'] === $child['customer']['id'] && '1 Jalan Test' === $child['address']['line1'], 'the follow-up is linked, confirmed, same customer and address', $child );
check( $cid && array( '09:30', '12:30' ) === array_slice( $rows( $cid )[0], 2, 2 ), 'its travel is held too: 09:30-12:30', $cid ? $rows( $cid ) : null );
list( $s ) = $fu( $insp, 'site', $gat( $later, '14:00' ) );
check( 422 === $s, 'a follow-up must be a standard service' );
list( $s ) = $fu( $insp, 'work', ( new DateTimeImmutable( '-2 hours', $tz ) )->format( DATE_ATOM ) );
check( 422 === $s, 'a follow-up starts in the future' );
$offi = $nbook( 'office', 'P2', $T3 );
$fst( $offi, 'in-progress' );
list( $s, $d ) = $fu( $offi, 'work', $gat( $later, '15:00' ) );
check( 422 === $s && false !== strpos( $d['message'], 'address' ), 'an office inspection has no address for on-site work', $d );
call( 'PATCH', "/admin/modules/{$np['id']}", array( 'settings' => array( 'latitude' => SEREMBANP[0], 'longitude' => SEREMBANP[1], 'service_radius_km' => 5 ) ) );
list( $s, $d ) = $fu( $insp, 'work', $gat( $later, '16:00' ) );
check( 422 === $s && 'outside_service_area' === $d['code'], 'an on-site follow-up is checked against the radius again', $d );
call( 'PATCH', "/admin/modules/{$np['id']}", array( 'settings' => array( 'latitude' => KLP[0], 'longitude' => KLP[1], 'service_radius_km' => 25 ) ) );
$direct = $nbook( 'work', 'P3', $T3, array( 'address' => $pin( NEARP ) ) );
$fst( $direct, 'in-progress' );
list( $s, $d ) = $fu( $direct, 'work', $gat( $later, '17:00' ) );
check( 422 === $s && false !== strpos( $d['message'], 'inspection' ), 'only an inspection can have a follow-up', $d );
list( $s ) = call( 'POST', "/admin/bookings/$p1id/follow-up", array( 'service_id' => $svc['id'], 'start' => $gat( $later, '10:00' ) ) );
list( $s2 ) = call( 'PATCH', "/admin/bookings/$p1id/actual-duration", array( 'minutes' => 90 ) );
check( 409 === $s && 409 === $s2, 'follow-ups and overruns are for field bookings only' );

$ov = $nbook( 'office', 'P4', $T3 );
list( $s ) = call( 'PATCH', "/admin/bookings/$ov/actual-duration", array( 'minutes' => 90 ) );
check( 409 === $s, 'only a job in progress can overrun' );
$fst( $ov, 'in-progress' );
list( $s ) = call( 'PATCH', "/admin/bookings/$ov/actual-duration", array( 'minutes' => 0 ) );
check( 422 === $s, 'minutes must be at least 1' );
list( $s, $d ) = call( 'PATCH', "/admin/bookings/$ov/actual-duration", array( 'minutes' => 90 ) );
check( 200 === $s && array() === $d['conflicting_booking_ids'] && 90 === $d['booking']['actual_duration_minutes'] && $iso3( $T3 + 5400 ) === $d['booking']['end'] && array( $hm2( $T3 ), $hm2( $T3 + 5400 ) ) === array_slice( $rows( $ov )[0], 2, 2 ), 'a free overrun stretches the reservation to 90 minutes', $d );
$sh = $nbook( 'office', 'P5', $T3 );
$fst( $sh, 'in-progress' );
list( $s, $d ) = call( 'PATCH', "/admin/bookings/$sh/actual-duration", array( 'minutes' => 40 ) );
check( 200 === $s && 40 === $d['booking']['actual_duration_minutes'] && array( $hm2( $T3 ), $hm2( $T3 + 3600 ) ) === array_slice( $rows( $sh )[0], 2, 2 ), 'a shorter job only records the figure' );
$first = $nbook( 'office', 'P6', $T3 );
$next  = $nbook( 'office', 'P6', $T3 + 3600 );
$fst( $first, 'in-progress' );
list( $s, $d ) = call( 'PATCH', "/admin/bookings/$first/actual-duration", array( 'minutes' => 90 ) );
check( 200 === $s && array( $next ) === $d['conflicting_booking_ids'] && true === $d['booking']['schedule_conflict'] && array( $hm2( $T3 ), $hm2( $T3 + 3600 ) ) === array_slice( $rows( $first )[0], 2, 2 ) && array( $hm2( $T3 + 3600 ), $hm2( $T3 + 7200 ) ) === array_slice( $rows( $next )[0], 2, 2 ), 'an overrun into the next appointment is flagged, not forced', $d );
list( , $flagged ) = call( 'GET', "/admin/modules/{$np['id']}/bookings", null, array( 'from' => wp_date( 'Y-m-d', $T3 ), 'to' => wp_date( 'Y-m-d', $T3 + 7200 ), 'schedule_conflict' => '1' ) );
check( array( $first, $next ) === array_column( $flagged, 'id' ), 'both flagged on the desk', array_column( $flagged, 'id' ) );
call( 'PATCH', "/admin/bookings/$first", array( 'schedule_conflict' => false ) );
list( , $flagged ) = call( 'GET', "/admin/modules/{$np['id']}/bookings", null, array( 'from' => wp_date( 'Y-m-d', $T3 ), 'to' => wp_date( 'Y-m-d', $T3 + 7200 ), 'schedule_conflict' => '1' ) );
check( array( $next ) === array_column( $flagged, 'id' ), 'clearing one leaves the other' );
list( , $fpart ) = call( 'POST', "/admin/modules/{$np['id']}/products", array( 'name' => 'Fuse', 'stock_qty' => 5 ) );
list( $s, $d ) = call( 'PUT', "/admin/bookings/$ov/parts", array( 'parts' => array( array( 'product_id' => $fpart['id'], 'quantity' => 2 ) ) ) );
$fst( $ov, 'completed' );
check( 200 === $s && array( 3, 'in_stock' ) === $stock( $fpart['id'] ), 'field jobs keep a parts list, taken on completion', $stock( $fpart['id'] ) );
check( (bool) array_filter( mails(), static function ( $x ) {
	return false !== strpos( $x->message, 'Visit at: 1 Jalan Test' );
} ), 'the emails say where the visit is' );

echo "== field race fixture\n";
$frace = array( 'day' => ( new DateTimeImmutable( 'today +3 days', $tz ) )->format( 'Y-m-d' ), 'near' => NEARP );
$one   = $practice( 'Race practice one', array( 'Dr A' ), array( '08:00', '18:00' ), $day_shift );
$two   = $practice( 'Race practice two', array( 'Dr A', 'Dr B' ), array( '08:00', '18:00' ), $day_shift );
$three = $practice( 'Race practice three', array( 'Dr A', 'Dr B', 'Dr C' ), array( '08:00', '18:00' ), $day_shift );
$frace += array( 'one' => array( $one['id'], $one['svc']['site'] ), 'two' => array( $two['id'], $two['svc']['site'] ), 'three' => array( $three['id'], $three['svc']['office'] ) );
$or1 = $nbook( 'office', 'P7', $T3 );
$or2 = $nbook( 'office', 'P8', $T3 );
$fst( $or1, 'in-progress' );
$fst( $or2, 'in-progress' );
$frace['overrun'] = array( 'module' => $np['id'], 'office' => $np['svc']['office'], 'jobs' => array( $or1, $or2 ), 'pros' => array( $np['pro']['P7'], $np['pro']['P8'] ), 'next' => $iso3( $T3 + 3600 ) );

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
echo 'RESTRACE ' . wp_json_encode( $rrace ) . "\n";
echo 'WSRACE ' . wp_json_encode( $wsrace ) . "\n";
echo 'FIELDRACE ' . wp_json_encode( $frace ) . "\n";
echo 'RACE ' . wp_json_encode( array( 'module' => $rm['id'], 'service' => $rs['id'], 'staff' => $race_staff, 'start10' => $at( '10:00' ), 'start11' => $at( '11:00' ) ) ) . "\n";
exit( $failures ? 1 : 0 );
