<?php
/**
 * Runs when the plugin is deleted from wp-admin (not when it is deactivated).
 * Tables are dropped only if the owner ticked "Also delete every module, booking and customer" in Booking > Settings.
 */

defined( 'WP_UNINSTALL_PLUGIN' ) || exit;

require_once __DIR__ . '/src/Activator.php';
require_once __DIR__ . '/src/Data/Schema.php';

wp_clear_scheduled_hook( JustSimple\Booking\Activator::CRON_SWEEP );
JustSimple\Booking\Activator::revoke_caps();

if ( '1' === get_option( 'jsb_delete_data_on_uninstall' ) ) {
	JustSimple\Booking\Data\Schema::drop_all();
}
delete_option( 'jsb_delete_data_on_uninstall' );
