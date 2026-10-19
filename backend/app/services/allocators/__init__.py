"""Allocator registry keyed by tenant business type."""
from app.services.allocators.base import Assignment, BaseAllocator
from app.services.allocators.field_service import FieldServiceAllocator
from app.services.allocators.restaurant import RestaurantAllocator
from app.services.allocators.salon import StylistUpsellAllocator
from app.services.allocators.staff import StaffAllocator
from app.services.allocators.workshop import WorkshopAllocator

_REGISTRY: dict[str, type[BaseAllocator]] = {
    "spa": StaffAllocator,
    "salon": StylistUpsellAllocator,
    "restaurant": RestaurantAllocator,
    "automotive": WorkshopAllocator,
    "professional": FieldServiceAllocator,
}


def get_allocator(business_type: str) -> BaseAllocator:
    return _REGISTRY[business_type]()


__all__ = ["Assignment", "BaseAllocator", "get_allocator"]
