<?php
namespace JustSimple\Showcase;

use JustSimple\Booking\Data\Schema;

defined( 'ABSPATH' ) || exit;

/** Tools › JustSimple Showcase: the demo's state, the visit log, the download emails, Reset now, the reset hour, the front page switch, and the caught mail. */
final class Admin {

	const SLUG = 'justsimple-showcase';

	public static function register() {
		add_action( 'admin_menu', static function () {
			add_management_page( __( 'JustSimple Showcase', 'justsimple-showcase' ), __( 'JustSimple Showcase', 'justsimple-showcase' ), 'manage_options', self::SLUG, array( __CLASS__, 'render' ) );
		} );
		add_action( 'admin_post_jss_reset', array( __CLASS__, 'reset' ) );
		add_action( 'admin_post_jss_settings', array( __CLASS__, 'settings' ) );
		add_action( 'admin_post_jss_emails_csv', array( __CLASS__, 'emails_csv' ) );
		add_action( 'admin_post_jss_email_delete', array( __CLASS__, 'email_delete' ) );
		add_action( 'admin_post_jss_mail_test', array( __CLASS__, 'mail_test' ) );
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

	private static function allowed( string $action ) {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'You cannot do that here.', 'justsimple-showcase' ), 403 );
		}
		check_admin_referer( $action );
	}

	/** Every address, as a spreadsheet. A leading = + - @ is quoted so a spreadsheet does not run it as a formula. */
	public static function emails_csv() {
		self::allowed( 'jss_emails_csv' );
		header( 'Content-Type: text/csv; charset=utf-8' );
		header( 'Content-Disposition: attachment; filename="justsimple-download-emails.csv"' );
		$out = fopen( 'php://output', 'w' );
		fputcsv( $out, array( 'email', 'asked', 'opened', 'times' ), ',', '"', '' );
		foreach ( Downloads::all() as $r ) {
			$r['email'] = preg_replace( '/^([=+\-@])/', "'$1", $r['email'] );
			fputcsv( $out, array( $r['email'], $r['asked'], (string) $r['opened'], $r['times'] ), ',', '"', '' );
		}
		exit;
	}

	public static function email_delete() {
		self::allowed( 'jss_email_delete' );
		Downloads::remove( (int) ( $_POST['id'] ?? 0 ) );
		self::back( 'deleted' );
	}

	public static function mail_test() {
		self::allowed( 'jss_mail_test' );
		$to = sanitize_email( wp_unslash( $_POST['to'] ?? '' ) ); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput.InputNotSanitized -- sanitize_email
		self::back( is_email( $to ) && Downloads::test( $to ) ? 'mailok' : 'mailbad' );
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
			'deleted' => array( 'success', __( 'The address is deleted.', 'justsimple-showcase' ) ),
			'mailok'  => array( 'success', __( 'WordPress handed the test email over. Check that it arrives, in the inbox and not in spam.', 'justsimple-showcase' ) ),
			'mailbad' => array( 'error', __( 'The test email could not be sent. Check the address, and that a mail plugin is set up: this host may not send mail by itself.', 'justsimple-showcase' ) ),
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

			<?php self::visits(); ?>

			<?php self::emails(); ?>

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

	/** The addresses that asked for the plugin, and the test of whether this site can really send mail. */
	private static function emails() {
		$rows = Downloads::all();
		$post = esc_url( admin_url( 'admin-post.php' ) );
		$when = static function ( $t ) {
			return wp_date( 'j M Y, H:i', strtotime( $t . ' UTC' ) );
		};
		?>
		<h2><?php esc_html_e( 'Plugin download emails', 'justsimple-showcase' ); ?></h2>
		<p><?php echo esc_html( sprintf( /* translators: 1: days a link works, 2: days an unopened address is kept */ __( 'Each address gets a private link that works for %1$d days. Opened means the address is real. An address whose link is never opened is removed after %2$d days; resets leave this list alone.', 'justsimple-showcase' ), Downloads::DAYS, Downloads::FORGET ) ); ?></p>
		<?php if ( '' === Downloads::file() ) : ?>
			<div class="notice notice-error inline"><p><?php esc_html_e( 'The plugin zip is missing from this Showcase, so links cannot download anything.', 'justsimple-showcase' ); ?></p></div>
		<?php endif; ?>
		<form method="post" action="<?php echo $post; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- esc_url above ?>" style="margin-bottom:16px">
			<input type="hidden" name="action" value="jss_mail_test">
			<?php wp_nonce_field( 'jss_mail_test' ); ?>
			<label for="jss-test"><?php esc_html_e( 'Send a real test email to', 'justsimple-showcase' ); ?></label>
			<input id="jss-test" name="to" type="email" required class="regular-text">
			<?php submit_button( __( 'Send test', 'justsimple-showcase' ), 'secondary', 'submit', false ); ?>
		</form>
		<?php if ( ! $rows ) : ?>
			<p><?php esc_html_e( 'No one has asked yet.', 'justsimple-showcase' ); ?></p>
			<?php
			return;
		endif;
		?>
		<form method="post" action="<?php echo $post; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- esc_url above ?>" style="margin-bottom:8px">
			<input type="hidden" name="action" value="jss_emails_csv">
			<?php wp_nonce_field( 'jss_emails_csv' ); ?>
			<?php submit_button( __( 'Export as CSV', 'justsimple-showcase' ), 'secondary', 'submit', false ); ?>
		</form>
		<table class="widefat striped" style="max-width:900px">
			<thead><tr><th><?php esc_html_e( 'Email', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Asked', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Link opened', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Downloads', 'justsimple-showcase' ); ?></th><th></th></tr></thead>
			<tbody>
			<?php foreach ( $rows as $r ) : ?>
				<tr>
					<td><?php echo esc_html( $r['email'] ); ?></td>
					<td><?php echo esc_html( $when( $r['asked'] ) ); ?></td>
					<td><?php echo esc_html( $r['opened'] ? $when( $r['opened'] ) : __( 'Not yet', 'justsimple-showcase' ) ); ?></td>
					<td><?php echo (int) $r['times']; ?></td>
					<td>
						<form method="post" action="<?php echo $post; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- esc_url above ?>">
							<input type="hidden" name="action" value="jss_email_delete">
							<input type="hidden" name="id" value="<?php echo (int) $r['id']; ?>">
							<?php wp_nonce_field( 'jss_email_delete' ); ?>
							<?php submit_button( __( 'Delete', 'justsimple-showcase' ), 'link-delete small', 'submit', false ); ?>
						</form>
					</td>
				</tr>
			<?php endforeach; ?>
			</tbody>
		</table>
		<?php
	}

	/** The visit log: totals for today and the last 7 days, then the latest visits, each opening to its steps. */
	private static function visits() {
		$today = ( new \DateTimeImmutable( 'today', wp_timezone() ) )->getTimestamp();
		$spans = array(
			__( 'Today', 'justsimple-showcase' )       => Visits::totals( $today ),
			__( 'Last 7 days', 'justsimple-showcase' ) => Visits::totals( $today - 6 * DAY_IN_SECONDS ),
		);
		$rows  = Visits::latest( 100 );
		?>
		<h2><?php esc_html_e( 'Visits to the marketing page', 'justsimple-showcase' ); ?></h2>
		<p><?php echo esc_html( sprintf( /* translators: %d: days */ __( 'Kept without cookies or IP addresses, for %d days; resets leave them alone. Your own visits while signed in, and obvious bots, are not counted. A reload counts as a new visit. Stayed is the time the page was on screen, not the time the tab was open.', 'justsimple-showcase' ), Visits::KEEP ) ); ?></p>
		<table class="widefat striped" style="max-width:720px">
			<thead><tr><th></th><th><?php esc_html_e( 'Visits', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'People', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Booked', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Asked for the plugin', 'justsimple-showcase' ); ?></th></tr></thead>
			<tbody>
			<?php foreach ( $spans as $label => $t ) : ?>
				<tr><th scope="row"><?php echo esc_html( $label ); ?></th><td><?php echo (int) $t['visits']; ?></td><td><?php echo (int) $t['people']; ?></td><td><?php echo (int) $t['booked']; ?></td><td><?php echo (int) $t['downloaded']; ?></td></tr>
			<?php endforeach; ?>
			</tbody>
		</table>
		<?php if ( ! $rows ) : ?>
			<p><?php esc_html_e( 'No visits yet.', 'justsimple-showcase' ); ?></p>
			<?php
			return;
		endif;
		?>
		<h3><?php esc_html_e( 'Latest visits', 'justsimple-showcase' ); ?></h3>
		<table class="widefat striped" style="max-width:1100px">
			<thead><tr><th><?php esc_html_e( 'When', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Stayed', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Came from', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'Device', 'justsimple-showcase' ); ?></th><th><?php esc_html_e( 'What it did', 'justsimple-showcase' ); ?></th></tr></thead>
			<tbody>
			<?php foreach ( $rows as $v ) : ?>
				<?php
				$steps = (array) json_decode( $v['steps'], true );
				$stay  = Visits::on_screen( $steps );
				$did   = array();
				foreach ( $steps as $s ) {
					if ( in_array( $s[1], array( 'business', 'booked', 'refused', 'download', 'hosted' ), true ) ) {
						$did[ Visits::say( $s ) ] = true; // each thing once
					}
				}
				$did = $did ? implode( ', ', array_keys( $did ) ) : __( 'Only read the page', 'justsimple-showcase' );
				?>
				<tr>
					<td><?php echo esc_html( wp_date( 'j M, H:i', strtotime( $v['started'] . ' UTC' ) ) ); ?></td>
					<td><?php echo esc_html( sprintf( '%d:%02d', intdiv( $stay, 60 ), $stay % 60 ) ); ?></td>
					<td><?php echo esc_html( '' !== $v['came_from'] ? $v['came_from'] : __( 'Direct', 'justsimple-showcase' ) ); ?></td>
					<td><?php echo esc_html( $v['device'] ); ?></td>
					<td>
						<details>
							<summary><?php echo esc_html( $did ); ?></summary>
							<ol>
								<?php foreach ( $steps as $s ) : ?>
									<li><?php echo esc_html( sprintf( '%d:%02d  %s', intdiv( (int) $s[0], 60 ), (int) $s[0] % 60, Visits::say( $s ) ) ); ?></li>
								<?php endforeach; ?>
							</ol>
						</details>
					</td>
				</tr>
			<?php endforeach; ?>
			</tbody>
		</table>
		<?php
	}
}
