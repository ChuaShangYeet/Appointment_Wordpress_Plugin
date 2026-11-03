<?php
namespace JustSimple\Showcase;

defined( 'ABSPATH' ) || exit;

/**
 * Serves the plugin's marketing page (built into site/ by marketing/build.php) as the demo site's front page. Being on the same
 * site as the booking API matters: some free hosts put a browser check in front of every site, which blocks API calls coming
 * from any other address. The page is told where the API is and which booking pages are the demo's; its widget books live.
 */
final class Front {

	const ENABLED = 'jss_front_page'; // '1' (default) or '0'

	public static function register() {
		add_action( 'template_redirect', array( __CLASS__, 'serve' ), 0 );
	}

	public static function enabled(): bool {
		return '0' !== (string) get_option( self::ENABLED, '1' );
	}

	public static function serve() {
		if ( ! self::enabled() || ! is_front_page() || is_admin() ) {
			return;
		}
		// a manage link from a booking email lands here too (?jsb_booking=...&jsb_token=...): the page's widget opens it
		$file = JSS_DIR . 'site/index.html';
		if ( ! is_readable( $file ) ) {
			return; // not built: WordPress's own front page stays
		}
		$pages  = array_map( 'intval', (array) get_option( Reset::PAGES, array() ) );
		$config = array(
			'api'     => untrailingslashit( rest_url( 'justsimple/v1' ) ),
			'pages'   => (object) $pages,
			'modules' => (object) self::details( $pages ),
		);
		$html = str_replace( '{{assets}}', esc_url( JSS_URL . 'site/' ), (string) file_get_contents( $file ) );
		$html = str_replace( '</head>', '<script>window.jsbShowcase = ' . wp_json_encode( $config, JSON_HEX_TAG | JSON_HEX_AMP ) . ";</script>\n</head>", $html );
		// the demo's free times change all day, and its booking page ids change at each reset: no cache may keep this page
		// (W3 Total Cache and most page caches honour DONOTCACHEPAGE; hosts often install one by default)
		if ( ! defined( 'DONOTCACHEPAGE' ) ) {
			define( 'DONOTCACHEPAGE', true );
		}
		status_header( 200 );
		nocache_headers();
		header( 'Content-Type: text/html; charset=utf-8' );
		echo $html; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- our own built page
		exit;
	}

	/**
	 * Each demo business's details as its booking form reads them (GET /modules/{id}, and the workshop's car models), asked of
	 * the booking plugin's own routes inside this request: on a free host every request to the site costs about half a second
	 * of WordPress starting up, so the form asks only for its free times. {kind: {module, vehicles}}; a page that does not
	 * answer is left out and the form fetches it as before.
	 */
	private static function details( array $pages ): array {
		$out = array();
		foreach ( $pages as $kind => $id ) {
			$module = rest_do_request( new \WP_REST_Request( 'GET', "/justsimple/v1/modules/$id" ) );
			if ( 200 !== $module->get_status() ) {
				continue;
			}
			$vehicles = array();
			if ( 'workshop' === $kind ) {
				$v        = rest_do_request( new \WP_REST_Request( 'GET', "/justsimple/v1/modules/$id/supported-vehicles" ) );
				$vehicles = 200 === $v->get_status() ? $v->get_data() : array();
			}
			$out[ $kind ] = array(
				'module'   => rest_get_server()->response_to_data( $module, false ),
				'vehicles' => $vehicles,
			);
		}
		return $out;
	}
}
