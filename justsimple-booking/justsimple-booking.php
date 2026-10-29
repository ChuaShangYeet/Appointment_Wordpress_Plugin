<?php
/**
 * Plugin Name:       JustSimple Booking
 * Plugin URI:        https://justsimple.example/booking
 * Description:       Appointments, table reservations, workshop bays and on-site visits, booked straight from your own WordPress site.
 * Version:           0.1.1
 * Requires at least: 6.2
 * Requires PHP:      7.4
 * Author:            JustSimple
 * License:           GPL-2.0-or-later
 * License URI:       https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain:       justsimple-booking
 * Domain Path:       /languages
 */

defined( 'ABSPATH' ) || exit;

define( 'JSB_VERSION', '0.1.1' );
define( 'JSB_FILE', __FILE__ );
define( 'JSB_DIR', plugin_dir_path( __FILE__ ) );
define( 'JSB_URL', plugin_dir_url( __FILE__ ) );

// PSR-4 style: JustSimple\Booking\Data\Schema -> src/Data/Schema.php. No Composer at runtime, so the zip runs on any host.
spl_autoload_register(
	static function ( $class ) {
		$prefix = 'JustSimple\\Booking\\';
		if ( strncmp( $class, $prefix, strlen( $prefix ) ) !== 0 ) {
			return;
		}
		$file = JSB_DIR . 'src/' . str_replace( '\\', '/', substr( $class, strlen( $prefix ) ) ) . '.php';
		if ( is_readable( $file ) ) {
			require $file;
		}
	}
);

register_activation_hook( __FILE__, array( 'JustSimple\\Booking\\Activator', 'activate' ) );
register_deactivation_hook( __FILE__, array( 'JustSimple\\Booking\\Activator', 'deactivate' ) );

add_action( 'plugins_loaded', array( 'JustSimple\\Booking\\Plugin', 'boot' ) );
