<?php
namespace JustSimple\Booking;

use JustSimple\Booking\Admin\Menu;
use JustSimple\Booking\Booking\HoldSweeper;
use JustSimple\Booking\Booking\Mailer;
use JustSimple\Booking\Data\Schema;
use JustSimple\Booking\Front\Front;
use JustSimple\Booking\Rest\AdminController;
use JustSimple\Booking\Rest\PublicController;

defined( 'ABSPATH' ) || exit;

/**
 * Wires the plugin into WordPress. Each layer registers its own hooks; this class only decides which layers load.
 */
final class Plugin {

	public static function boot() {
		// A plugin updated by copying files never runs the activation hook, so the schema is brought up to date here too.
		Schema::maybe_upgrade();

		load_plugin_textdomain( 'justsimple-booking', false, dirname( plugin_basename( JSB_FILE ) ) . '/languages' );

		add_action( 'rest_api_init', array( PublicController::class, 'register' ) );
		add_action( 'rest_api_init', array( AdminController::class, 'register' ) );
		add_action( Activator::CRON_SWEEP, array( HoldSweeper::class, 'cron' ) );
		Mailer::register();
		Front::register();

		if ( is_admin() ) {
			Menu::register();
		}
	}
}
