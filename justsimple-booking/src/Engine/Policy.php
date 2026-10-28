<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** A booking policy with every duration in seconds. Ported from BookingPolicy and merge_policies / validate_booking_window. */
final class Policy {

	/** @var int|null */
	public $id;
	/** @var string */
	public $name;
	/** @var string 'confirmed' | 'pending' */
	public $default_status;
	/** @var int */
	public $min_notice_booking;
	/** @var int */
	public $min_notice_cancel;
	/** @var int */
	public $booking_window;
	/** @var int */
	public $pending_timeout;
	/** @var int[] ids of the policies a merged policy was made from */
	public $merged_from = array();

	public function __construct( ?int $id, string $name, string $default_status, int $min_notice_booking, int $min_notice_cancel, int $booking_window, int $pending_timeout ) {
		$this->id                 = $id;
		$this->name               = $name;
		$this->default_status     = $default_status;
		$this->min_notice_booking = $min_notice_booking;
		$this->min_notice_cancel  = $min_notice_cancel;
		$this->booking_window     = $booking_window;
		$this->pending_timeout    = $pending_timeout;
	}

	/** From a jsb_policies row. */
	public static function from_row( array $row ): self {
		return new self(
			(int) $row['id'],
			(string) $row['name'],
			(string) $row['default_status'],
			60 * (int) $row['min_notice_booking_minutes'],
			60 * (int) $row['min_notice_cancel_minutes'],
			86400 * (int) $row['booking_window_days'],
			60 * (int) $row['pending_timeout_minutes']
		);
	}

	/** From the snapshot stored on a booking: the rules it was made under, whatever the policy says now. */
	public static function from_snapshot( array $s ): self {
		$p              = self::from_snapshot_fields( $s );
		$p->merged_from = array_map( 'intval', $s['merged_from'] ?? array() );
		return $p;
	}

	private static function from_snapshot_fields( array $s ): self {
		return new self(
			isset( $s['policy_id'] ) ? (int) $s['policy_id'] : null,
			(string) $s['name'],
			(string) $s['default_status'],
			(int) $s['min_notice_booking_s'],
			(int) $s['min_notice_cancel_s'],
			(int) $s['booking_window_s'],
			(int) $s['pending_timeout_s']
		);
	}

	/**
	 * The strictest combination of several policies: longest booking and cancellation notice, shortest window,
	 * pending if any is pending, shortest pending timeout. One distinct policy is returned as it is.
	 *
	 * @param Policy[] $policies
	 */
	public static function merge( array $policies ): self {
		$unique = array();
		foreach ( $policies as $p ) {
			$unique[ (string) $p->id ] = $p;
		}
		$unique = array_values( $unique );
		if ( 1 === count( $unique ) ) {
			return $unique[0];
		}
		$pick   = static function ( $f, $prop ) use ( $unique ) {
			return $f( array_map( static function ( $p ) use ( $prop ) {
				return $p->$prop;
			}, $unique ) );
		};
		$merged = new self(
			null,
			substr( 'Combined: ' . implode( ' + ', array_map( static function ( $p ) {
				return $p->name;
			}, $unique ) ), 0, 100 ),
			in_array( 'pending', array_map( static function ( $p ) {
				return $p->default_status;
			}, $unique ), true ) ? 'pending' : 'confirmed',
			$pick( 'max', 'min_notice_booking' ),
			$pick( 'max', 'min_notice_cancel' ),
			$pick( 'min', 'booking_window' ),
			$pick( 'min', 'pending_timeout' )
		);
		$merged->merged_from = array_map( static function ( $p ) {
			return (int) $p->id;
		}, $unique );
		return $merged;
	}

	/** @throws EngineError outside_window */
	public function validate_start( int $start, int $now ): void {
		if ( $start < $now + $this->min_notice_booking ) {
			throw new EngineError( EngineError::OUTSIDE_WINDOW, 'Too close to the start time to book.' );
		}
		if ( $start > $now + $this->booking_window ) {
			throw new EngineError( EngineError::OUTSIDE_WINDOW, 'Too far in advance to book.' );
		}
	}

	/** Earliest and latest start that may be offered; an admin move ignores notice and window. */
	public function bounds( int $now, bool $ignore_notice = false ): array {
		if ( $ignore_notice ) {
			return array( $now, $now + 3650 * 86400 );
		}
		return array( $now + $this->min_notice_booking, $now + $this->booking_window );
	}

	/** The least time a business has to answer a request, even one made minutes before it starts. */
	const MIN_HOLD = 300;

	/**
	 * When a request waiting for the business lapses: after the pending timeout, but never after the time it holds begins
	 * (for a visit, when the travel starts), since a request still unanswered then cannot be kept. Never sooner than
	 * MIN_HOLD from now, so a request made just before its time is not gone the moment it is made.
	 *
	 * Not in the original engine, which held every request for the full timeout (a 16:30 request could lapse at 18:16).
	 */
	public function hold_until( int $now, int $first_held ): int {
		return min( $now + $this->pending_timeout, max( $first_held, $now + self::MIN_HOLD ) );
	}

	/** Immutable copy kept on the booking (same keys as the engine's policy_snapshot). */
	public function snapshot(): array {
		$s = array(
			'policy_id'            => $this->id,
			'name'                 => $this->name,
			'default_status'       => $this->default_status,
			'min_notice_booking_s' => $this->min_notice_booking,
			'min_notice_cancel_s'  => $this->min_notice_cancel,
			'booking_window_s'     => $this->booking_window,
			'pending_timeout_s'    => $this->pending_timeout,
		);
		if ( $this->merged_from ) {
			$s['merged_from'] = $this->merged_from;
		}
		return $s;
	}
}
