<?php
namespace JustSimple\Booking;

use JustSimple\Booking\Data\Schema;

defined( 'ABSPATH' ) || exit;

final class Activator {

	/** Capability for setting up modules, services, staff and policies (the engine's "owner" and "manager"). */
	const CAP_MANAGE = 'jsb_manage';
	/** Capability for working the bookings list and schedule (the engine's "staff"). */
	const CAP_WORK = 'jsb_work_bookings';

	/** WP-Cron hook that releases expired pending holds and sends reminders (handler arrives with the booking service). */
	const CRON_SWEEP = 'jsb_sweep';

	public static function activate() {
		Schema::install();
		self::grant_caps();
		add_option( 'jsb_delete_data_on_uninstall', '0', '', false );

		if ( ! wp_next_scheduled( self::CRON_SWEEP ) ) {
			wp_schedule_event( time() + MINUTE_IN_SECONDS, 'hourly', self::CRON_SWEEP );
		}
	}

	public static function deactivate() {
		// Data and capabilities stay: deactivating is not uninstalling.
		wp_clear_scheduled_hook( self::CRON_SWEEP );
	}

	private static function grant_caps() {
		$admin = get_role( 'administrator' );
		if ( $admin ) {
			$admin->add_cap( self::CAP_MANAGE );
			$admin->add_cap( self::CAP_WORK );
		}
		$editor = get_role( 'editor' );
		if ( $editor ) {
			$editor->add_cap( self::CAP_WORK );
		}
	}

	/** Used by uninstall.php. */
	public static function revoke_caps() {
		foreach ( wp_roles()->role_objects as $role ) {
			$role->remove_cap( self::CAP_MANAGE );
			$role->remove_cap( self::CAP_WORK );
		}
	}
}
