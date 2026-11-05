<?php
/**
 * Plugin Name:       JustSimple Showcase
 * Description:       The live demo of JustSimple Booking: five demo businesses that reset every night, email caught instead of sent, the plugin's marketing page with its booking form working live, a log of its visits kept without cookies, and the plugin handed out by emailed link. For the demo site only, never a real business's site.
 * Version:           0.3.1
 * Requires at least: 6.2
 * Requires PHP:      7.4
 * Requires Plugins:  justsimple-booking
 * Author:            JustSimple
 * License:           GPL-2.0-or-later
 * Text Domain:       justsimple-showcase
 */

defined( 'ABSPATH' ) || exit;

define( 'JSS_FILE', __FILE__ );
define( 'JSS_DIR', plugin_dir_path( __FILE__ ) );
define( 'JSS_URL', plugin_dir_url( __FILE__ ) );

require_once JSS_DIR . 'src/DemoData.php';
require_once JSS_DIR . 'src/Reset.php';
require_once JSS_DIR . 'src/MailCatcher.php';
require_once JSS_DIR . 'src/Front.php';
require_once JSS_DIR . 'src/Visits.php';
require_once JSS_DIR . 'src/Downloads.php';
require_once JSS_DIR . 'src/Admin.php';

use JustSimple\Showcase\Admin;
use JustSimple\Showcase\Downloads;
use JustSimple\Showcase\Front;
use JustSimple\Showcase\MailCatcher;
use JustSimple\Showcase\Reset;
use JustSimple\Showcase\Visits;

// Mail is caught from the first moment, even before the booking plugin is confirmed: a demo site never emails its visitors.
MailCatcher::register();

add_action( 'plugins_loaded', static function () {
	if ( ! class_exists( 'JustSimple\\Booking\\Plugin' ) ) {
		add_action( 'admin_notices', static function () {
			echo '<div class="notice notice-error"><p>' . esc_html__( 'JustSimple Showcase needs the JustSimple Booking plugin to be active.', 'justsimple-showcase' ) . '</p></div>';
		} );
		return;
	}
	Reset::register();
	Front::register();
	Visits::register();
	Downloads::register();
	if ( is_admin() ) {
		Admin::register();
	}
}, 20 );

register_activation_hook( __FILE__, static function () {
	Reset::schedule();
	Visits::install();
	Downloads::install();
} );
register_deactivation_hook( __FILE__, array( Reset::class, 'unschedule' ) );
