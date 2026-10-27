<?php
namespace JustSimple\Booking\Front;

use JustSimple\Booking\Data\Catalog;
use JustSimple\Booking\Data\Schema;

defined( 'ABSPATH' ) || exit;

/**
 * The booking widget on the owner's site: the [justsimple_booking id="3"] shortcode and the "Booking" block both print
 * <jsb-booking module="3">, a web component with its own shadow root, so the theme's CSS cannot reach in and ours cannot leak out.
 * Without JavaScript the element's light-DOM fallback stays: a sentence with the business's phone number.
 */
final class Front {

	const HANDLE = 'jsb-booking';

	public static function register() {
		add_shortcode( 'justsimple_booking', array( __CLASS__, 'shortcode' ) );
		add_action( 'init', array( __CLASS__, 'register_block' ) );
		add_action( 'wp_enqueue_scripts', array( __CLASS__, 'register_assets' ) );
		add_filter( 'script_loader_tag', array( __CLASS__, 'module_type' ), 10, 2 );
		add_filter( 'jsb_manage_url', array( __CLASS__, 'manage_url' ), 10, 4 );
	}

	public static function register_assets() {
		wp_register_script( self::HANDLE, JSB_URL . 'assets/front/jsb-booking.js', array(), JSB_VERSION, true );
		// @font-face rules do not apply inside a shadow root in every browser, so the bundled face is declared on the page itself.
		wp_register_style( 'jsb-dm-sans', JSB_URL . 'assets/fonts/dm-sans.css', array(), JSB_VERSION );
	}

	/** The widget is an ES module. */
	public static function module_type( $tag, $handle ) {
		if ( self::HANDLE !== $handle ) {
			return $tag;
		}
		return str_replace( '<script ', '<script type="module" ', $tag );
	}

	/** [justsimple_booking id="3"] */
	public static function shortcode( $atts ) {
		$atts = shortcode_atts( array( 'id' => 0 ), $atts, 'justsimple_booking' );
		return self::render( (int) $atts['id'] );
	}

	public static function register_block() {
		wp_register_script( 'jsb-block-editor', JSB_URL . 'assets/front/block-editor.js', array( 'wp-blocks', 'wp-element', 'wp-components', 'wp-block-editor', 'wp-api-fetch', 'wp-i18n' ), JSB_VERSION, true );
		register_block_type(
			JSB_DIR . 'blocks/booking',
			array(
				'editor_script'   => 'jsb-block-editor',
				'render_callback' => static function ( $attrs ) {
					return self::render( (int) ( $attrs['module'] ?? 0 ) );
				},
			)
		);
	}

	public static function render( int $module_id ): string {
		$m = Catalog::module( $module_id );
		if ( ! $m || 'active' !== $m['status'] ) {
			// visitors see nothing; someone who can fix it sees why
			return current_user_can( 'jsb_manage' ) ? '<p><em>' . esc_html__( 'This booking page is not switched on yet (Booking › Modules).', 'justsimple-booking' ) . '</em></p>' : '';
		}
		self::remember_page( $m );
		if ( ! wp_script_is( self::HANDLE, 'registered' ) ) {
			self::register_assets(); // a shortcode rendered outside the normal front-end request (a page builder preview)
		}
		wp_enqueue_script( self::HANDLE );
		if ( 'dm-sans' === $m['settings']['font'] ) {
			wp_enqueue_style( 'jsb-dm-sans' );
		}
		$phone    = $m['settings']['phone'];
		$fallback = $phone
			/* translators: 1: business name, 2: phone number */
			? sprintf( __( 'To book %1$s, please call %2$s.', 'justsimple-booking' ), esc_html( $m['title'] ), '<a href="tel:' . esc_attr( preg_replace( '/[^\d+]/', '', $phone ) ) . '">' . esc_html( $phone ) . '</a>' )
			/* translators: %s: business name */
			: sprintf( esc_html__( 'Online booking for %s needs JavaScript to be switched on.', 'justsimple-booking' ), esc_html( $m['title'] ) );
		return sprintf(
			'<jsb-booking module="%d" api="%s" css="%s"><p class="jsb-fallback">%s</p></jsb-booking>',
			$m['id'],
			esc_url( rest_url( 'justsimple/v1' ) ),
			esc_url( JSB_URL . 'assets/front/jsb-booking.css?ver=' . JSB_VERSION ),
			$fallback
		);
	}

	/** The first page that shows a module becomes where its manage links point (an owner can change it later). */
	private static function remember_page( array $m ): void {
		if ( '' !== $m['settings']['page_url'] || ! is_singular() ) {
			return;
		}
		$url = get_permalink();
		if ( ! $url ) {
			return;
		}
		global $wpdb;
		$settings             = $m['settings'];
		$settings['page_url'] = $url;
		$wpdb->update( Schema::table( 'modules' ), array( 'settings' => wp_json_encode( $settings ) ), array( 'id' => $m['id'] ) );
	}

	/** The link in booking emails: the module's page with the booking code and its token, which the widget opens as "manage". */
	public static function manage_url( $url, $code, $token, $module_id ) {
		$m = Catalog::module( (int) $module_id );
		if ( ! $m || '' === $m['settings']['page_url'] ) {
			return $url;
		}
		return add_query_arg( array( 'jsb_booking' => rawurlencode( $code ), 'jsb_token' => rawurlencode( $token ) ), $m['settings']['page_url'] );
	}
}
