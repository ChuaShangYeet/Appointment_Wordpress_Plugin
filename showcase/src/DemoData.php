<?php
namespace JustSimple\Showcase;

defined( 'ABSPATH' ) || exit;

/**
 * The five demo businesses, built through the booking plugin's own REST routes, so the engine checks every setting and allocates
 * every booking: nothing is written around it. Each gets a few bookings in the coming days, so its admin day board has a day.
 * Ported from the booking plugin's tests/seed-demo.php (which also makes a stylist login; the live demo has no logins).
 */
final class DemoData {

	const TITLES = array(
		'staff'      => 'Klinik Dr Wong',
		'salon'      => 'Mei Hair Studio',
		'restaurant' => 'Rumah Makan Seri',
		'workshop'   => 'Bengkel Ah Kow',
		'field'      => 'CoolAir Services',
	);

	/** @var array<int,string> what went wrong, for the admin page */
	private static $errors = array();

	/** @return array{pages: array<string,int>, errors: string[]} */
	public static function build(): array {
		self::$errors = array();
		$pages        = array(
			'staff'      => self::clinic(),
			'salon'      => self::salon(),
			'restaurant' => self::restaurant(),
			'workshop'   => self::workshop(),
			'field'      => self::field(),
		);
		return array( 'pages' => array_filter( $pages ), 'errors' => self::$errors );
	}

	// ------------------------------------------------------------------ REST, as the owner and as a visitor

	private static function call( string $method, string $path, ?array $body = null, array $query = array() ) {
		$req = new \WP_REST_Request( $method, '/justsimple/v1' . $path );
		if ( null !== $body ) {
			$req->set_header( 'content-type', 'application/json' );
			$req->set_body( wp_json_encode( $body ) );
		}
		$req->set_query_params( $query );
		$res = rest_do_request( $req );
		$out = json_decode( wp_json_encode( $res->get_data() ), true );
		if ( $res->get_status() >= 400 ) {
			self::$errors[] = "$method $path: " . ( $out['message'] ?? $res->get_status() );
			return null;
		}
		return $out;
	}

	private static function page( string $kind, array $settings ): ?int {
		$m = self::call( 'POST', '/admin/modules', array( 'title' => self::TITLES[ $kind ], 'type' => $kind ) );
		if ( ! $m ) {
			return null;
		}
		self::call( 'PATCH', "/admin/modules/{$m['id']}", array( 'status' => 'active', 'settings' => $settings ) );
		return (int) $m['id'];
	}

	private static function hours( int $id, string $open, string $close, array $days ) {
		self::call( 'PUT', "/admin/modules/$id/hours", array( 'hours' => array_map( static function ( $d ) use ( $open, $close ) {
			return array( 'weekday' => $d, 'open_time' => $open, 'close_time' => $close );
		}, $days ) ) );
	}

	private static function person( int $id, string $name, array $sh, array $days, array $extra = array() ): ?int {
		$r = self::call( 'POST', "/admin/modules/$id/staff", array( 'name' => $name ) + $extra );
		if ( ! $r ) {
			return null;
		}
		self::call( 'PUT', "/admin/staff/{$r['id']}/shifts", array( 'shifts' => array_map( static function ( $d ) use ( $sh ) {
			return array( 'weekday' => $d, 'start_time' => $sh[0], 'end_time' => $sh[1], 'break_start' => $sh[2] ?? null, 'break_end' => $sh[3] ?? null );
		}, $days ) ) );
		return (int) $r['id'];
	}

	private static function service( int $id, array $fields ): ?int {
		$r = self::call( 'POST', "/admin/modules/$id/services", $fields );
		return $r ? (int) $r['id'] : null;
	}

	/**
	 * One booking, as a visitor would make it: the first time the engine offers at or after `$at` on the day `$in_days` from
	 * today. Skipped quietly when that day has no time left (a booking page made late in the evening).
	 */
	private static function book( int $id, int $in_days, string $at, array $query, array $body, string $name, string $phone ) {
		$tz   = wp_timezone();
		$day  = ( new \DateTimeImmutable( 'today', $tz ) )->modify( "+$in_days days" );
		$want = ( new \DateTimeImmutable( $day->format( 'Y-m-d' ) . " $at", $tz ) )->getTimestamp();
		wp_set_current_user( 0 );
		$avail = self::call( 'GET', "/modules/$id/availability", null, $query + array( 'date' => $day->format( 'Y-m-d' ) ) );
		foreach ( (array) ( $avail['slots'] ?? array() ) as $slot ) {
			if ( strtotime( $slot['start'] ) >= $want ) {
				self::call( 'POST', "/modules/$id/bookings", $body + array(
					'start'    => $slot['start'],
					'customer' => array( 'name' => $name, 'phone' => $phone ),
				) );
				break;
			}
		}
		wp_set_current_user( Reset::owner() );
	}

	// ------------------------------------------------------------------ the five

	private static function clinic(): ?int {
		$id = self::page( 'staff', array( 'phone' => '+60 3-2142 8810', 'accent' => '#4f6238' ) );
		if ( ! $id ) {
			return null;
		}
		self::hours( $id, '09:00', '18:00', array( 1, 2, 3, 4, 5, 6 ) );
		$gp = self::service( $id, array( 'name' => 'General consultation', 'duration_minutes' => 15, 'price' => '45.00' ) );
		self::service( $id, array( 'name' => 'Follow-up visit', 'duration_minutes' => 10, 'price' => '30.00' ) );
		self::service( $id, array( 'name' => 'Vaccination', 'duration_minutes' => 15, 'price' => '80.00' ) );
		$wong = self::person( $id, 'Dr Wong', array( '09:00', '18:00', '13:00', '14:00' ), array( 1, 2, 3, 4, 5, 6 ) );
		self::person( $id, 'Dr Aisyah', array( '09:00', '13:00' ), array( 1, 2, 3, 4, 5, 6 ) );
		self::book( $id, 1, '09:30', array( 'services' => $gp, 'staff' => $wong ), array( 'service_ids' => array( $gp ), 'staff_id' => $wong ), 'Tan Mei Hua', '+60 12-300 1101' );
		self::book( $id, 1, '10:30', array( 'services' => $gp ), array( 'service_ids' => array( $gp ) ), 'Rahim Osman', '+60 13-300 1102' );
		return $id;
	}

	private static function salon(): ?int {
		$id = self::page( 'salon', array( 'phone' => '+60 3-7781 2290', 'accent' => '#8a4458' ) );
		if ( ! $id ) {
			return null;
		}
		self::hours( $id, '10:00', '21:00', range( 0, 6 ) );
		$ask    = self::call( 'POST', "/admin/modules/$id/policies", array( 'name' => 'Colour: we confirm first', 'default_status' => 'pending', 'pending_timeout_minutes' => 180, 'min_notice_cancel_minutes' => 1440 ) );
		$cut    = self::service( $id, array( 'name' => 'Haircut', 'duration_minutes' => 45, 'price' => '45.00' ) );
		self::service( $id, array( 'name' => 'Wash and blow-dry', 'duration_minutes' => 30, 'price' => '30.00' ) );
		$colour = self::service( $id, array( 'name' => 'Colour', 'duration_minutes' => 90, 'price' => '180.00', 'policy_id' => $ask['id'] ?? null ) );
		$gel    = self::service( $id, array( 'name' => 'Gel finish', 'duration_minutes' => 15, 'price' => '15.00', 'is_addon' => true ) );
		self::service( $id, array( 'name' => 'Scalp treatment', 'duration_minutes' => 30, 'price' => '60.00', 'is_addon' => true ) );
		foreach ( array( 'Mei Ling' => array( '10:00', '19:00', '14:00', '14:45' ), 'Aishah' => array( '10:00', '19:00', '13:30', '14:15' ), 'Priya' => array( '12:00', '21:00', '16:00', '16:30' ), 'Jason' => array( '10:00', '18:00' ) ) as $name => $sh ) {
			self::person( $id, $name, $sh, range( 0, 6 ) );
		}
		self::book( $id, 1, '11:00', array( 'services' => "$cut,$gel" ), array( 'service_ids' => array( $cut, $gel ) ), 'Nurul Huda Ismail', '+60 17-300 1201' );
		self::book( $id, 1, '15:00', array( 'services' => (string) $colour ), array( 'service_ids' => array( $colour ) ), 'Farah Aziz', '+60 11-3000 1202' );
		self::book( $id, 2, '12:00', array( 'services' => (string) $cut ), array( 'service_ids' => array( $cut ) ), 'Daniel Tan', '+60 16-300 1203' );
		return $id;
	}

	private static function restaurant(): ?int {
		$id = self::page( 'restaurant', array( 'phone' => '+60 3-9284 1177', 'accent' => '#a63d2a' ) );
		if ( ! $id ) {
			return null;
		}
		self::hours( $id, '11:00', '22:30', range( 0, 6 ) );
		foreach ( array( array( 'A1', 2 ), array( 'A2', 2 ), array( 'A3', 2 ), array( 'B1', 4 ), array( 'B2', 4 ), array( 'B3', 4 ), array( 'C1', 6 ), array( 'C2', 6 ) ) as $t ) {
			self::call( 'POST', "/admin/modules/$id/tables", array( 'name' => $t[0], 'capacity' => $t[1] ) );
		}
		self::call( 'POST', "/admin/modules/$id/tables", array( 'name' => 'Teratai', 'capacity' => 10, 'is_vip' => true, 'minimum_spend' => '800.00' ) );
		self::call( 'PUT', "/admin/modules/$id/seating", array( 'booking_mode' => 'flexible', 'slot_duration_minutes' => 90, 'slot_interval_minutes' => 30, 'first_slot_time' => '11:30', 'last_slot_time' => '21:00', 'buffer_minutes' => 15 ) );
		self::book( $id, 1, '19:30', array( 'party_size' => 6, 'table_type' => 'normal' ), array( 'party_size' => 6, 'table_type' => 'normal' ), 'Lim Wei Jie', '+60 19-300 1301' );
		self::book( $id, 2, '20:00', array( 'party_size' => 4, 'table_type' => 'normal' ), array( 'party_size' => 4, 'table_type' => 'normal' ), 'Siti Rahmah', '+60 12-300 1302' );
		return $id;
	}

	private static function workshop(): ?int {
		$id = self::page( 'workshop', array( 'phone' => '+60 3-7956 4402', 'accent' => '#3c5a73' ) );
		if ( ! $id ) {
			return null;
		}
		self::hours( $id, '08:30', '18:00', array( 1, 2, 3, 4, 5, 6 ) );
		$oil   = self::service( $id, array( 'name' => 'Oil and filter change', 'duration_minutes' => 45, 'price' => '120.00' ) );
		$brake = self::service( $id, array( 'name' => 'Brake pads, front', 'duration_minutes' => 90, 'price' => '260.00' ) );
		self::service( $id, array( 'name' => 'Aircon gas top-up', 'duration_minutes' => 60, 'price' => '150.00' ) );
		self::person( $id, 'Ah Kow', array( '08:30', '18:00', '12:30', '13:30' ), array( 1, 2, 3, 4, 5, 6 ) );
		self::person( $id, 'Ravi', array( '08:30', '17:00' ), array( 1, 2, 3, 4, 5, 6 ) );
		self::call( 'POST', "/admin/modules/$id/bays", array( 'name' => 'Bay 1' ) );
		self::call( 'POST', "/admin/modules/$id/bays", array( 'name' => 'Bay 2' ) );
		$cars = array();
		foreach ( array( array( 'Perodua', 'Myvi', 2011, 2024 ), array( 'Perodua', 'Axia', 2014, 2024 ), array( 'Proton', 'Saga', 2016, 2024 ), array( 'Honda', 'City', 2014, 2024 ), array( 'Toyota', 'Vios', 2013, 2024 ) ) as $v ) {
			$r                 = self::call( 'POST', "/admin/modules/$id/supported-vehicles", array( 'make' => $v[0], 'model' => $v[1], 'year_from' => $v[2], 'year_to' => $v[3] ) );
			$cars[ $v[1] ]     = $r['id'] ?? null;
		}
		$pads = self::call( 'POST', "/admin/modules/$id/products", array( 'name' => 'Brake pads (front set)', 'stock_status' => 'available_on_order', 'estimated_lead_time_days' => 3, 'price' => '140.00', 'stock_qty' => 0 ) );
		if ( $pads && $brake ) {
			self::call( 'PUT', "/admin/services/$brake/parts", array( 'product_ids' => array( $pads['id'] ) ) );
		}
		self::book( $id, 1, '09:00', array( 'services' => (string) $oil ), array( 'service_ids' => array( $oil ), 'vehicle' => array( 'plate_no' => 'WXY 1234', 'supported_vehicle_id' => $cars['Myvi'], 'year' => 2019 ) ), 'Hafiz Rahman', '+60 17-300 1401' );
		return $id;
	}

	private static function field(): ?int {
		$id = self::page( 'field', array( 'phone' => '+60 12-388 4120', 'accent' => '#2c6e73', 'latitude' => 3.139, 'longitude' => 101.6869, 'service_radius_km' => 25 ) );
		if ( ! $id ) {
			return null;
		}
		self::hours( $id, '09:00', '18:00', array( 1, 2, 3, 4, 5, 6 ) );
		$check = self::service( $id, array( 'name' => 'Aircon check at your place', 'duration_minutes' => 45, 'price' => '60.00', 'location_type' => 'on_site', 'service_category' => 'inspection', 'travel_buffer_minutes' => 30 ) );
		self::service( $id, array( 'name' => 'Aircon servicing', 'duration_minutes' => 90, 'price' => '150.00', 'location_type' => 'on_site', 'travel_buffer_minutes' => 30 ) );
		self::service( $id, array( 'name' => 'Video call: is it worth fixing?', 'duration_minutes' => 20, 'price' => '0.00', 'location_type' => 'virtual' ) );
		self::person( $id, 'Faizal', array( '09:00', '18:00', '13:00', '14:00' ), array( 1, 2, 3, 4, 5, 6 ), array( 'meeting_link' => 'https://meet.example.test/faizal' ) );
		self::person( $id, 'Mei Yee', array( '10:00', '18:00' ), array( 1, 2, 3, 4, 5 ) );
		$pin = array( 'latitude' => 3.1478, 'longitude' => 101.6953 );
		self::book( $id, 1, '10:00', array( 'services' => (string) $check ) + $pin, array( 'service_ids' => array( $check ), 'address' => array( 'line1' => '12 Jalan Ampang', 'city' => 'Kuala Lumpur' ) + $pin ), 'Grace Wong', '+60 12-300 1501' );
		return $id;
	}
}
