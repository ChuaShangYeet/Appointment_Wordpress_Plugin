<?php
namespace JustSimple\Showcase;

defined( 'ABSPATH' ) || exit;

/**
 * A public demo must never email the addresses visitors type in. While the Showcase is active, every wp_mail() is caught: the
 * last 50 are kept (recipient, subject, time) for the admin page and cleared at each reset, and nothing is sent. The one exception
 * is the download link a visitor asks for by email (Downloads), which sets $pass for that single send.
 */
final class MailCatcher {

	const OPTION = 'jss_mail';
	const KEEP   = 50;

	public static $pass = false;

	public static function register() {
		add_filter( 'pre_wp_mail', array( __CLASS__, 'catch_mail' ), 1, 2 );
	}

	public static function catch_mail( $short, $atts ) {
		if ( self::$pass ) {
			return $short;
		}
		$kept = (array) get_option( self::OPTION, array() );
		array_unshift( $kept, array(
			'at'      => time(),
			'to'      => is_array( $atts['to'] ) ? implode( ', ', $atts['to'] ) : (string) $atts['to'],
			'subject' => (string) $atts['subject'],
		) );
		update_option( self::OPTION, array_slice( $kept, 0, self::KEEP ), false );
		return true; // reported as sent, so the booking flow carries on as it would on a real site
	}

	public static function all(): array {
		return (array) get_option( self::OPTION, array() );
	}

	public static function clear() {
		delete_option( self::OPTION );
	}
}
