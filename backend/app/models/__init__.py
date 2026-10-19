"""Import all models so ``Base.metadata`` is complete for Alembic."""
from app.models.base import Base
from app.models.catalog import Product, Service, ServiceProduct, ServiceResource, SupportedVehicle
from app.models.business import BusinessProfile, Review
from app.models.calendar import CalendarConnection, CalendarEvent
from app.models.compliance import AuditLog, DataRequest
from app.models.customer import Customer, CustomerAddress, Vehicle
from app.models.events import BookingStatusHistory, OutboxEvent
from app.models.identity import User, UserRole
from app.models.notifications import BookingReminder, NotificationLog, NotificationTemplate
from app.models.webhooks import WebhookDelivery, WebhookEndpoint
from app.models.scheduling import (
    Booking,
    BookingAllocation,
    BookingItem,
    BookingPolicy,
    BookingProduct,
    DayOff,
    OperatingHours,
    Resource,
    ResourceShift,
    TableOpenRule,
    TimeSlotSetting,
)
from app.models.tenant import ApiKey, Plan, PlanWarning, Site, Tenant

__all__ = [
    "ApiKey", "AuditLog", "Base", "CalendarConnection", "CalendarEvent", "BusinessProfile", "Review", "DataRequest", "Booking", "BookingStatusHistory", "OutboxEvent", "BookingAllocation", "BookingItem", "BookingPolicy", "BookingProduct", "Customer",
    "CustomerAddress", "DayOff", "OperatingHours", "Plan", "PlanWarning", "Product", "Resource", "ResourceShift", "Service",
    "BookingReminder", "NotificationLog", "NotificationTemplate", "ServiceProduct", "ServiceResource", "Site", "SupportedVehicle", "TableOpenRule", "Tenant", "TimeSlotSetting", "User", "UserRole", "Vehicle", "WebhookDelivery", "WebhookEndpoint",
]
