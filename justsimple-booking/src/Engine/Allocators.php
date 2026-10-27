<?php
namespace JustSimple\Booking\Engine;

defined( 'ABSPATH' ) || exit;

/** Module type -> allocator (get_allocator in the engine). Types arrive here as their phases are built. */
final class Allocators {

	const BY_TYPE = array(
		'staff'      => StaffAllocator::class,
		'salon'      => SalonAllocator::class,
		'restaurant' => RestaurantAllocator::class,
		'workshop'   => WorkshopAllocator::class,
	);

	/** @throws EngineError validation for a type not built yet */
	public static function for_type( string $type ): Allocator {
		if ( ! isset( self::BY_TYPE[ $type ] ) ) {
			throw new EngineError( EngineError::VALIDATION, 'This kind of booking page is not available yet.' );
		}
		$class = self::BY_TYPE[ $type ];
		return new $class();
	}

	/** @return string[] */
	public static function types(): array {
		return array_keys( self::BY_TYPE );
	}
}
