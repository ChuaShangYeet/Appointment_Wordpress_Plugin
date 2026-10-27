<?php
/**
 * Installs the built plugin zip on a LOCAL test site as a separate copy, uses it, and puts everything back. Three runs, because
 * the copy and the site's own plugin cannot be loaded in one PHP process:
 *
 *   php marketing/check-zip.php <wordpress-root> prepare    unzip into plugins/justsimple-booking-zipcheck/, check it, switch the site's plugin off
 *   php marketing/check-zip.php <wordpress-root> use        activate the copy and use it: health checks, a public page, an admin route
 *   php marketing/check-zip.php <wordpress-root> restore    switch the copy off, remove only its folder, switch the site's plugin back on
 *
 * The site's own plugin folder (on the test site, a link to the source) is never written to.
 */

if ( PHP_SAPI !== 'cli' || empty( $argv[2] ) ) {
	fwrite( STDERR, "usage: php marketing/check-zip.php <wordpress-root> prepare|use|restore\n" );
	exit( 1 );
}
$site_root = rtrim( $argv[1], '/\\' ); // not $wp: that is WordPress's own global
$phase     = $argv[2];
require $site_root . '/wp-load.php';
require_once ABSPATH . 'wp-admin/includes/plugin.php';

const SITE = 'justsimple-booking/justsimple-booking.php';
const COPY = 'justsimple-booking-zipcheck/justsimple-booking.php';
$dir = WP_PLUGIN_DIR . '/justsimple-booking-zipcheck';
$ok  = true;
$say = static function ( bool $pass, string $what ) use ( &$ok ) {
	echo ( $pass ? '  ok   ' : '  FAIL ' ), $what, "\n";
	$ok = $ok && $pass;
};

if ( 'prepare' === $phase ) {
	$zips = glob( __DIR__ . '/dist/justsimple-booking-*.zip' );
	$say( 1 === count( $zips ), 'one zip in marketing/dist: ' . basename( $zips[0] ?? '' ) );
	$z = new ZipArchive();
	$z->open( $zips[0] );
	$names = array();
	for ( $i = 0; $i < $z->numFiles; $i++ ) {
		$names[] = $z->getNameIndex( $i );
	}
	$say( ! array_filter( $names, static function ( $n ) {
		return 0 !== strpos( $n, 'justsimple-booking/' );
	} ), 'everything sits in one folder, justsimple-booking/' );
	$say( ! preg_grep( '#/tests/|/\.#', $names ), 'no tests and no hidden files' );
	$say( in_array( 'justsimple-booking/justsimple-booking.php', $names, true ) && in_array( 'justsimple-booking/uninstall.php', $names, true ), 'the main file and uninstall.php are there' );
	if ( is_dir( $dir ) ) {
		fwrite( STDERR, "$dir already exists: run restore first\n" );
		exit( 1 );
	}
	$tmp = sys_get_temp_dir() . '/jsb-zipcheck-' . getmypid();
	$z->extractTo( $tmp );
	$z->close();
	rename( "$tmp/justsimple-booking", $dir );
	@rmdir( $tmp );
	$lint = 0;
	foreach ( new RecursiveIteratorIterator( new RecursiveDirectoryIterator( $dir, FilesystemIterator::SKIP_DOTS ) ) as $f ) {
		if ( 'php' === $f->getExtension() ) {
			exec( escapeshellarg( PHP_BINARY ) . ' -l ' . escapeshellarg( $f->getPathname() ) . ' 2>&1', $out, $code );
			$lint += $code ? 1 : 0;
		}
	}
	$say( 0 === $lint, 'every PHP file in the zip parses' );
	deactivate_plugins( SITE, true );
	$say( ! is_plugin_active( SITE ), "the site's own plugin is switched off for the check" );
} elseif ( 'use' === $phase ) {
	$r = activate_plugin( COPY );
	$say( ! is_wp_error( $r ), 'the zip copy activates' . ( is_wp_error( $r ) ? ': ' . $r->get_error_message() : '' ) );
	// load it as WordPress would on the next request
	include_once WP_PLUGIN_DIR . '/' . COPY;
	do_action( 'plugins_loaded' );
	do_action( 'init' );
	$say( defined( 'JSB_DIR' ) && false !== strpos( str_replace( '\\', '/', JSB_DIR ), 'justsimple-booking-zipcheck' ), 'the running code is the zip copy' );
	$fails = array_filter( JustSimple\Booking\Data\Health::checks(), static function ( $c ) {
		return false === $c['ok'];
	} );
	$say( ! $fails, 'its System status shows no problem' . ( $fails ? ': ' . implode( ', ', array_column( $fails, 'label' ) ) : '' ) );
	do_action( 'rest_api_init' );
	global $wpdb;
	$id  = (int) $wpdb->get_var( 'SELECT MAX(id) FROM ' . $wpdb->prefix . "jsb_modules WHERE title = 'Mei Hair Studio'" );
	$res = rest_do_request( new WP_REST_Request( 'GET', "/justsimple/v1/modules/$id" ) );
	$say( 200 === $res->get_status() && 'salon' === $res->get_data()['type'], 'a booking page answers from the zip copy' );
	wp_set_current_user( 1 );
	$res = rest_do_request( new WP_REST_Request( 'GET', "/justsimple/v1/admin/modules/$id/day" ) );
	$say( 200 === $res->get_status() && isset( $res->get_data()['resources'] ), 'and the admin day board too' );
	$say( false !== strpos( JustSimple\Booking\Front\Front::render( $id ), '<jsb-booking' ), 'its shortcode prints the widget' );
} elseif ( 'restore' === $phase ) {
	deactivate_plugins( COPY, true );
	if ( is_dir( $dir ) && 'justsimple-booking-zipcheck' === basename( $dir ) && ! is_link( $dir ) ) {
		foreach ( new RecursiveIteratorIterator( new RecursiveDirectoryIterator( $dir, FilesystemIterator::SKIP_DOTS ), RecursiveIteratorIterator::CHILD_FIRST ) as $f ) {
			$f->isDir() ? rmdir( $f->getPathname() ) : unlink( $f->getPathname() );
		}
		rmdir( $dir );
	}
	$say( ! is_dir( $dir ), 'the zip copy is removed' );
	$r = activate_plugin( SITE );
	$say( ! is_wp_error( $r ) && is_plugin_active( SITE ), "the site's own plugin is back on" );
	$say( is_file( WP_PLUGIN_DIR . '/' . SITE ), "the site's own plugin files are untouched" );
}
exit( $ok ? 0 : 1 );
