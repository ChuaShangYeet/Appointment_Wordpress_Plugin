<?php
namespace JustSimple\Showcase;

defined( 'ABSPATH' ) || exit;

/**
 * A log of visits to the marketing page, without cookies. The page sends what a visit does (the sections it reached, the
 * businesses it tried, a booking, a download) to POST /justsimple-showcase/v1/visit under an id that lives only in the open
 * tab. Nothing is stored in the browser and no IP address is kept: to count people once a day, the IP and browser are mixed
 * with a key that changes every day and only the first 16 characters of the result are kept, which cannot be turned back.
 * Rows older than 90 days are removed each night. Site admins and obvious bots are not logged.
 */
final class Visits {

	const DB      = 'jss_visits_db'; // the table's version
	const SALT    = 'jss_visits_salt'; // { day, key }
	const KEEP    = 90; // days
	const ROUTE   = 'justsimple-showcase/v1';
	const STEPS   = 200; // per visit, at most
	const EVENTS  = array( 'open', 'section', 'room', 'business', 'booked', 'refused', 'download', 'hosted', 'leave', 'back' );
	const KINDS   = array( 'salon', 'restaurant', 'workshop', 'field', 'staff' );
	const PLACES  = array( 'try', 'kinds', 'how', 'questions', 'close', 'foot' ); // the page's sections, top to bottom
	const BOTS    = '/bot|crawl|spider|slurp|fetch|curl|wget|python|headless|lighthouse|preview|monitor|scan/i';

	public static function table(): string {
		global $wpdb;
		return $wpdb->prefix . 'jss_visits';
	}

	public static function register() {
		if ( 1 !== (int) get_option( self::DB ) ) {
			self::install(); // a new Showcase uploaded over the old one does not run the activation hook
		}
		add_action( 'rest_api_init', array( __CLASS__, 'routes' ) );
		add_action( Reset::HOOK, array( __CLASS__, 'prune' ) );
	}

	public static function install() {
		global $wpdb;
		require_once ABSPATH . 'wp-admin/includes/upgrade.php';
		$t = self::table();
		dbDelta( "CREATE TABLE $t (
			id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
			visit varchar(32) NOT NULL,
			person char(16) NOT NULL,
			started datetime NOT NULL,
			seen datetime NOT NULL,
			came_from varchar(100) NOT NULL DEFAULT '',
			device varchar(60) NOT NULL DEFAULT '',
			reached varchar(16) NOT NULL DEFAULT '',
			booked tinyint(1) NOT NULL DEFAULT 0,
			downloaded tinyint(1) NOT NULL DEFAULT 0,
			steps text NOT NULL,
			PRIMARY KEY  (id),
			UNIQUE KEY visit (visit),
			KEY started (started)
		) ENGINE=InnoDB " . $wpdb->get_charset_collate() . ';' );
		update_option( self::DB, 1, false );
	}

	/** Where the page sends its steps; empty when this request should not be logged (a site admin looking at their own page). */
	public static function endpoint(): string {
		return current_user_can( 'manage_options' ) ? '' : rest_url( self::ROUTE . '/visit' );
	}

	public static function routes() {
		register_rest_route( self::ROUTE, '/visit', array(
			'methods'             => 'POST',
			'permission_callback' => '__return_true', // anyone who opens the page; what they may send is checked in record()
			'callback'            => array( __CLASS__, 'record' ),
		) );
	}

	/**
	 * { visit, from?, steps: [[seconds, event, detail], ...] }. The first call for a visit makes its row, later ones add steps.
	 * Answers 204 also when it chose not to log (a bot, too many calls), so the page never retries or shows anything.
	 */
	public static function record( \WP_REST_Request $request ) {
		$body  = json_decode( (string) $request->get_body(), true );
		$visit = is_array( $body ) ? (string) ( $body['visit'] ?? '' ) : '';
		if ( ! preg_match( '/^[a-z0-9]{12,32}$/', $visit ) || ! is_array( $body['steps'] ?? null ) ) {
			return new \WP_Error( 'jss_visit', 'Not a visit.', array( 'status' => 400 ) );
		}
		$agent = substr( (string) ( $_SERVER['HTTP_USER_AGENT'] ?? '' ), 0, 400 ); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput -- only matched and hashed
		if ( '' === $agent || preg_match( self::BOTS, $agent ) ) {
			return new \WP_REST_Response( null, 204 );
		}
		$person = self::person( $agent );
		$calls  = 'jss_vc_' . $person;
		$count  = (int) get_transient( $calls );
		if ( $count >= 300 ) {
			return new \WP_REST_Response( null, 204 ); // about one call every 10 s is normal; this is not a person reading a page
		}
		set_transient( $calls, $count + 1, HOUR_IN_SECONDS );

		$steps = self::clean_steps( array_slice( $body['steps'], 0, 50 ) );
		global $wpdb;
		$t   = self::table();
		$now = current_time( 'mysql', true );
		$row = $wpdb->get_row( $wpdb->prepare( "SELECT id, steps FROM $t WHERE visit = %s", $visit ), ARRAY_A ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		$all = $row ? array_merge( (array) json_decode( $row['steps'], true ), $steps ) : $steps;
		$all = array_slice( $all, 0, self::STEPS );
		$sum = self::summary( $all );
		if ( $row ) {
			$wpdb->update( $t, array( 'seen' => $now, 'steps' => wp_json_encode( $all ) ) + $sum, array( 'id' => (int) $row['id'] ) );
		} else {
			$wpdb->insert( $t, array(
				'visit'     => $visit,
				'person'    => $person,
				'started'   => $now,
				'seen'      => $now,
				'came_from' => self::came_from( (string) ( $body['from'] ?? '' ) ),
				'device'    => self::device( $agent ),
				'steps'     => wp_json_encode( $all ),
			) + $sum );
		}
		return new \WP_REST_Response( null, 204 );
	}

	/** Only known events with known details, each [seconds since the page opened, event, detail]. */
	private static function clean_steps( array $steps ): array {
		$out = array();
		foreach ( $steps as $s ) {
			if ( ! is_array( $s ) || count( $s ) < 2 || ! in_array( $s[1], self::EVENTS, true ) ) {
				continue;
			}
			$detail = (string) ( $s[2] ?? '' );
			if ( '' !== $detail && ! in_array( $detail, array_merge( self::KINDS, self::PLACES, array( 'chair', 'table', 'bay', 'visit', 'treatment' ) ), true ) ) {
				$detail = '';
			}
			$out[] = array( max( 0, min( 86400, (int) $s[0] ) ), $s[1], $detail );
		}
		return $out;
	}

	/** The columns worked out from the steps: the furthest section reached, and whether it booked or downloaded. */
	private static function summary( array $steps ): array {
		$far = -1;
		$out = array( 'booked' => 0, 'downloaded' => 0 );
		foreach ( $steps as $s ) {
			$at = 'section' === $s[1] ? array_search( $s[2], self::PLACES, true ) : false;
			if ( false !== $at ) {
				$far = max( $far, $at );
			}
			$out['booked']     = $out['booked'] || 'booked' === $s[1] ? 1 : 0;
			$out['downloaded'] = $out['downloaded'] || 'download' === $s[1] ? 1 : 0;
		}
		return $out + array( 'reached' => $far >= 0 ? self::PLACES[ $far ] : '' );
	}

	/** The same person (IP and browser) gives the same 16 characters all day and different ones the next; the IP is not kept. */
	private static function person( string $agent ): string {
		$day  = wp_date( 'Y-m-d' );
		$salt = get_option( self::SALT );
		if ( ! is_array( $salt ) || ( $salt['day'] ?? '' ) !== $day ) {
			$salt = array( 'day' => $day, 'key' => wp_generate_password( 32, true, true ) ); // yesterday's key is gone for good
			update_option( self::SALT, $salt, false );
		}
		$ip = (string) ( $_SERVER['REMOTE_ADDR'] ?? '' ); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput -- only hashed
		return substr( hash_hmac( 'sha256', $ip . '|' . $agent, $salt['key'] ), 0, 16 );
	}

	/** The referring site's name only (google.com), never the full address; a campaign tag is kept as it is. */
	private static function came_from( string $from ): string {
		$from = trim( $from );
		if ( preg_match( '/^utm:([a-z0-9._-]{1,40})$/i', $from, $m ) ) {
			return 'campaign: ' . strtolower( $m[1] );
		}
		$host = strtolower( (string) wp_parse_url( $from, PHP_URL_HOST ) );
		$own  = strtolower( (string) wp_parse_url( home_url(), PHP_URL_HOST ) );
		$host = preg_replace( '/^(www|m|l|lm)\./', '', $host );
		return ( '' === $host || $host === $own ) ? '' : substr( $host, 0, 100 );
	}

	/** "Phone · Chrome · Android": enough to know how the page was seen, nothing that picks one person out. */
	private static function device( string $a ): string {
		$kind    = preg_match( '/iPad|Tablet/i', $a ) ? 'Tablet' : ( preg_match( '/Mobi|iPhone|Android/i', $a ) ? 'Phone' : 'Computer' );
		$browser = 'Other';
		foreach ( array( 'Edg/' => 'Edge', 'SamsungBrowser' => 'Samsung', 'OPR/' => 'Opera', 'Firefox/' => 'Firefox', 'Chrome/' => 'Chrome', 'Safari/' => 'Safari' ) as $mark => $name ) {
			if ( false !== strpos( $a, $mark ) ) {
				$browser = $name;
				break;
			}
		}
		$os = 'Other';
		foreach ( array( 'Android' => 'Android', 'iPhone' => 'iOS', 'iPad' => 'iOS', 'Windows' => 'Windows', 'Mac OS' => 'macOS', 'Linux' => 'Linux' ) as $mark => $name ) {
			if ( false !== strpos( $a, $mark ) ) {
				$os = $name;
				break;
			}
		}
		return "$kind · $browser · $os";
	}

	public static function prune() {
		global $wpdb;
		$t = self::table();
		$wpdb->query( $wpdb->prepare( "DELETE FROM $t WHERE started < %s", gmdate( 'Y-m-d H:i:s', time() - self::KEEP * DAY_IN_SECONDS ) ) ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
	}

	/** Visits, people, bookings and downloads since a time (UTC). */
	public static function totals( int $since ): array {
		global $wpdb;
		$t   = self::table();
		$row = $wpdb->get_row( $wpdb->prepare( "SELECT COUNT(*) AS visits, COUNT(DISTINCT person) AS people, COALESCE(SUM(booked),0) AS booked, COALESCE(SUM(downloaded),0) AS downloaded FROM $t WHERE started >= %s", gmdate( 'Y-m-d H:i:s', $since ) ), ARRAY_A ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		return array_map( 'intval', (array) $row );
	}

	/**
	 * Seconds the page was on screen: the time from the first step to the last, without the time the tab was hidden. Any step after
	 * a 'leave' ends the time away (visits logged before 'back' existed have only their next step to go by, so they read a little low).
	 */
	public static function on_screen( array $steps ): int {
		$seconds = 0;
		$before  = 0;
		$away    = false;
		foreach ( $steps as $s ) {
			$seconds += $away ? 0 : max( 0, (int) $s[0] - $before );
			$before   = (int) $s[0];
			$away     = 'leave' === $s[1];
		}
		return $seconds;
	}

	/** One line of what a visit's step means, for the admin page. */
	public static function say( array $s ): string {
		$name  = DemoData::TITLES[ $s[2] ] ?? $s[2];
		$place = array( 'try' => 'Try it', 'kinds' => 'Five kinds', 'how' => 'Set up', 'questions' => 'Questions', 'close' => 'the download section', 'foot' => 'the footer' );
		switch ( $s[1] ) {
			case 'open':
				return __( 'Opened the page', 'justsimple-showcase' );
			case 'section':
				/* translators: %s: a section of the page */
				return sprintf( __( 'Reached %s', 'justsimple-showcase' ), $place[ $s[2] ] ?? $s[2] );
			case 'room':
				/* translators: %s: a word in the hero, such as Table */
				return sprintf( __( 'Clicked "%s" in the hero', 'justsimple-showcase' ), ucfirst( $s[2] ) );
			case 'business':
				/* translators: %s: a demo business */
				return sprintf( __( 'Tried %s', 'justsimple-showcase' ), $name );
			case 'booked':
				/* translators: %s: a demo business */
				return sprintf( __( 'Booked at %s', 'justsimple-showcase' ), $name );
			case 'refused':
				/* translators: %s: a demo business */
				return sprintf( __( 'A booking at %s was refused', 'justsimple-showcase' ), $name );
			case 'download':
				return __( 'Clicked Download', 'justsimple-showcase' );
			case 'hosted':
				return __( 'Clicked Hosted version', 'justsimple-showcase' );
			case 'back':
				return __( 'Came back', 'justsimple-showcase' );
			default:
				return __( 'Switched away or left', 'justsimple-showcase' );
		}
	}

	public static function latest( int $n ): array {
		global $wpdb;
		$t = self::table();
		return (array) $wpdb->get_results( $wpdb->prepare( "SELECT * FROM $t ORDER BY started DESC, id DESC LIMIT %d", $n ), ARRAY_A ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
	}
}
