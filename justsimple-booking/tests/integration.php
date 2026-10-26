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
echo 'RACE ' . wp_json_encode( array( 'module' => $rm['id'], 'service' => $rs['id'], 'staff' => $race_staff, 'start10' => $at( '10:00' ), 'start11' => $at( '11:00' ) ) ) . "\n";
exit( $failures ? 1 : 0 );
