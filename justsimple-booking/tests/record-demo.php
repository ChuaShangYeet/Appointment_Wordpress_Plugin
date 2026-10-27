<?php
/**
 * Records the real engine's answers for the marketing page's demo widget.
 *
 *   php tests/record-demo.php C:/xampp/htdocs/jsb-test marketing/demo
 *
 * For each demo booking page made by tests/seed-demo.php, it asks the public REST routes everything the widget can ask in the
 * demo (every service combination, every party size) for each day of the seeded reference week, and writes one JSON file per
 * kind. The page's DemoSource replays them, moving each answer to the weekday the visitor looks at. Nothing is invented: every
 * free time in the demo is one the engine offered.
 */

if ( PHP_SAPI !== 'cli' || empty( $argv[2] ) ) {
	fwrite( STDERR, "usage: php tests/record-demo.php <wordpress-root> <output-dir>\n" );
	exit( 1 );
}
require rtrim( $argv[1], '/\\' ) . '/wp-load.php';

use JustSimple\Booking\Data\Schema;

add_filter( 'jsb_rate_limit', '__return_zero' );
wp_set_current_user( 0 ); // a visitor, as on the page

$out = rtrim( $argv[2], '/\\' );
if ( ! is_dir( $out ) ) {
	mkdir( $out, 0777, true );
}
$ref = get_option( 'jsb_demo_ref_monday' );
if ( ! $ref ) {
	fwrite( STDERR, "run tests/seed-demo.php first\n" );
	exit( 1 );
}
$days = array_map( static function ( $n ) use ( $ref ) {
	return gmdate( 'Y-m-d', strtotime( "$ref +$n days" ) );
}, range( 0, 6 ) );

function get( string $path, array $query = array() ) {
	$req = new WP_REST_Request( 'GET', '/justsimple/v1' . $path );
	$req->set_query_params( $query );
	$res = rest_do_request( $req );
	return array( $res->get_status(), json_decode( wp_json_encode( $res->get_data() ), true ) );
}

/** The same key the page's DemoSource builds from a widget query: sorted, without the date, the pin or a chosen person. */
function key_of( array $q ): string {
	$q = array_filter( $q, static function ( $v, $k ) {
		return '' !== (string) $v && null !== $v && ! in_array( $k, array( 'date', 'month', 'latitude', 'longitude', 'staff', 'table' ), true );
	}, ARRAY_FILTER_USE_BOTH );
	ksort( $q );
	return http_build_query( $q );
}

/** Only what the widget reads from a slot. */
function slim( array $slot ): array {
	return array_intersect_key( $slot, array_flip( array( 'start', 'end', 'staff_ids', 'bay_ids', 'table_ids', 'combined', 'minimum_spend' ) ) );
}

/** Every subset of `$ids`, in their order (the widget sends ticked boxes in the order they are listed). */
function subsets( array $ids ): array {
	$out = array();
	for ( $mask = 1; $mask < ( 1 << count( $ids ) ); $mask++ ) {
		$out[] = array_values( array_filter( $ids, static function ( $i ) use ( $mask, $ids ) {
			return $mask & ( 1 << array_search( $i, $ids, true ) );
		} ) );
	}
	return $out;
}

global $wpdb;
$pages = array(
	'staff'      => 'Klinik Dr Wong',
	'salon'      => 'Mei Hair Studio',
	'restaurant' => 'Rumah Makan Seri',
	'workshop'   => 'Bengkel Ah Kow',
	'field'      => 'CoolAir Services',
);
$pin = array( 'latitude' => 3.1478, 'longitude' => 101.6953 ); // a street in Kuala Lumpur, inside the demo's 25 km

foreach ( $pages as $kind => $title ) {
	$id = (int) $wpdb->get_var( $wpdb->prepare( 'SELECT MAX(id) FROM ' . Schema::table( 'modules' ) . " WHERE title = %s AND status = 'active'", $title ) );
	list( $s, $module ) = get( "/modules/$id" );
	if ( 200 !== $s ) {
		fwrite( STDERR, "$title: not found; run tests/seed-demo.php\n" );
		exit( 1 );
	}
	$base   = array_values( array_filter( $module['services'], static function ( $x ) {
		return ! $x['is_addon'];
	} ) );
	$addons = array_values( array_filter( $module['services'], static function ( $x ) {
		return $x['is_addon'];
	} ) );
	$queries = array();
	switch ( $kind ) {
		case 'restaurant':
			foreach ( array( 'normal', 'vip' ) as $type ) {
				$info = $module['restaurant'][ $type ];
				// up to 12 people: the demo's parties (the page's stepper stops there; a bigger group would be its own conversation)
				for ( $p = 1; $p <= min( 12, max( $info['largest_table'], $info['largest_separate_party'] ) ); $p++ ) {
					$queries[] = array( 'party_size' => $p, 'table_type' => $type );
					if ( $p > $info['largest_table'] ) {
						$queries[] = array( 'party_size' => $p, 'table_type' => $type, 'separate' => 1 );
					}
				}
			}
			break;
		case 'salon':
			foreach ( $base as $b ) {
				$queries[] = array( 'services' => (string) $b['id'] );
				foreach ( subsets( array_column( $addons, 'id' ) ) as $set ) {
					$queries[] = array( 'services' => implode( ',', array_merge( array( $b['id'] ), $set ) ) );
				}
			}
			break;
		case 'workshop':
			foreach ( subsets( array_column( $base, 'id' ) ) as $set ) {
				$queries[] = array( 'services' => implode( ',', $set ) );
			}
			break;
		default:
			foreach ( $base as $b ) {
				$queries[] = array( 'services' => (string) $b['id'] ) + ( 'on_site' === $b['location_type'] ? $pin : array() );
			}
	}

	$answers = array();
	foreach ( $queries as $q ) {
		$k = key_of( $q );
		foreach ( $days as $d ) {
			list( $s, $a ) = get( "/modules/$id/availability", $q + array( 'date' => $d ) );
			if ( 200 !== $s ) {
				fwrite( STDERR, "$title $k $d: $s " . wp_json_encode( $a ) . "\n" );
				continue;
			}
			$entry = array( 'slots' => array_map( 'slim', $a['slots'] ) );
			if ( ! empty( $a['lead_time_notice'] ) ) {
				$entry['lead_time_notice'] = $a['lead_time_notice'];
			}
			$answers[ $k ][ (int) gmdate( 'w', strtotime( $d ) ) ] = $entry;
		}
	}
	list( , $vehicles ) = 'workshop' === $kind ? get( "/modules/$id/supported-vehicles" ) : array( 200, array() );

	$module['id']                      = 1; // the demo page has no real id
	$module['look']['phone']           = '';
	$data                              = array(
		'kind'     => $kind,
		'recorded' => gmdate( 'c' ),
		'ref'      => $days,
		'module'   => $module,
		'vehicles' => $vehicles,
		'answers'  => $answers,
	);
	file_put_contents( "$out/$kind.json", wp_json_encode( $data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE ) );
	$slots = 0;
	foreach ( $answers as $byday ) {
		foreach ( $byday as $e ) {
			$slots += count( $e['slots'] );
		}
	}
	printf( "%-10s %-18s %3d questions x 7 days, %5d free times, %6.1f KB\n", $kind, $title, count( $queries ), $slots, filesize( "$out/$kind.json" ) / 1024 );
}
