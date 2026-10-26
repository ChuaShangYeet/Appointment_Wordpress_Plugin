<?php
namespace JustSimple\Booking\Admin;

use JustSimple\Booking\Activator;
use JustSimple\Booking\Data\Health;

defined( 'ABSPATH' ) || exit;

/**
 * wp-admin pages. Phase 1 uses WordPress's own admin markup only; the designed admin screens replace the
 * Modules page body later and keep these slugs, so bookmarks and links keep working.
 */
final class Menu {

	const SLUG = 'jsb';

	public static function register() {
		add_action( 'admin_menu', array( __CLASS__, 'add_pages' ) );
		add_action( 'admin_init', array( __CLASS__, 'register_settings' ) );
		add_filter( 'plugin_action_links_' . plugin_basename( JSB_FILE ), array( __CLASS__, 'action_links' ) );
	}

	public static function add_pages() {
		add_menu_page(
			__( 'Booking', 'justsimple-booking' ),
			__( 'Booking', 'justsimple-booking' ),
			Activator::CAP_WORK,
			self::SLUG,
			array( __CLASS__, 'render_modules' ),
			'dashicons-calendar-alt',
			26
		);
		add_submenu_page( self::SLUG, __( 'Booking modules', 'justsimple-booking' ), __( 'Modules', 'justsimple-booking' ), Activator::CAP_WORK, self::SLUG, array( __CLASS__, 'render_modules' ) );
		add_submenu_page( self::SLUG, __( 'Booking settings', 'justsimple-booking' ), __( 'Settings', 'justsimple-booking' ), Activator::CAP_MANAGE, self::SLUG . '-settings', array( __CLASS__, 'render_settings' ) );
		add_submenu_page( self::SLUG, __( 'Booking system status', 'justsimple-booking' ), __( 'System status', 'justsimple-booking' ), Activator::CAP_MANAGE, self::SLUG . '-status', array( __CLASS__, 'render_status' ) );
	}

	public static function register_settings() {
		register_setting(
			'jsb_settings',
			'jsb_delete_data_on_uninstall',
			array(
				'type'              => 'string',
				'sanitize_callback' => static function ( $v ) {
					return $v ? '1' : '0';
				},
				'default'           => '0',
			)
		);
	}

	public static function action_links( $links ) {
		array_unshift( $links, '<a href="' . esc_url( admin_url( 'admin.php?page=' . self::SLUG ) ) . '">' . esc_html__( 'Modules', 'justsimple-booking' ) . '</a>' );
		return $links;
	}

	public static function render_modules() {
		?>
		<div class="wrap">
			<h1><?php esc_html_e( 'Booking modules', 'justsimple-booking' ); ?></h1>
			<p><?php esc_html_e( 'A module is one booking page: haircuts, dinner reservations, car servicing or home visits. Each gets its own services, staff or resources, opening hours and a shortcode to place on any page.', 'justsimple-booking' ); ?></p>
			<p><em><?php esc_html_e( 'Creating modules arrives in the next release.', 'justsimple-booking' ); ?></em></p>
		</div>
		<?php
	}

	public static function render_settings() {
		?>
		<div class="wrap">
			<h1><?php esc_html_e( 'Booking settings', 'justsimple-booking' ); ?></h1>
			<?php settings_errors(); // Only pages under Settings print the "Settings saved" notice by themselves. ?>
			<form method="post" action="options.php">
				<?php settings_fields( 'jsb_settings' ); ?>
				<table class="form-table" role="presentation">
					<tr>
						<th scope="row"><?php esc_html_e( 'When the plugin is deleted', 'justsimple-booking' ); ?></th>
						<td>
							<label>
								<input type="checkbox" name="jsb_delete_data_on_uninstall" value="1" <?php checked( get_option( 'jsb_delete_data_on_uninstall' ), '1' ); ?> />
								<?php esc_html_e( 'Also delete every module, booking and customer it stored', 'justsimple-booking' ); ?>
							</label>
							<p class="description"><?php esc_html_e( 'Left unticked, deleting the plugin keeps your data, so installing it again picks up where you left off.', 'justsimple-booking' ); ?></p>
						</td>
					</tr>
				</table>
				<?php submit_button(); ?>
			</form>
		</div>
		<?php
	}

	public static function render_status() {
		$icons = array(
			'ok'   => array( 'yes-alt', '#1f7a4a', __( 'OK', 'justsimple-booking' ) ),
			'warn' => array( 'warning', '#996800', __( 'Warning', 'justsimple-booking' ) ),
			'bad'  => array( 'dismiss', '#b32d2e', __( 'Problem', 'justsimple-booking' ) ),
		);
		?>
		<div class="wrap">
			<h1><?php esc_html_e( 'Booking system status', 'justsimple-booking' ); ?></h1>
			<p><?php esc_html_e( 'What the booking engine needs from this site and its host.', 'justsimple-booking' ); ?></p>
			<table class="widefat striped" style="max-width:900px">
				<tbody>
				<?php
				foreach ( Health::checks() as $check ) :
					$key  = true === $check['ok'] ? 'ok' : ( null === $check['ok'] ? 'warn' : 'bad' );
					$icon = $icons[ $key ];
					?>
					<tr>
						<td style="width:28px"><span class="dashicons dashicons-<?php echo esc_attr( $icon[0] ); ?>" style="color:<?php echo esc_attr( $icon[1] ); ?>" aria-hidden="true"></span><span class="screen-reader-text"><?php echo esc_html( $icon[2] ); ?></span></td>
						<th scope="row" style="width:220px"><?php echo esc_html( $check['label'] ); ?></th>
						<td><?php echo esc_html( $check['detail'] ); ?></td>
					</tr>
				<?php endforeach; ?>
				</tbody>
			</table>
			<p class="description">
				<?php
				/* translators: 1: plugin version, 2: PHP version, 3: WordPress version */
				echo esc_html( sprintf( __( 'Plugin %1$s · PHP %2$s · WordPress %3$s', 'justsimple-booking' ), JSB_VERSION, PHP_VERSION, get_bloginfo( 'version' ) ) );
				?>
			</p>
		</div>
		<?php
	}
}
