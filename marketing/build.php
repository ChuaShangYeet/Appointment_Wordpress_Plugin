<?php
/**
 * Builds the marketing page into marketing/dist/, ready for any static host:
 *
 *   php marketing/build.php
 *
 * - the page (index.html, page.css, page.js), its photographs and credits, and the recorded demo answers
 * - the booking widget, its demo source and the DM Sans files, copied from the plugin so the page always shows the real widget
 * - justsimple-booking-<version>.zip, the plugin as WordPress installs it (no tests/), for the Download button
 *
 *   php marketing/build.php --artifact
 *
 * builds marketing/dist-artifact/ instead: the same page for a private Claude Artifact review link, whose host adds the document
 * skeleton itself and blocks downloads, so the page's own <html>/<head>/<body> tags go and the download links point at the
 * closing section. No zip.
 */

if ( PHP_SAPI !== 'cli' ) {
	exit( 1 );
}
$artifact = in_array( '--artifact', $argv, true );
$here     = __DIR__;
$plugin   = dirname( __DIR__ ) . '/justsimple-booking';
$dist     = $here . ( $artifact ? '/dist-artifact' : '/dist' );

if ( ! preg_match( '/^\s*\*\s*Version:\s*(\S+)/m', file_get_contents( "$plugin/justsimple-booking.php" ), $v ) ) {
	fwrite( STDERR, "no Version: header in justsimple-booking.php\n" );
	exit( 1 );
}
$version = $v[1];

/** Empties a folder this script made (only under marketing/dist). */
function clear( string $dir, string $dist ) {
	if ( ! is_dir( $dir ) || 0 !== strpos( realpath( $dir ), realpath( $dist ) ) ) {
		return;
	}
	foreach ( new RecursiveIteratorIterator( new RecursiveDirectoryIterator( $dir, FilesystemIterator::SKIP_DOTS ), RecursiveIteratorIterator::CHILD_FIRST ) as $f ) {
		$f->isDir() ? rmdir( $f->getPathname() ) : unlink( $f->getPathname() );
	}
}
function put( string $from, string $to ) {
	if ( ! is_dir( dirname( $to ) ) ) {
		mkdir( dirname( $to ), 0777, true );
	}
	if ( ! copy( $from, $to ) ) {
		fwrite( STDERR, "could not copy $from\n" );
		exit( 1 );
	}
}

if ( ! is_dir( $dist ) ) {
	mkdir( $dist, 0777, true );
}
clear( $dist, $dist );

// the page, with the version and the zip's name filled in
foreach ( array( 'index.html', 'page.css', 'page.js' ) as $f ) {
	$text = str_replace( array( '{{version}}', '{{zip}}' ), array( $version, $artifact ? '#get' : "justsimple-booking-$version.zip" ), file_get_contents( "$here/$f" ) );
	if ( $artifact && 'index.html' === $f ) {
		// the host wraps the page in its own document: keep the head's contents and the body's, with a plain name as the title
		preg_match( '#<head>(.*?)</head>#s', $text, $head );
		preg_match( '#<body>(.*?)</body>#s', $text, $body );
		$text = preg_replace( '#<title>.*?</title>#s', '<title>JustSimple Booking</title>', $head[1] ) . $body[1];
		$text = str_replace( ' download>', '>', $text );
	}
	file_put_contents( "$dist/$f", $text );
}
foreach ( glob( "$here/img/*" ) as $f ) {
	put( $f, "$dist/img/" . basename( $f ) );
}
foreach ( glob( "$here/demo/*.json" ) as $f ) {
	put( $f, "$dist/demo/" . basename( $f ) );
}
foreach ( array( 'jsb-booking.js', 'jsb-booking.css', 'demo-source.js', 'map-picker.js' ) as $f ) {
	put( "$plugin/assets/front/$f", "$dist/widget/$f" );
}
foreach ( glob( "$plugin/assets/fonts/*" ) as $f ) {
	put( $f, "$dist/fonts/" . basename( $f ) );
}

if ( $artifact ) {
	echo "marketing/dist-artifact built for review: plugin $version\n";
	exit( 0 );
}

// the plugin zip: one top folder, everything WordPress needs, nothing for development
$zipPath = "$dist/justsimple-booking-$version.zip";
$zip     = new ZipArchive();
if ( true !== $zip->open( $zipPath, ZipArchive::CREATE | ZipArchive::OVERWRITE ) ) {
	fwrite( STDERR, "cannot write $zipPath\n" );
	exit( 1 );
}
$files = 0;
$base  = realpath( $plugin );
foreach ( new RecursiveIteratorIterator( new RecursiveDirectoryIterator( $base, FilesystemIterator::SKIP_DOTS ) ) as $f ) {
	$rel = str_replace( '\\', '/', substr( $f->getPathname(), strlen( $base ) + 1 ) );
	if ( preg_match( '#^(tests/|\.)|/\.|\.(log|bak)$#', $rel ) ) {
		continue;
	}
	$zip->addFile( $f->getPathname(), "justsimple-booking/$rel" );
	$files++;
}
$zip->close();

printf( "marketing/dist built: plugin %s, %d files in the zip (%.0f KB)\n", $version, $files, filesize( $zipPath ) / 1024 );
