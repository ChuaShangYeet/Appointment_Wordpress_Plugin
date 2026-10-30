<?php
/**
 * The mobile-number check against the backend's. Plain PHP, no WordPress: `php tests/phone.php`.
 *
 * tests/phone-cases.json holds every kind of example number of every country (Google's), typed the ways people type them
 * and a digit short or long, with the answer the backend's check gives (tools/make-phone-plans.py runs it through Python's
 * phonenumbers). Every one must get the same answer here. The named cases after it are the backend's tests/test_mobile_numbers.py.
 */

define( 'ABSPATH', __DIR__ . '/' );
spl_autoload_register(
	static function ( $class ) {
		$prefix = 'JustSimple\\Booking\\';
		if ( 0 === strncmp( $class, $prefix, strlen( $prefix ) ) ) {
			$file = dirname( __DIR__ ) . '/src/' . str_replace( '\\', '/', substr( $class, strlen( $prefix ) ) ) . '.php';
			if ( is_readable( $file ) ) {
				require $file;
			}
		}
	}
);

use JustSimple\Booking\Booking\Phone;
use JustSimple\Booking\Engine\EngineError;

$fail = 0;
function ok( bool $pass, string $what, $detail = null ) {
	global $fail;
	if ( ! $pass ) {
		$fail++;
		echo "  FAIL $what", null === $detail ? '' : "\n       " . json_encode( $detail, JSON_UNESCAPED_UNICODE ), "\n";
	}
}
/** The check's answer as the cases write it: +<digits>, L a landline, G no number at all, N <country> not a mobile there. */
function answer( string $raw, string $region ): string {
	try {
		return Phone::mobile( $raw, $region );
	} catch ( EngineError $e ) {
		$m = $e->getMessage();
		if ( Phone::LANDLINE === $m ) {
			return 'L';
		}
		if ( Phone::INVALID === $m ) {
			return 'G';
		}
		if ( preg_match( '/^That is not a mobile number in (.+): /', $m, $x ) ) {
			foreach ( json_decode( file_get_contents( dirname( __DIR__ ) . '/assets/front/phone-countries.json' ), true ) as $c ) {
				if ( $c['name'] === $x[1] && $m === Phone::not_mobile_message( $c['code'] ) ) {
					return 'N ' . $c['code'];
				}
			}
		}
		return '? ' . $m;
	}
}

echo "== every country's examples, as the backend answers\n";
$data  = json_decode( file_get_contents( __DIR__ . '/phone-cases.json' ), true );
$wrong = array();
foreach ( $data['cases'] as list( $raw, $region, $want ) ) {
	$got = answer( $raw, $region );
	if ( $got !== $want ) {
		$wrong[] = array( $raw, $region, $want, $got );
	}
}
ok( ! $wrong, count( $wrong ) . ' of ' . count( $data['cases'] ) . ' numbers answered differently from libphonenumber ' . $data['library'], array_slice( $wrong, 0, 15 ) );
echo '  ', count( $data['cases'] ) - count( $wrong ), ' of ', count( $data['cases'] ), " the same\n";

echo "== the backend's named cases\n";
foreach ( array( '012-345 6789' => '+60123456789', '011-2345 6789' => '+601123456789', '0123456789' => '+60123456789', '+60 12-345 6789' => '+60123456789', '0060 12 345 6789' => '+60123456789', '12 345 6789' => '+60123456789', '  (012) 345.6789 ' => '+60123456789' ) as $raw => $want ) {
	ok( $want === answer( $raw, 'MY' ), "a Malaysian mobile of 9 or 10 digits is one number however it is typed: $raw" );
}
foreach ( array( '012-345 678', '12345678', '011-2345 67890', '+60 12 345 67890' ) as $raw ) {
	try {
		Phone::mobile( $raw, 'MY' );
		ok( false, "refused: $raw" );
	} catch ( EngineError $e ) {
		ok( 'That is not a mobile number in Malaysia: it has 9 or 10 digits after +60, such as 012-345 6789' === $e->getMessage() && 'customer.phone' === $e->data['field'], "a Malaysian mobile of another length says what it should look like: $raw", $e->getMessage() );
	}
}
foreach ( array( array( '03-2282 4410', 'MY' ), array( '6123 4567', 'SG' ), array( '02 9374 4000', 'AU' ) ) as list( $raw, $region ) ) {
	ok( 'L' === answer( $raw, $region ), "a landline is refused: $raw ($region)" );
}
foreach ( array( array( '9123 4567', 'SG', '+6591234567' ), array( '8123 4567', 'SG', '+6581234567' ), array( '0412 345 678', 'AU', '+61412345678' ), array( '(201) 555-0123', 'US', '+12015550123' ), array( '07400 123456', 'GB', '+447400123456' ) ) as list( $raw, $region, $want ) ) {
	ok( $want === answer( $raw, $region ), "other countries read numbers their own way: $raw ($region)", answer( $raw, $region ) );
}
ok( '+6591234567' === answer( '+65 9123 4567', 'MY' ) && '+6591234567' === answer( '0065 9123 4567', 'MY' ), 'a number typed with its country code keeps that country' );
ok( 'N GB' === answer( '+44 7400 12', 'MY' ), 'and the message is about that country', answer( '+44 7400 12', 'MY' ) );
ok( '+60123456789' === answer( '0060 12 345 6789', 'US' ), '00 in front means international, even where people dial 011 abroad' );
ok( 'N MY' === answer( '+800 12', 'MY' ), 'a worldwide code that is no country gets the chosen country\'s message' );
ok( 'G' === answer( '12ab345', 'MY' ) && 'G' === answer( '', 'MY' ), 'letters and nothing are no number at all' );
ok( '+60123456789' === Phone::spelled( '012-345 6789', 'MY' ) && '+60312345678' === Phone::spelled( '03-1234 5678', 'MY' ) && null === Phone::spelled( 'abc', 'MY' ), 'spelled(): a landline spelled the standard way, to find a customer on file' );
ok( Phone::known( 'MY' ) && ! Phone::known( 'my' ) && ! Phone::known( 'ZZ' ), 'known countries are two capital letters' );

echo "\n" . ( $fail ? "$fail FAILED" : 'all passed' ) . "\n";
exit( $fail ? 1 : 0 );
