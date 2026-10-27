<?php
namespace JustSimple\Booking\Admin;

use JustSimple\Booking\Activator;
use JustSimple\Booking\Booking\Access;
use JustSimple\Booking\Data\Health;

defined( 'ABSPATH' ) || exit;

/**
 * wp-admin pages. WordPress's own menu is the sidebar (Booking › Today, Bookings, Modules, Settings, System status); the
 * designed screens draw inside the page canvas from assets/admin, as ES modules talking to the plugin's REST routes through
 * wp.apiFetch, which sends the nonce. Settings and System status keep WordPress's own forms.
 */
final class Menu {

	const SLUG = 'jsb';

	/** page slug => screen module in assets/admin */
	const SCREENS = array(
		'jsb'          => 'today',
		'jsb-bookings' => 'bookings',
		'jsb-modules'  => 'modules',
	);

	public static function register() {
		add_action( 'admin_menu', array( __CLASS__, 'add_pages' ) );
		add_action( 'admin_init', array( __CLASS__, 'register_settings' ) );
		add_action( 'admin_enqueue_scripts', array( __CLASS__, 'enqueue' ) );
		add_filter( 'script_loader_tag', array( __CLASS__, 'module_type' ), 10, 2 );
		add_filter( 'plugin_action_links_' . plugin_basename( JSB_FILE ), array( __CLASS__, 'action_links' ) );
	}

	public static function add_pages() {
		add_menu_page( __( 'Booking', 'justsimple-booking' ), __( 'Booking', 'justsimple-booking' ), Activator::CAP_WORK, self::SLUG, array( __CLASS__, 'render_screen' ), 'dashicons-calendar-alt', 26 );
		add_submenu_page( self::SLUG, __( 'Today', 'justsimple-booking' ), __( 'Today', 'justsimple-booking' ), Activator::CAP_WORK, self::SLUG, array( __CLASS__, 'render_screen' ) );
		add_submenu_page( self::SLUG, __( 'Bookings', 'justsimple-booking' ), __( 'Bookings', 'justsimple-booking' ), Activator::CAP_WORK, 'jsb-bookings', array( __CLASS__, 'render_screen' ) );
		add_submenu_page( self::SLUG, __( 'Booking pages', 'justsimple-booking' ), __( 'Booking pages', 'justsimple-booking' ), Activator::CAP_MANAGE, 'jsb-modules', array( __CLASS__, 'render_screen' ) );
		add_submenu_page( self::SLUG, __( 'Booking settings', 'justsimple-booking' ), __( 'Settings', 'justsimple-booking' ), Activator::CAP_MANAGE, self::SLUG . '-settings', array( __CLASS__, 'render_settings' ) );
		add_submenu_page( self::SLUG, __( 'Booking system status', 'justsimple-booking' ), __( 'System status', 'justsimple-booking' ), Activator::CAP_MANAGE, self::SLUG . '-status', array( __CLASS__, 'render_status' ) );
	}

	private static function current_screen(): ?string {
		$page = isset( $_GET['page'] ) ? sanitize_key( wp_unslash( $_GET['page'] ) ) : ''; // phpcs:ignore WordPress.Security.NonceVerification.Recommended -- choosing a page, not acting
		return self::SCREENS[ $page ] ?? null;
	}

	public static function enqueue() {
		$screen = self::current_screen();
		if ( ! $screen ) {
			return;
		}
		wp_enqueue_style( 'jsb-dm-sans', JSB_URL . 'assets/fonts/dm-sans.css', array(), JSB_VERSION );
		wp_enqueue_style( 'jsb-admin', JSB_URL . 'assets/admin/admin.css', array( 'jsb-dm-sans' ), JSB_VERSION );
		wp_enqueue_script( 'jsb-admin', JSB_URL . 'assets/admin/' . $screen . '.js', array( 'wp-api-fetch' ), JSB_VERSION, true );
		$user = get_current_user_id();
		wp_add_inline_script(
			'jsb-admin',
			'window.jsbAdmin = ' . wp_json_encode(
				array(
					'screen'   => $screen,
					'version'  => JSB_VERSION,
					'timezone' => wp_timezone_string(),
					'manage'   => current_user_can( Activator::CAP_MANAGE ),
					'ownStaff' => Access::own_resources( $user ),
					'urls'     => array(
						'today'    => admin_url( 'admin.php?page=jsb' ),
						'bookings' => admin_url( 'admin.php?page=jsb-bookings' ),
						'modules'  => admin_url( 'admin.php?page=jsb-modules' ),
						'assets'   => JSB_URL . 'assets/',
						'rest'     => rest_url( 'justsimple/v1' ),
						'newPage'  => admin_url( 'post-new.php?post_type=page' ),
					),
				) + ( 'modules' === $screen ? self::setup_config() : array() )
			) . ';',
			'before'
		);
	}

	/** What the setup screen offers to pick from: logins a staff member can be linked to, and the site's pages. */
	private static function setup_config(): array {
		$users = get_users( array( 'capability' => Activator::CAP_WORK, 'fields' => array( 'ID', 'display_name', 'user_login' ), 'number' => 200 ) );
		$pages = get_pages( array( 'post_status' => 'publish', 'number' => 200 ) );
		return array(
			'users' => array_map( static function ( $u ) {
				return array( 'id' => (int) $u->ID, 'name' => $u->display_name . ' (' . $u->user_login . ')' );
			}, $users ),
			'pages' => array_map( static function ( $p ) {
				return array( 'url' => get_permalink( $p ), 'title' => get_the_title( $p ) );
			}, $pages ),
		);
	}

	/** The screens are ES modules (import/export between assets/admin files). */
	public static function module_type( $tag, $handle ) {
		return 'jsb-admin' === $handle ? str_replace( '<script ', '<script type="module" ', $tag ) : $tag;
	}

	public static function render_screen() {
		$titles = array(
			'today'    => __( 'Today', 'justsimple-booking' ),
			'bookings' => __( 'Bookings', 'justsimple-booking' ),
			'modules'  => __( 'Booking pages', 'justsimple-booking' ),
		);
		$screen = self::current_screen();
		?>
		<div class="wrap jsb-wrap">
			<h1 class="screen-reader-text"><?php echo esc_html( $titles[ $screen ] ?? '' ); ?></h1>
			<div id="jsb-admin" class="jsb-admin" data-screen="<?php echo esc_attr( (string) $screen ); ?>">
				<noscript><p><?php esc_html_e( 'The booking screens need JavaScript to be switched on.', 'justsimple-booking' ); ?></p></noscript>
			</div>
		</div>
		<?php
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
		array_unshift( $links, '<a href="' . esc_url( admin_url( 'admin.php?page=' . self::SLUG ) ) . '">' . esc_html__( 'Today', 'justsimple-booking' ) . '</a>' );
		return $links;
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
