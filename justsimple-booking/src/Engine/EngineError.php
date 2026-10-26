<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/**
 * A rule said no. `error_code` is stable and machine-readable (the widget switches on it); the message is for people.
 * The REST layer turns it into a WP_Error with `status` and `data`.
 */
class EngineError extends \RuntimeException {

	const SLOT_UNAVAILABLE   = 'slot_unavailable';   // 409
	const OUTSIDE_WINDOW     = 'outside_window';     // 422
	const INVALID_TRANSITION = 'invalid_transition'; // 409
	const TOO_LATE_TO_CANCEL = 'too_late_to_cancel'; // 409
	const VALIDATION         = 'validation';         // 422
	const NOT_FOUND          = 'not_found';          // 404
	const IDEMPOTENCY_REUSED = 'idempotency_reused'; // 409
	const BUSY               = 'busy';               // 503

	const STATUS = array(
		self::SLOT_UNAVAILABLE   => 409,
		self::OUTSIDE_WINDOW     => 422,
		self::INVALID_TRANSITION => 409,
		self::TOO_LATE_TO_CANCEL => 409,
		self::VALIDATION         => 422,
		self::NOT_FOUND          => 404,
		self::IDEMPOTENCY_REUSED => 409,
		self::BUSY               => 503,
	);

	/** @var string */
	public $error_code;
	/** @var array<string,mixed> */
	public $data;

	public function __construct( string $error_code, string $message, array $data = array() ) {
		parent::__construct( $message );
		$this->error_code = $error_code;
		$this->data       = $data;
	}

	public function http_status(): int {
		return self::STATUS[ $this->error_code ] ?? 400;
	}
}
