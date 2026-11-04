<?php
namespace JustSimple\Showcase;

defined( 'ABSPATH' ) || exit;

/**
 * The plugin's zip is handed out by email: a visitor gives an address, gets a private link that works for 7 days, and the link
 * serves the zip from private/ (which the web server refuses to serve itself). Only a hash of the link's token is stored. An
 * address whose link was never opened is removed after 30 days; one that was opened stays until the owner deletes it. Resets
 * leave this list alone, and it is not tied to the visit log, so visits stay anonymous.
 */
final class Downloads {

	const DB      = 'jss_downloads_db'; // the table's version
	const ROUTE   = 'justsimple-showcase/v1';
	const PARAM   = 'jss_download';
	const DAYS    = 7; // a link works this long
	const FORGET  = 30; // days an address with an unopened link is kept
	const SENDS   = 3; // emails to one address in a day
	const ASKS    = 10; // requests from one person in an hour

	public static function table(): string {
		global $wpdb;
		return $wpdb->prefix . 'jss_downloads';
	}

	public static function register() {
		if ( 1 !== (int) get_option( self::DB ) ) {
			self::install(); // a new Showcase uploaded over the old one does not run the activation hook
		}
		add_action( 'rest_api_init', array( __CLASS__, 'routes' ) );
		add_action( 'template_redirect', array( __CLASS__, 'serve' ), -1 );
		add_action( Reset::HOOK, array( __CLASS__, 'prune' ) );
	}

	public static function install() {
		global $wpdb;
		require_once ABSPATH . 'wp-admin/includes/upgrade.php';
		$t = self::table();
		dbDelta( "CREATE TABLE $t (
			id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
			email varchar(190) NOT NULL,
			token char(64) NOT NULL,
			asked datetime NOT NULL,
			expires datetime NOT NULL,
			opened datetime DEFAULT NULL,
			times int(10) unsigned NOT NULL DEFAULT 0,
			PRIMARY KEY  (id),
			UNIQUE KEY email (email),
			KEY token (token)
		) ENGINE=InnoDB " . $wpdb->get_charset_collate() . ';' );
		update_option( self::DB, 1, false );
	}

	public static function endpoint(): string {
		return rest_url( self::ROUTE . '/download' );
	}

	/** The zip the build put in private/, or '' when there is none. */
	public static function file(): string {
		$zips = glob( JSS_DIR . 'private/justsimple-booking-*.zip' );
		return $zips ? (string) end( $zips ) : '';
	}

	public static function routes() {
		register_rest_route( self::ROUTE, '/download', array(
			'methods'             => 'POST',
			'permission_callback' => '__return_true', // anyone on the page; the limits are in ask()
			'callback'            => array( __CLASS__, 'ask' ),
		) );
	}

	/** { email, website } ('website' is a field people never see: only a robot fills it in, and it is told all went well). */
	public static function ask( \WP_REST_Request $request ) {
		$body  = json_decode( (string) $request->get_body(), true );
		$email = is_array( $body ) ? strtolower( trim( (string) ( $body['email'] ?? '' ) ) ) : '';
		if ( ! empty( $body['website'] ) ) {
			return array( 'sent' => true );
		}
		if ( strlen( $email ) > 190 || ! is_email( $email ) ) {
			return new \WP_Error( 'jss_email', __( 'That does not look like an email address.', 'justsimple-showcase' ), array( 'status' => 400 ) );
		}
		$agent = substr( (string) ( $_SERVER['HTTP_USER_AGENT'] ?? '' ), 0, 400 ); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput -- only hashed
		$asks  = 'jss_da_' . Visits::person( $agent );
		$sends = 'jss_ds_' . md5( $email );
		if ( (int) get_transient( $asks ) >= self::ASKS || (int) get_transient( $sends ) >= self::SENDS ) {
			return new \WP_Error( 'jss_limit', __( 'We have already sent that a few times. Check your inbox and spam folder, or try again tomorrow.', 'justsimple-showcase' ), array( 'status' => 429 ) );
		}
		set_transient( $asks, (int) get_transient( $asks ) + 1, HOUR_IN_SECONDS );

		$token = wp_generate_password( 40, false );
		if ( ! self::send( $email, $token ) ) {
			return new \WP_Error( 'jss_mail', __( 'We could not send the email just now. Please try again later.', 'justsimple-showcase' ), array( 'status' => 503 ) );
		}
		set_transient( $sends, (int) get_transient( $sends ) + 1, DAY_IN_SECONDS );

		global $wpdb;
		$t = self::table();
		$wpdb->query( $wpdb->prepare( // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
			"INSERT INTO $t (email, token, asked, expires) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE token = VALUES(token), asked = VALUES(asked), expires = VALUES(expires)",
			$email,
			hash( 'sha256', $token ),
			current_time( 'mysql', true ),
			gmdate( 'Y-m-d H:i:s', time() + self::DAYS * DAY_IN_SECONDS )
		) );
		return array( 'sent' => true );
	}

	private static function send( string $email, string $token ): bool {
		$link = add_query_arg( self::PARAM, $token, home_url( '/' ) );
		/* translators: 1: the private link, 2: days it works */
		$text = sprintf( __( "Here is your private link to JustSimple Booking:\n\n%1\$s\n\nIt works for %2\$d days. Open it in a browser to download the plugin, then in your WordPress go to Plugins > Add New Plugin > Upload Plugin.\n\nDid not ask for this? Ignore this email. To have your address deleted, reply to it.", 'justsimple-showcase' ), $link, self::DAYS );
		return self::real_mail( $email, __( 'Your JustSimple Booking download', 'justsimple-showcase' ), $text );
	}

	/** A test from the admin page, through the same door as the download emails. */
	public static function test( string $email ): bool {
		return self::real_mail( $email, __( 'JustSimple Showcase test email', 'justsimple-showcase' ), __( 'If you can read this, the demo site can send download links.', 'justsimple-showcase' ) );
	}

	/** The one kind of mail this site really sends: everything else stays caught. */
	private static function real_mail( string $to, string $subject, string $text ): bool {
		MailCatcher::$pass = true;
		try {
			return (bool) wp_mail( $to, $subject, $text );
		} finally {
			MailCatcher::$pass = false;
		}
	}

	/** Opens a private link: the zip, or a page saying why not. */
	public static function serve() {
		$token = isset( $_GET[ self::PARAM ] ) ? (string) $_GET[ self::PARAM ] : ''; // phpcs:ignore WordPress.Security.NonceVerification.Recommended, WordPress.Security.ValidatedSanitizedInput -- the token is the permission; only hashed
		if ( '' === $token ) {
			return;
		}
		if ( ! defined( 'DONOTCACHEPAGE' ) ) {
			define( 'DONOTCACHEPAGE', true );
		}
		global $wpdb;
		$t    = self::table();
		$row  = $wpdb->get_row( $wpdb->prepare( "SELECT id, expires FROM $t WHERE token = %s", hash( 'sha256', $token ) ), ARRAY_A ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		$back = '<p><a href="' . esc_url( home_url( '/' ) ) . '">' . esc_html__( 'Ask for a new link', 'justsimple-showcase' ) . '</a></p>';
		$file = self::file();
		if ( ! $row ) {
			wp_die( '<p>' . esc_html__( 'This link is not valid: a newer link may have replaced it.', 'justsimple-showcase' ) . '</p>' . $back, '', array( 'response' => 404 ) ); // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- built above
		}
		if ( strtotime( $row['expires'] . ' UTC' ) < time() ) {
			wp_die( '<p>' . esc_html__( 'This link has expired.', 'justsimple-showcase' ) . '</p>' . $back, '', array( 'response' => 410 ) ); // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- built above
		}
		if ( '' === $file ) {
			wp_die( esc_html__( 'The download is not available just now.', 'justsimple-showcase' ), '', array( 'response' => 503 ) );
		}
		$wpdb->query( $wpdb->prepare( "UPDATE $t SET opened = COALESCE(opened, %s), times = times + 1 WHERE id = %d", current_time( 'mysql', true ), $row['id'] ) ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		while ( ob_get_level() ) {
			ob_end_clean();
		}
		nocache_headers();
		header( 'Content-Type: application/zip' );
		header( 'Content-Disposition: attachment; filename="' . basename( $file ) . '"' );
		header( 'Content-Length: ' . filesize( $file ) );
		header( 'X-Robots-Tag: noindex' );
		readfile( $file ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_readfile
		exit;
	}

	public static function prune() {
		global $wpdb;
		$t = self::table();
		$wpdb->query( $wpdb->prepare( "DELETE FROM $t WHERE opened IS NULL AND asked < %s", gmdate( 'Y-m-d H:i:s', time() - self::FORGET * DAY_IN_SECONDS ) ) ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
	}

	public static function all(): array {
		global $wpdb;
		$t = self::table();
		return (array) $wpdb->get_results( "SELECT id, email, asked, opened, times FROM $t ORDER BY asked DESC, id DESC", ARRAY_A ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
	}

	public static function remove( int $id ) {
		global $wpdb;
		$wpdb->delete( self::table(), array( 'id' => $id ) );
	}
}
