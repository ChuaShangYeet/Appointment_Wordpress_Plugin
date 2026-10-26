<?php
namespace JustSimple\Booking\Booking;

use JustSimple\Booking\Data\Catalog;

defined( 'ABSPATH' ) || exit;

/**
 * Plain-text emails through wp_mail. Every message passes the `jsb_mail` filter as
 * array( to, subject, body, event, booking ); return false from it to stop a message.
 */
final class Mailer {

	public static function register() {
		add_action( 'jsb_booking_created', array( __CLASS__, 'created' ), 10, 2 );
		add_action( 'jsb_booking_status_changed', array( __CLASS__, 'status_changed' ), 10, 4 );
	}

	public static function created( int $booking_id, string $token ) {
		$b    = BookingService::view( $booking_id, true );
		$site = wp_specialchars_decode( get_bloginfo( 'name' ), ENT_QUOTES );
		$when = self::when( $b );
		$what = implode( ', ', wp_list_pluck( $b['services'], 'name' ) );
		$link = ManageLink::url( $b['code'], $token, $b['module']['id'] );

		if ( $b['contact_email'] ) {
			$lines = array(
				/* translators: %s: customer name */
				sprintf( __( 'Hello %s,', 'justsimple-booking' ), $b['customer']['name'] ),
				'',
				'pending' === $b['status']
					? __( 'We have your booking request. The business will confirm it shortly.', 'justsimple-booking' )
					: __( 'Your booking is confirmed.', 'justsimple-booking' ),
				'',
				$what,
				$when,
				/* translators: %s: staff names */
				$b['staff'] ? sprintf( __( 'With: %s', 'justsimple-booking' ), implode( ', ', wp_list_pluck( $b['staff'], 'name' ) ) ) : null,
				/* translators: %s: booking code */
				sprintf( __( 'Booking code: %s', 'justsimple-booking' ), $b['code'] ),
				$link ? sprintf( __( 'View or cancel: %s', 'justsimple-booking' ), $link ) : null,
			);
			self::send(
				$b['contact_email'],
				/* translators: 1: site name, 2: date and time */
				sprintf( __( '[%1$s] Booking %2$s', 'justsimple-booking' ), $site, 'pending' === $b['status'] ? __( 'requested', 'justsimple-booking' ) : __( 'confirmed', 'justsimple-booking' ) ),
				$lines,
				'created_customer',
				$b
			);
		}

		$module = Catalog::module( $b['module']['id'] );
		$owner  = $module && is_email( $module['settings']['notify_email'] ) ? $module['settings']['notify_email'] : get_option( 'admin_email' );
		self::send(
			$owner,
			/* translators: 1: site name, 2: module title */
			sprintf( __( '[%1$s] New booking: %2$s', 'justsimple-booking' ), $site, $b['module']['title'] ),
			array(
				/* translators: %s: customer name and phone */
				sprintf( __( 'Customer: %s', 'justsimple-booking' ), $b['customer']['name'] . ' (' . $b['customer']['phone'] . ')' ),
				$what,
				$when,
				/* translators: %s: booking status */
				sprintf( __( 'Status: %s', 'justsimple-booking' ), $b['status'] ),
				$b['special_request'] ? sprintf( __( 'Request: %s', 'justsimple-booking' ), $b['special_request'] ) : null,
				admin_url( 'admin.php?page=jsb' ),
			),
			'created_owner',
			$b
		);
	}

	public static function status_changed( int $booking_id, string $from, string $to, string $actor ) {
		$messages = array(
			'confirmed' => __( 'Your booking is confirmed.', 'justsimple-booking' ),
			'rejected'  => __( 'Sorry, the business could not accept your booking request.', 'justsimple-booking' ),
			'cancelled' => __( 'Your booking has been cancelled.', 'justsimple-booking' ),
		);
		if ( ! isset( $messages[ $to ] ) ) {
			return;
		}
		$b = BookingService::view( $booking_id, true );
		if ( ! $b['contact_email'] ) {
			return;
		}
		self::send(
			$b['contact_email'],
			/* translators: 1: site name, 2: new status */
			sprintf( __( '[%1$s] Booking %2$s', 'justsimple-booking' ), wp_specialchars_decode( get_bloginfo( 'name' ), ENT_QUOTES ), $to ),
			array(
				/* translators: %s: customer name */
				sprintf( __( 'Hello %s,', 'justsimple-booking' ), $b['customer']['name'] ),
				'',
				$messages[ $to ],
				'',
				implode( ', ', wp_list_pluck( $b['services'], 'name' ) ),
				self::when( $b ),
				/* translators: %s: booking code */
				sprintf( __( 'Booking code: %s', 'justsimple-booking' ), $b['code'] ),
			),
			'status_' . $to,
			$b
		);
	}

	private static function when( array $b ): string {
		$start = new \DateTimeImmutable( $b['start'] );
		return wp_date( get_option( 'date_format' ) . ' ' . get_option( 'time_format' ), $start->getTimestamp() );
	}

	private static function send( string $to, string $subject, array $lines, string $event, array $booking ) {
		$mail = apply_filters( 'jsb_mail', array( $to, $subject, implode( "\n", array_filter( $lines, static function ( $l ) {
			return null !== $l;
		} ) ), $event, $booking ) );
		if ( $mail ) {
			wp_mail( $mail[0], $mail[1], $mail[2] );
		}
	}
}
