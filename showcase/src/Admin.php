<?php
namespace JustSimple\Showcase;

use JustSimple\Booking\Data\Schema;

defined( 'ABSPATH' ) || exit;

/** Tools › JustSimple Showcase: the demo's state, Reset now, the reset hour, the front page switch, and the caught mail. */
final class Admin {

	const SLUG = 'justsimple-showcase';

	public static function register() {
		add_action( 'admin_menu', static function () {
			add_management_page( __( 'JustSimple Showcase', 'justsimple-showcase' ), __( 'JustSimple Showcase', 'justsimple-showcase' ), 'manage_options', self::SLUG, array( __CLASS__, 'render' ) );
		} );
		add_action( 'admin_post_jss_reset', array( __CLASS__, 'reset' ) );
		add_action( 'admin_post_jss_settings', array( __CLASS__, 'settings' ) );
	}

	private static function back( string $msg ) {
		wp_safe_redirect( add_query_arg( array( 'page' => self::SLUG, 'jss' => $msg ), admin_url( 'tools.php' ) ) );
		exit;
	}

	public static function reset() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'You cannot reset the demo.', 'justsimple-showcase' ), 403 );
		}
		check_admin_referer( 'jss_reset' );
		$out = Reset::run();
		self::back( null === $out ? 'busy' : ( $out['errors'] ? 'partial' : 'reset' ) );
	}

	public static function settings() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'You cannot change the demo settings.', 'justsimple-showcase' ), 403 );
		}
		check_admin_referer( 'jss_settings' );
		$hour = isset( $_POST['hour'] ) ? (int) $_POST['hour'] : 3;
		update_option( Reset::HOUR, max( -1, min( 23, $hour ) ), false );
		update_option( Front::ENABLED, empty( $_POST['front'] ) ? '0' : '1', false );
		Reset::schedule();
		self::back( 'saved' );
	}

	public static function render() {
		global $wpdb;
		$pages = (array) get_option( Reset::PAGES, array() );
		$last  = get_option( Reset::LAST );
		$next  = wp_next_scheduled( Reset::HOOK );
		$fmt   = static function ( $ts ) {
			return $ts ? wp_date( 'j M Y, H:i', $ts ) : '–';
		};
		$notes = array(
			'reset'   => array( 'success', __( 'The demo is fresh again.', 'justsimple-showcase' ) ),
			'partial' => array( 'warning', __( 'The demo was reset, but some steps were refused: see below.', 'justsimple-showcase' ) ),
			'busy'    => array( 'warning', __( 'A reset is already running. Try again in a minute.', 'justsimple-showcase' ) ),
			'saved'   => array( 'success', __( 'Saved.', 'justsimple-showcase' ) ),
		);
		$note  = $notes[ sanitize_key( $_GET['jss'] ?? '' ) ] ?? null; // phpcs:ignore WordPress.Security.NonceVerification.Recommended -- a message, not an action
		?>
		<div class="wrap">
			<h1><?php esc_html_e( 'JustSimple Showcase', 'justsimple-showcase' ); ?></h1>
			<p><?php esc_html_e( 'This site is the live demo of JustSimple Booking. Its five demo booking pages, and every booking visitors make on them, go back to fresh at each reset. No email is sent while this plugin is active.', 'justsimple-showcase' ); ?></p>
			<?php if ( $note ) : ?>
				<div class="notice notice-<?php echo esc_attr( $note[0] ); ?>"><p><?php echo esc_html( $note[1] ); ?></p></div>
			<?php endif; ?>

			<h2><?php esc_html_e( 'Demo booking pages', 'justsimple-showcase' ); ?></h2>
			<?php if ( ! $pages ) : ?>
				<p><?php esc_html_e( 'Not loaded yet. Reset the demo to load them.', 'justsimple-showcase' ); ?></p>
			<?php else : ?>
				<table class="widefat striped" style="max-width:720px">
					<thead><tr><th><?php esc_html_e( 'Booking page', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Kind', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Bookings', 'justsimple-showcase' ); ?></th></tr></thead>
					<tbody>
					<?php foreach ( $pages as $kind => $id ) : ?>
						<tr>
							<td><a href="<?php echo esc_url( admin_url( 'admin.php?page=jsb-modules&edit=' . (int) $id ) ); ?>"><?php echo esc_html( DemoData::TITLES[ $kind ] ?? $kind ); ?></a></td>
							<td><?php echo esc_html( $kind ); ?></td>
							<td><?php echo (int) $wpdb->get_var( $wpdb->prepare( 'SELECT COUNT(*) FROM ' . Schema::table( 'bookings' ) . " WHERE module_id = %d AND status NOT IN ('cancelled','rejected')", $id ) ); ?></td>
						</tr>
					<?php endforeach; ?>
					</tbody>
				</table>
			<?php endif; ?>

			<h2><?php esc_html_e( 'Reset', 'justsimple-showcase' ); ?></h2>
			<p>
				<?php
				/* translators: 1: when the last reset ran, 2: seconds it took, 3: when the next is due */
				echo esc_html( sprintf( __( 'Last reset: %1$s (%2$s s). Next: %3$s. WordPress runs scheduled work when someone visits, so on a quiet night the reset can run later than its hour.', 'justsimple-showcase' ), $fmt( $last['at'] ?? 0 ), $last['seconds'] ?? '–', $fmt( $next ) ) );
				?>
			</p>
			<?php if ( ! empty( $last['errors'] ) ) : ?>
				<ul class="ul-disc"><?php foreach ( array_slice( $last['errors'], 0, 10 ) as $e ) : ?><li><?php echo esc_html( $e ); ?></li><?php endforeach; ?></ul>
			<?php endif; ?>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="jss_reset">
				<?php wp_nonce_field( 'jss_reset' ); ?>
				<?php submit_button( $pages ? __( 'Reset the demo now', 'justsimple-showcase' ) : __( 'Load the demo', 'justsimple-showcase' ), 'primary', 'submit', false ); ?>
				<span class="description"><?php esc_html_e( 'Takes up to a minute.', 'justsimple-showcase' ); ?></span>
			</form>

			<h2><?php esc_html_e( 'Settings', 'justsimple-showcase' ); ?></h2>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="jss_settings">
				<?php wp_nonce_field( 'jss_settings' ); ?>
				<table class="form-table" role="presentation">
					<tr>
						<th scope="row"><label for="jss-hour"><?php esc_html_e( 'Reset every night at', 'justsimple-showcase' ); ?></label></th>
						<td>
							<select id="jss-hour" name="hour">
								<option value="-1"<?php selected( Reset::hour(), -1 ); ?>><?php esc_html_e( 'Never (reset by hand only)', 'justsimple-showcase' ); ?></option>
								<?php for ( $h = 0; $h < 24; $h++ ) : ?>
									<option value="<?php echo (int) $h; ?>"<?php selected( Reset::hour(), $h ); ?>><?php echo esc_html( sprintf( '%02d:00', $h ) ); ?></option>
								<?php endfor; ?>
							</select>
							<p class="description"><?php echo esc_html( sprintf( /* translators: %s: time zone */ __( 'In the site\'s time zone (%s).', 'justsimple-showcase' ), wp_timezone_string() ) ); ?></p>
						</td>
					</tr>
					<tr>
						<th scope="row"><?php esc_html_e( 'Front page', 'justsimple-showcase' ); ?></th>
						<td><label><input type="checkbox" name="front" value="1"<?php checked( Front::enabled() ); ?>> <?php esc_html_e( 'Show the JustSimple Booking marketing page, with its live booking form, as this site\'s front page', 'justsimple-showcase' ); ?></label></td>
					</tr>
				</table>
				<?php submit_button( __( 'Save', 'justsimple-showcase' ) ); ?>
			</form>

			<h2><?php esc_html_e( 'Email caught instead of sent', 'justsimple-showcase' ); ?></h2>
			<?php $mail = MailCatcher::all(); ?>
			<?php if ( ! $mail ) : ?>
				<p><?php esc_html_e( 'None since the last reset.', 'justsimple-showcase' ); ?></p>
			<?php else : ?>
				<table class="widefat striped" style="max-width:900px">
					<thead><tr><th><?php esc_html_e( 'When', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'To', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Subject', 'justsimple-showcase' ); ?></th></tr></thead>
					<tbody>
					<?php foreach ( $mail as $m ) : ?>
						<tr><td><?php echo esc_html( $fmt( $m['at'] ) ); ?></td><td><?php echo esc_html( $m['to'] ); ?></td><td><?php echo esc_html( $m['subject'] ); ?></td></tr>
					<?php endforeach; ?>
					</tbody>
				</table>
			<?php endif; ?>
		</div>
		<?php
	}
}
