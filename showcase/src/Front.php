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
		$config = array(
			'api'   => untrailingslashit( rest_url( 'justsimple/v1' ) ),
			'pages' => (object) array_map( 'intval', (array) get_option( Reset::PAGES, array() ) ),
		);
		$html = str_replace( '{{assets}}', esc_url( JSS_URL . 'site/' ), (string) file_get_contents( $file ) );
		$html = str_replace( '</head>', '<script>window.jsbShowcase = ' . wp_json_encode( $config ) . ";</script>\n</head>", $html );
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
}
