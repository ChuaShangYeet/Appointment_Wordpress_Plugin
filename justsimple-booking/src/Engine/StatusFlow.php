<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** The booking state machine, ported from the constants and checks in backend/app/services/booking.py. */
final class StatusFlow {

	/** from-status => allowed to-statuses (blueprint §13.4). Leaving waiting-for-parts is a reschedule, not a transition. */
	const TRANSITIONS = array(
		'pending'           => array( 'confirmed', 'rejected', 'cancelled' ),
		'confirmed'         => array( 'cancelled', 'in-progress', 'completed', 'no-show' ),
		'in-progress'       => array( 'completed', 'waiting-for-parts' ),
		'waiting-for-parts' => array( 'cancelled' ),
	);

	/** Module types that use the arrival ("in-progress") step. */
	const IN_PROGRESS_TYPES = array( 'salon', 'workshop', 'field' );

	/** Moving to these frees the booking's slot. */
	const RELEASING = array( 'rejected', 'cancelled', 'completed', 'no-show' );

	/** What a customer may cancel online; anything started or finished needs the business. */
	const CUSTOMER_CANCELLABLE = array( 'pending', 'confirmed' );

	/** in-progress and completed open this long before the start, because people arrive early. */
	const EARLY_SECONDS = 3600;

	/**
	 * @param array{status:string,start:int,policy_snapshot:array} $booking
	 * @throws EngineError invalid_transition | too_late_to_cancel
	 */
	public static function check( array $booking, string $to, string $module_type, string $actor, int $now ): void {
		$from = $booking['status'];
		if ( 'waiting-for-parts' === $from && 'confirmed' === $to ) {
			throw new EngineError( EngineError::INVALID_TRANSITION, 'A job waiting for parts is rescheduled, not confirmed.' );
		}
		if ( ! in_array( $to, self::TRANSITIONS[ $from ] ?? array(), true ) ) {
			throw new EngineError( EngineError::INVALID_TRANSITION, sprintf( "Cannot move a booking from '%s' to '%s'.", $from, $to ) );
		}
		if ( 'in-progress' === $to && ! in_array( $module_type, self::IN_PROGRESS_TYPES, true ) ) {
			throw new EngineError( EngineError::INVALID_TRANSITION, sprintf( "'in-progress' is not used by %s bookings.", $module_type ) );
		}
		if ( 'waiting-for-parts' === $to && 'workshop' !== $module_type ) {
			throw new EngineError( EngineError::INVALID_TRANSITION, "'waiting-for-parts' is only used by workshop job orders." );
		}
		if ( 'customer' === $actor ) {
			if ( 'cancelled' !== $to ) {
				throw new EngineError( EngineError::INVALID_TRANSITION, 'Customers can only cancel.' );
			}
			if ( ! in_array( $from, self::CUSTOMER_CANCELLABLE, true ) ) {
				throw new EngineError( EngineError::INVALID_TRANSITION, sprintf( "A booking that is '%s' can no longer be cancelled online; please contact the business.", $from ) );
			}
			$deadline = self::cancel_deadline( $booking );
			if ( null !== $deadline && $now > $deadline ) {
				throw new EngineError( EngineError::TOO_LATE_TO_CANCEL, 'Too late to cancel under this booking\'s cancellation policy.', array( 'cancel_deadline' => $deadline ) );
			}
		}
		self::ensure_status_time( $booking, $to, $now );
	}

	/** Refuse to mark a booking as started, finished or missed before its time has come. */
	public static function ensure_status_time( array $booking, string $to, int $now ): void {
		if ( 'no-show' === $to ) {
			$opens = $booking['start'];
		} elseif ( in_array( $to, array( 'in-progress', 'completed' ), true ) && 'in-progress' !== $booking['status'] ) {
			$opens = $booking['start'] - self::EARLY_SECONDS;
		} else {
			return;
		}
		if ( $now < $opens ) {
			throw new EngineError( EngineError::INVALID_TRANSITION, sprintf( 'This booking cannot be marked %s yet.', $to ), array( 'opens' => $opens ) );
		}
	}

	/** Last moment a customer may cancel a *confirmed* booking; a pending request can always be withdrawn. */
	public static function cancel_deadline( array $booking ): ?int {
		if ( 'confirmed' !== $booking['status'] ) {
			return null;
		}
		return $booking['start'] - (int) $booking['policy_snapshot']['min_notice_cancel_s'];
	}

	public static function can_cancel_online( array $booking, int $now ): bool {
		if ( 'pending' === $booking['status'] ) {
			return true;
		}
		$deadline = self::cancel_deadline( $booking );
		return null !== $deadline && $now <= $deadline;
	}
}
