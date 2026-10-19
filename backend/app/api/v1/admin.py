"""Admin endpoints (JWT roles: staff < manager < owner)."""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditAdmin, DbDep, Principal, get_tenant, require_role
from app.core import countries
from app.core.countries import AddressProblem
from app.core.exceptions import Forbidden, NotFound, ValidationFailed
from app.core.phone import normalise_phone
from app.core.security import decrypt_field
from app.core.config import get_settings
from app.models import (
    User,
    AuditLog,
    Booking,
    BookingStatusHistory,
    BusinessProfile,
    Customer,
    Review,
    DataRequest,
    BookingItem,
    BookingPolicy,
    BookingProduct,
    DayOff,
    OperatingHours,
    NotificationLog,
    NotificationTemplate,
    Product,
    Resource,
    ResourceShift,
    Service,
    ServiceProduct,
    ServiceResource,
    SupportedVehicle,
    TableOpenRule,
    Tenant,
    TimeSlotSetting,
)
from app.schemas.catalog import (
    ProductIn,
    ProductOut,
    ServiceCreate,
    ServiceOut,
    ServicePartsIn,
    ServiceUpdate,
    SupportedVehicleIn,
    SupportedVehicleOut,
)
from app.schemas.business import (
    DETAIL_KEYS,
    BusinessProfileIn,
    BusinessProfileOut,
    BusinessSettingsIn,
    BusinessSettingsOut,
    ReviewAdminOut,
    ReviewModerate,
)
from app.schemas.countries import TimezoneChoice
from app.schemas.plans import PlanOut, PlanUsageOut, TenantPlanOut, UsageWarningOut
from app.schemas.sites import DomainChangeOut, SiteIn, SiteOut
from app.schemas.identity import RoleOut, UserOut, UserStatusIn
from app.models.notifications import CHANNELS
from app.schemas.notifications import (
    Channel,
    NotifiableEvent,
    NotificationLogOut,
    ReminderSettingsIn,
    ReminderSettingsOut,
    TemplateIn,
    TemplateOut,
)
from app.schemas.compliance import AuditEntryOut, DataRequestCompleted, DataRequestOut, DataRequestReject
from app.schemas.public import RescheduleRequest
from app.schemas.scheduling import (
    ActualDurationIn,
    BookingAdminOut,
    BookingDetailOut,
    BookingStatus,
    BusinessLocationIn,
    BusinessLocationOut,
    BusinessLocationSaved,
    ClashAlert,
    check_table,
    DayOffCreate,
    DayOffOut,
    FollowUpRequest,
    MeetingLinkIn,
    OperatingHoursIn,
    OperatingHoursOut,
    OverrunResult,
    PartsIn,
    PartsOut,
    PolicyCreate,
    PolicyOut,
    ResourceAdminOut,
    ResourceCreate,
    ResourceUpdate,
    ResumeRequest,
    ShiftIn,
    ShiftOut,
    StatusChangeOut,
    StatusPatch,
    TableOpenRuleIn,
    TableOpenRuleOut,
    TicketItemOut,
    TicketPartOut,
    TicketProductOut,
    TimeSlotSettingIn,
    TimeSlotSettingOut,
    UpsellRequest,
    UpsellResult,
)
from app.services import booking as booking_service
from app.services import plans as plans_service
from app.services import sites as sites_service
from app.services import audit, compliance, events, field_service, flagging, upsell
from app.services import geocoding, maps_links
from app.services import parts as parts_service
from app.services import notifications as notifications_service
from app.services import users as users_service

_staff = require_role("staff")
_manager = require_role("manager")
_owner = require_role("owner")
# Router-level auth guarantees the tenant is resolved before ``get_db`` runs; the per-route
# annotations reuse the same callables, so FastAPI's dependency cache avoids a second check.
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(_staff), AuditAdmin])
Staff = Annotated[Principal, Depends(_staff)]
Manager = Annotated[Principal, Depends(_manager)]
Owner = Annotated[Principal, Depends(_owner)]
TenantDep = Annotated[Tenant, Depends(get_tenant)]


async def _own_job_only(principal: Principal, db: AsyncSession, booking_id: uuid.UUID, action: str = "update", booking: Booking | None = None) -> None:
    """Staff tied to one resource (a stylist, a mechanic) work on their own appointments only (blueprint: "own schedule, own appointments").

    Managers, owners and staff tied to nobody (a receptionist) are not held to it. Every route a staff member may call on a single booking must start with this.
    """
    if principal.role == "staff" and principal.resource_id:
        existing = booking or await booking_service.get_booking(db, booking_id)
        if principal.resource_id not in {a.resource_id for a in existing.allocations}:
            raise Forbidden(f"Staff can only {action} their own appointments")


async def _customers(db: AsyncSession, bookings: list[Booking]) -> dict[uuid.UUID, Customer]:
    """Who made each booking, in one query however long the list (never one query per row)."""
    ids = {b.customer_id for b in bookings}
    return {c.id: c for c in (await db.scalars(select(Customer).where(Customer.id.in_(ids)))).all()}


def _admin_out(b: Booking, customer: Customer) -> BookingAdminOut:
    """Every admin role sees who booked: the name, the phone and the booking's own email (staff call a customer who is late or missing).

    An erased customer keeps the anonymised name and shows no phone (the stored one is a placeholder); erasure already cleared the booking's email.
    """
    return BookingAdminOut(
        id=b.id, customer_id=b.customer_id, customer_name=customer.name,
        customer_phone=None if customer.erased_at is not None else customer.phone, contact_email=b.contact_email,
        status=b.status, start_at=b.start_at, end_at=b.end_at,
        party_size=b.party_size, special_request=b.special_request, hold_expires_at=b.hold_expires_at,
        source=b.source, resource_ids=[a.resource_id for a in b.allocations if a.state != "released"],
        service_ids=[i.service_id for i in b.items], has_health_notes=b.health_notes_enc is not None,
        table_type=b.table_type, allow_separate_seating=b.allow_separate_seating,
        minimum_spend_agreed=b.minimum_spend_agreed, vehicle_id=b.vehicle_id,
        lead_time_acknowledged_at=b.lead_time_acknowledged_at, address_id=b.address_id, meeting_link=b.meeting_link,
        parent_booking_id=b.parent_booking_id, actual_duration_minutes=b.actual_duration_minutes,
        schedule_conflict=b.schedule_conflict, flags=list(b.flags),
    )


async def _admin_view(db: AsyncSession, b: Booking) -> BookingAdminOut:
    """One booking with who booked it: every route that answers with a booking does so through here (or the list), so none leaves the customer out."""
    return _admin_out(b, (await _customers(db, [b]))[b.customer_id])


def _flag_header(response: Response, report: flagging.FlagReport) -> None:
    """Tell the caller how many upcoming bookings this change flagged for review (they are never cancelled)."""
    response.headers["X-Bookings-Flagged"] = str(report.newly_flagged)
    response.headers["X-Flags-Lifted"] = str(report.lifted)  # bookings that fit again and lost a flag


# ------------------------------------------------------------------ bookings
@router.get("/bookings", response_model=list[BookingAdminOut])
async def list_bookings(
    principal: Staff, db: DbDep, tenant: TenantDep,
    date: dt.date | None = None, status: BookingStatus | None = None, resource_id: uuid.UUID | None = None,
    table_type: Literal["normal", "vip"] | None = None, schedule_conflict: bool | None = None,
    flagged: bool | None = None, flag: str | None = None, limit: int = 100, offset: int = 0,
) -> list[BookingAdminOut]:
    from zoneinfo import ZoneInfo

    if flag is not None and flag not in flagging.FLAG_REASONS:
        raise ValidationFailed(f"Unknown flag '{flag}'; one of {sorted(flagging.FLAG_REASONS)}")

    if principal.role == "staff" and principal.resource_id:  # staff see only their own appointments
        resource_id = principal.resource_id
    rows = await booking_service.list_bookings(
        db, day=date, tz=ZoneInfo(tenant.timezone), status=status, resource_id=resource_id,
        table_type=table_type, schedule_conflict=schedule_conflict, flagged=flagged, flag=flag,
        limit=max(1, min(limit, 500)), offset=max(0, offset),
    )
    customers = await _customers(db, rows)
    return [_admin_out(b, customers[b.customer_id]) for b in rows]


@router.get("/bookings/{booking_id}/history", response_model=list[StatusChangeOut])
async def booking_history(booking_id: uuid.UUID, principal: Staff, db: DbDep) -> list[BookingStatusHistory]:
    """Every status the booking has been in, oldest first: who moved it, and when."""
    booking = await booking_service.get_booking(db, booking_id)
    await _own_job_only(principal, db, booking_id, "see", booking)
    q = select(BookingStatusHistory).where(BookingStatusHistory.booking_id == booking_id).order_by(BookingStatusHistory.seq)
    return list((await db.scalars(q)).all())


@router.get("/bookings/{booking_id}", response_model=BookingDetailOut)
async def get_booking(booking_id: uuid.UUID, principal: Staff, db: DbDep, tenant: TenantDep) -> BookingDetailOut:
    """Detail view. Decrypted health notes are returned to owner/manager only (PDPA)."""
    b = await booking_service.get_booking(db, booking_id)
    await _own_job_only(principal, db, booking_id, "see", b)
    notes = None
    if b.health_notes_enc and principal.role in ("owner", "manager"):
        notes = decrypt_field(b.health_notes_enc, tenant.id)
        audit.record(db, tenant_id=tenant.id, action="booking.health_notes.read", actor_id=principal.user_id, role=principal.role,
                     target_type="booking", target_id=b.id)  # health data was disclosed: PDPA wants to know who looked
    items = [
        TicketItemOut(service_id=i.service_id, name=name, duration_minutes=i.duration_minutes, is_upsell=i.is_upsell)
        for i, name in (await db.execute(
            select(BookingItem, Service.name).join(Service, Service.id == BookingItem.service_id).where(BookingItem.booking_id == b.id)
        )).all()
    ]
    products = [
        TicketProductOut(product_id=line.product_id, name=name, quantity=line.quantity, unit_price=line.unit_price)
        for line, name in (await db.execute(
            select(BookingProduct, Product.name).join(Product, Product.id == BookingProduct.product_id).where(BookingProduct.booking_id == b.id)
        )).all()
    ]
    customer = (await _customers(db, [b]))[b.customer_id]
    return BookingDetailOut(
        **_admin_out(b, customer).model_dump(), health_notes=notes, customer_gender=customer.gender, policy_snapshot=b.policy_snapshot, items=items, products=products
    )


@router.patch("/bookings/{booking_id}/status", response_model=BookingAdminOut)
async def patch_status(booking_id: uuid.UUID, body: StatusPatch, principal: Staff, db: DbDep, tenant: TenantDep) -> BookingAdminOut:
    await _own_job_only(principal, db, booking_id)
    b = await booking_service.transition_booking(db, tenant, booking_id, body.status, actor_id=principal.user_id)
    return await _admin_view(db, b)


@router.post("/bookings/{booking_id}/reschedule", response_model=BookingAdminOut)
async def reschedule_booking(booking_id: uuid.UUID, body: RescheduleRequest, principal: Manager, db: DbDep, tenant: TenantDep) -> BookingAdminOut:
    """Move a pending or confirmed booking to another time (managers and owners).

    Same services and status; staff stay the same unless ``resource_id`` names another or ``any_available`` asks for the
    least-loaded one. The customer's notice rules do not apply, but the new time must be one the business could serve (409
    otherwise, nothing changes) and lie in the future. The customer is told through the usual ``booking.rescheduled`` event.
    A job ``waiting-for-parts`` is moved with ``/resume``.
    """
    b = await booking_service.reschedule_as_admin(
        db, tenant, booking_id, body.start_at, actor_id=principal.user_id, resource_id=body.resource_id,
        any_available=body.any_available, accept_minimum_spend=body.accept_minimum_spend,
        accepted_minimum_spend=body.accepted_minimum_spend,
    )
    return await _admin_view(db, b)


# ------------------------------------------------------------------ services
@router.get("/services", response_model=list[ServiceOut])
async def list_services(_: Manager, db: DbDep) -> list[Service]:
    return list((await db.scalars(select(Service).order_by(Service.name))).all())


@router.post("/services", response_model=ServiceOut, status_code=201)
async def create_service(body: ServiceCreate, _: Manager, db: DbDep, tenant: TenantDep) -> Service:
    if body.active:
        await plans_service.ensure_can_add_service(db, tenant)
    svc = Service(tenant_id=tenant.id, **body.model_dump(exclude={"resource_ids"}))
    db.add(svc)
    await db.flush()
    db.add_all(ServiceResource(tenant_id=tenant.id, service_id=svc.id, resource_id=r) for r in dict.fromkeys(body.resource_ids))
    await db.flush()
    return svc


@router.patch("/services/{service_id}", response_model=ServiceOut)
async def update_service(
    service_id: uuid.UUID, body: ServiceUpdate, response: Response, _: Manager, db: DbDep, tenant: TenantDep
) -> Service:
    svc = await db.get(Service, service_id)
    if svc is None:
        raise NotFound("Service not found")
    data = body.model_dump(exclude_unset=True, exclude={"resource_ids"})
    if data.get("active") and not svc.active:  # switching one back on takes a place again
        await plans_service.ensure_can_add_service(db, tenant)
    for k, v in data.items():
        setattr(svc, k, v)
    if body.resource_ids is not None:
        await db.execute(delete(ServiceResource).where(ServiceResource.service_id == service_id))
        db.add_all(ServiceResource(tenant_id=tenant.id, service_id=service_id, resource_id=r) for r in dict.fromkeys(body.resource_ids))
    await db.flush()
    await db.refresh(svc)
    if body.resource_ids is not None:  # who can do this service changed: upcoming bookings may no longer fit
        _flag_header(response, await flagging.reevaluate(db, tenant, ["staff_not_qualified"], service_id=service_id))
    return svc


# ------------------------------------------------------------------ resources & shifts
def _resource_view(res: Resource, *, contacts: bool = True) -> ResourceAdminOut:
    out = ResourceAdminOut.model_validate(res, from_attributes=True)
    if not contacts:  # colleagues' phone numbers and emails are for managers and owners
        out.contact_email = out.contact_phone = None
    return out


def _contact(tenant: Tenant, data: dict[str, Any]) -> dict[str, Any]:
    """The contact fields of a create / change, with the phone number written the one way every phone number is (country code of the business)."""
    if data.get("contact_phone"):
        data = {**data, "contact_phone": normalise_phone(data["contact_phone"], tenant.phone_country_code)}
    return data


@router.get("/resources", response_model=list[ResourceAdminOut])
async def list_resources(principal: Staff, db: DbDep) -> list[ResourceAdminOut]:
    rows = (await db.scalars(select(Resource).order_by(Resource.name))).all()
    return [_resource_view(r, contacts=principal.role != "staff") for r in rows]


@router.post("/resources", response_model=ResourceAdminOut, status_code=201)
async def create_resource(body: ResourceCreate, _: Manager, db: DbDep, tenant: TenantDep) -> ResourceAdminOut:
    if body.status == "active":
        await plans_service.ensure_can_add_resource(db, tenant)
    res = Resource(tenant_id=tenant.id, **_contact(tenant, body.model_dump()))
    db.add(res)
    await db.flush()
    return _resource_view(res)


@router.patch("/resources/{resource_id}", response_model=ResourceAdminOut)
async def update_resource(
    resource_id: uuid.UUID, body: ResourceUpdate, response: Response, _: Manager, db: DbDep, tenant: TenantDep
) -> ResourceAdminOut:
    """Rename, (de)activate, resize or reclassify a resource. Upcoming bookings it no longer suits are *flagged*, never
    cancelled: an inactive resource (``resource_inactive``), a table that no longer seats the party (``table_too_small``)
    or moved between normal and VIP (``table_class_changed``). Undoing the change lifts the flags again."""
    res = await db.get(Resource, resource_id)
    if res is None:
        raise NotFound("Resource not found")
    data = _contact(tenant, body.model_dump(exclude_unset=True))
    if data.get("status") == "active" and res.status != "active":  # switching one back on takes a place again
        await plans_service.ensure_can_add_resource(db, tenant)
    if res.type != "staff" and any(data.get(k) for k in ("contact_email", "contact_phone", "meeting_link")):
        raise ValidationFailed("Contact details and a meeting link can only be saved for staff")
    for key, value in data.items():
        setattr(res, key, value)
    if res.type == "table":
        try:
            check_table(res.capacity, res.attributes)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
    await db.flush()
    reasons = [r for field, r in (("status", "resource_inactive"), ("capacity", "table_too_small"), ("attributes", "table_class_changed")) if field in data]
    if reasons:
        _flag_header(response, await flagging.reevaluate(db, tenant, reasons, resource_ids=[resource_id]))
    return _resource_view(res)


@router.get("/resources/{resource_id}/shifts", response_model=list[ShiftOut])
async def get_shifts(resource_id: uuid.UUID, _: Staff, db: DbDep) -> list[ResourceShift]:
    q = select(ResourceShift).where(ResourceShift.resource_id == resource_id).order_by(ResourceShift.weekday, ResourceShift.start_time)
    return list((await db.scalars(q)).all())


@router.put("/resources/{resource_id}/shifts", response_model=list[ShiftOut])
async def replace_shifts(
    resource_id: uuid.UUID, body: list[ShiftIn], response: Response, _: Manager, db: DbDep, tenant: TenantDep
) -> list[ResourceShift]:
    """Replace the whole weekly schedule of a resource."""
    if await db.get(Resource, resource_id) is None:
        raise NotFound("Resource not found")
    await db.execute(delete(ResourceShift).where(ResourceShift.resource_id == resource_id))
    rows = [ResourceShift(tenant_id=tenant.id, resource_id=resource_id, **s.model_dump()) for s in body]
    db.add_all(rows)
    events.enqueue(db, tenant.id, "shift.updated", "resource", resource_id, {"resource_id": str(resource_id)})
    await db.flush()
    _flag_header(response, await flagging.reevaluate(db, tenant, ["outside_shift"], resource_ids=[resource_id]))
    return rows


# ------------------------------------------------------------------ policies, hours, days off
@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(_: Manager, db: DbDep) -> list[BookingPolicy]:
    return list((await db.scalars(select(BookingPolicy).order_by(BookingPolicy.name))).all())


@router.post("/policies", response_model=PolicyOut, status_code=201)
async def create_policy(body: PolicyCreate, _: Manager, db: DbDep, tenant: TenantDep) -> BookingPolicy:
    policy = BookingPolicy(tenant_id=tenant.id, **body.model_dump(exclude={"make_default"}))
    db.add(policy)
    await db.flush()
    if body.make_default:
        await db.execute(update(Tenant).where(Tenant.id == tenant.id).values(default_policy_id=policy.id))
    return policy


@router.put("/operating-hours", response_model=list[OperatingHoursOut])
async def replace_operating_hours(
    body: list[OperatingHoursIn], response: Response, _: Manager, db: DbDep, tenant: TenantDep
) -> list[OperatingHours]:
    """Replace tenant opening hours (an empty list removes the restriction)."""
    await db.execute(delete(OperatingHours))
    rows = [OperatingHours(tenant_id=tenant.id, **h.model_dump()) for h in body]
    db.add_all(rows)
    await db.flush()
    _flag_header(response, await flagging.reevaluate(db, tenant, ["outside_operating_hours"]))
    return rows


@router.get("/days-off", response_model=list[DayOffOut])
async def list_days_off(_: Staff, db: DbDep) -> list[DayOff]:
    return list((await db.scalars(select(DayOff).order_by(DayOff.start_date))).all())


@router.delete("/days-off/{day_off_id}", status_code=204)
async def delete_day_off(day_off_id: uuid.UUID, response: Response, _: Manager, db: DbDep, tenant: TenantDep) -> None:
    """Remove a holiday or leave. Bookings that were flagged only because of it are un-flagged (``X-Flags-Lifted``)."""
    off = await db.get(DayOff, day_off_id)
    if off is None:
        raise NotFound("Day off not found")
    scope = [off.resource_id] if off.resource_id else None
    await db.delete(off)
    await db.flush()
    _flag_header(response, await flagging.reevaluate(db, tenant, ["day_off"], resource_ids=scope))


@router.post("/days-off", response_model=DayOffOut, status_code=201)
async def create_day_off(body: DayOffCreate, response: Response, _: Manager, db: DbDep, tenant: TenantDep) -> DayOff:
    if body.resource_id and await db.get(Resource, body.resource_id) is None:
        raise NotFound("Resource not found")
    off = DayOff(tenant_id=tenant.id, **body.model_dump())
    db.add(off)
    await db.flush()
    scope = [body.resource_id] if body.resource_id else None  # a company holiday touches everyone
    _flag_header(response, await flagging.reevaluate(db, tenant, ["day_off"], resource_ids=scope))
    return off


# ------------------------------------------------------------------ restaurant configuration
@router.get("/time-slot-settings", response_model=TimeSlotSettingOut | None)
async def get_time_slot_settings(_: Manager, db: DbDep) -> TimeSlotSetting | None:
    return await db.scalar(select(TimeSlotSetting))


@router.put("/time-slot-settings", response_model=TimeSlotSettingOut)
async def put_time_slot_settings(
    body: TimeSlotSettingIn, response: Response, _: Manager, db: DbDep, tenant: TenantDep
) -> TimeSlotSetting:
    """Create or replace the tenant's slot rules (flexible/fixed mode, duration, interval, buffer)."""
    if body.policy_id and await db.get(BookingPolicy, body.policy_id) is None:
        raise NotFound("Policy not found")
    setting = await db.scalar(select(TimeSlotSetting))
    if setting is None:
        setting = TimeSlotSetting(tenant_id=tenant.id, **body.model_dump())
        db.add(setting)
    else:
        for k, v in body.model_dump().items():
            setattr(setting, k, v)
    await db.flush()
    _flag_header(response, await flagging.reevaluate(db, tenant, ["outside_operating_hours", "outside_slot_times"]))
    return setting


@router.get("/table-open-rules", response_model=list[TableOpenRuleOut])
async def list_table_open_rules(_: Manager, db: DbDep) -> list[TableOpenRule]:
    return list((await db.scalars(select(TableOpenRule).order_by(TableOpenRule.table_type, TableOpenRule.weekday))).all())


@router.put("/table-open-rules", response_model=list[TableOpenRuleOut])
async def replace_table_open_rules(
    body: list[TableOpenRuleIn], response: Response, _: Manager, db: DbDep, tenant: TenantDep
) -> list[TableOpenRule]:
    """Replace all rules. ``open_tables`` may not exceed the number of active tables of that class."""
    keys = [(r.table_type, r.weekday, r.specific_date) for r in body]
    if len(keys) != len(set(keys)):
        raise ValidationFailed("Duplicate rule for the same table type and weekday/date")
    totals = {"normal": 0, "vip": 0}
    for table in (await db.scalars(select(Resource).where(Resource.type == "table", Resource.status == "active"))).all():
        totals["vip" if table.attributes.get("is_vip") else "normal"] += 1
    for rule in body:
        if rule.open_tables > totals[rule.table_type]:
            raise ValidationFailed(
                f"open_tables ({rule.open_tables}) exceeds the {totals[rule.table_type]} active {rule.table_type} tables"
            )
    await db.execute(delete(TableOpenRule))
    rows = [TableOpenRule(tenant_id=tenant.id, **r.model_dump()) for r in body]
    db.add_all(rows)
    await db.flush()
    _flag_header(response, await flagging.reevaluate(db, tenant, ["table_cap_exceeded"]))
    return rows


# ------------------------------------------------------------------ users
async def _user_out(db: Any, users: list[User]) -> list[UserOut]:
    roles = await users_service.roles_by_user(db, [u.id for u in users])
    return [
        UserOut(id=u.id, external_id=u.external_id, display_name=u.display_name, status=u.status, last_seen_at=u.last_seen_at,
                roles=[RoleOut(role=r.role, resource_id=r.resource_id) for r in roles.get(u.id, [])])
        for u in users
    ]


@router.get("/users", response_model=list[UserOut])
async def list_users(_: Manager, db: DbDep, status: Literal["active", "disabled"] | None = None) -> list[UserOut]:
    """People who have logged in to the admin, with the roles WordPress gave them."""
    q = select(User).order_by(User.created_at, User.id)
    if status:
        q = q.where(User.status == status)
    return await _user_out(db, list((await db.scalars(q)).all()))


@router.patch("/users/{user_id}", response_model=UserOut)
async def set_user_status(user_id: uuid.UUID, body: UserStatusIn, principal: Owner, db: DbDep) -> UserOut:
    """Disable or re-enable a user (owner only). A disabled user gets no new token and their current ones stop at once."""
    user = await users_service.set_status(db, principal.tenant_id, user_id, body.status, principal.user_id)
    return (await _user_out(db, [user]))[0]


# ------------------------------------------------------------------ business profile and reviews
@router.get("/business-profile", response_model=BusinessProfileOut)
async def get_business_profile(_: Manager, db: DbDep, tenant: TenantDep) -> BusinessProfileOut:
    profile = await db.scalar(select(BusinessProfile))
    return BusinessProfileOut.model_validate(profile, from_attributes=True) if profile else BusinessProfileOut(business_name=tenant.name)


@router.put("/business-profile", response_model=BusinessProfileOut)
async def put_business_profile(body: BusinessProfileIn, _: Manager, db: DbDep, tenant: TenantDep) -> BusinessProfile:
    """Save the whole profile (fields left out are cleared). ``details`` may only hold what suits this kind of business."""
    allowed = DETAIL_KEYS.get(tenant.business_type, frozenset())
    unknown = sorted(set(body.details) - allowed)
    if unknown:
        raise ValidationFailed(f"details {unknown} do not apply to a {tenant.business_type} business; allowed: {sorted(allowed)}")
    country = countries.get(tenant.country)
    try:
        address = body.address_for(country)
    except AddressProblem as exc:
        raise ValidationFailed(str(exc)) from exc
    values = {**body.model_dump(), **countries.profile_columns(country, address)}  # (the parts as the country spells them, and ``address`` written from them)
    profile = await db.scalar(select(BusinessProfile))
    if profile is None:
        profile = BusinessProfile(tenant_id=tenant.id, **values)
        db.add(profile)
    else:
        for key, value in values.items():
            setattr(profile, key, value)
    await db.flush()
    await db.refresh(profile)
    return profile


@router.get("/plan", response_model=TenantPlanOut)
async def get_plan(_: Manager, db: DbDep, tenant: TenantDep) -> TenantPlanOut:
    """The business's plan, what it allows, what is in use now, and any limit it is over (after a move to a smaller plan)."""
    plan = await plans_service.plan_of(db, tenant)
    limits, used = plans_service.limits_of(plan, tenant), await plans_service.usage(db, tenant)
    site = await sites_service.first_site(db, tenant)
    return TenantPlanOut(
        plan=PlanOut.of(plan, limits), customised=sorted(plans_service.overrides_of(tenant)), usage=PlanUsageOut(**asdict(used)),
        over_limit=plans_service.over_limit(limits, used), warnings=[UsageWarningOut(**w) for w in plans_service.warnings(limits, used)],
        domain_change=DomainChangeOut(every_days=limits.domain_change_days, next_allowed_at=sites_service.next_domain_change(plan, tenant, site) if site is not None else None),
    )


# ------------------------------------------------------------------ the business's website
async def _site_out(db: DbDep, tenant: Tenant) -> SiteOut:
    site = await sites_service.first_site(db, tenant)
    if site is None:
        raise NotFound("This business has no site")
    plan = await plans_service.plan_of(db, tenant)
    return SiteOut(
        site_id=site.id, name=site.name, domain=site.domain, allowed_origins=list(site.allowed_origins),
        origins_follow_domain=plans_service.limit_value(plan, tenant, "domains") is not None, domain_changed_at=site.domain_changed_at,
        domain_change=DomainChangeOut(every_days=plans_service.limit_value(plan, tenant, "domain_change_days"), next_allowed_at=sites_service.next_domain_change(plan, tenant, site)),
    )


@router.get("/site", response_model=SiteOut)
async def get_site(_: Manager, db: DbDep, tenant: TenantDep) -> SiteOut:
    """The business's website address and the browser origins its publishable key is accepted from, and when the address may next be changed."""
    return await _site_out(db, tenant)


@router.put("/site", response_model=SiteOut)
async def put_site(body: SiteIn, _: Owner, db: DbDep, tenant: TenantDep) -> SiteOut:
    """Owner only. Change the website address. On the free plan the address is one domain (www and non-www count as one), the origins follow it, and it can change once every
    30 days (402 ``plan_limit_reached`` with ``next_allowed_at`` otherwise; nothing changes then). The address given at sign-up is not a change. A plan without a domain limit keeps a
    free-form list of origins: ``allowed_origins`` replaces it (empty = any origin)."""
    site = await sites_service.first_site(db, tenant)
    if site is None:
        raise NotFound("This business has no site")
    await sites_service.update_site(db, tenant, site, domain=body.domain, allowed_origins=body.allowed_origins)
    return await _site_out(db, tenant)


def _settings_out(tenant: Tenant) -> BusinessSettingsOut:
    country = countries.get(tenant.country)
    return BusinessSettingsOut(
        country=country.code, country_name=country.name, currency=country.currency, phone_country_code=country.calling_code, timezone=tenant.timezone,
        timezone_choices=[TimezoneChoice(timezone=zone, label=label) for zone, label in country.timezones],
    )


@router.get("/business-settings", response_model=BusinessSettingsOut)
async def get_business_settings(_: Manager, tenant: TenantDep) -> BusinessSettingsOut:
    """The country (chosen when the business was set up), its currency and phone code, and the time zone with the others the country offers."""
    return _settings_out(tenant)


@router.put("/business-settings", response_model=BusinessSettingsOut)
async def put_business_settings(body: BusinessSettingsIn, response: Response, _: Owner, db: DbDep, tenant: TenantDep) -> BusinessSettingsOut:
    """Owner only. Move the business to another of its country's time zones (a Queensland business set up as Sydney, say).

    Opening hours and shifts are clock times, so they move with the zone: upcoming bookings no longer inside them are flagged for review, never
    cancelled. The country itself is fixed at set-up.
    """
    country = countries.get(tenant.country)
    if not country.has_timezone(body.timezone):
        raise ValidationFailed(f"{body.timezone!r} is not one of {country.name}'s time zones: " + ", ".join(zone for zone, _label in country.timezones))
    if body.timezone != tenant.timezone:
        await db.execute(update(Tenant).where(Tenant.id == tenant.id).values(timezone=body.timezone))
        tenant.timezone = body.timezone
        _flag_header(response, await flagging.reevaluate(db, tenant, ["outside_shift", "day_off", "outside_operating_hours", "outside_slot_times"]))
    return _settings_out(tenant)


@router.get("/reviews", response_model=list[ReviewAdminOut])
async def list_reviews(
    _: Manager, db: DbDep, status: Literal["published", "hidden"] | None = None, resource_id: uuid.UUID | None = None, limit: int = 100, offset: int = 0
) -> list[Review]:
    q = select(Review).order_by(Review.created_at.desc(), Review.id).limit(max(1, min(limit, 500))).offset(max(0, offset))
    if status:
        q = q.where(Review.status == status)
    if resource_id:
        q = q.where(Review.resource_id == resource_id)
    return list((await db.scalars(q)).all())


@router.patch("/reviews/{review_id}", response_model=ReviewAdminOut)
async def moderate_review(review_id: uuid.UUID, body: ReviewModerate, _: Manager, db: DbDep) -> Review:
    """Hide a review from the public page (or publish it again). It is never deleted."""
    review = await db.get(Review, review_id)
    if review is None:
        raise NotFound("Review not found")
    review.status = body.status
    await db.flush()
    return review


# ------------------------------------------------------------------ PDPA: audit log and data requests
def _like(text: str) -> str:
    """A ``LIKE`` pattern matching ``text`` anywhere, with its own wildcards taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("/audit-log", response_model=list[AuditEntryOut])
async def list_audit_log(
    _: Owner, db: DbDep, action: str | None = None, actor_id: str | None = None, target_id: str | None = None,
    since: dt.datetime | None = None, until: dt.datetime | None = None, limit: int = 100, offset: int = 0,
) -> list[AuditLog]:
    """Who did what, newest first (owner only). ``action`` matches anywhere in the action text."""
    q = select(AuditLog).order_by(AuditLog.seq.desc()).limit(max(1, min(limit, 500))).offset(max(0, offset))
    if action:
        q = q.where(AuditLog.action.ilike(_like(action), escape="\\"))
    if actor_id:
        q = q.where(AuditLog.actor_id == actor_id)
    if target_id:
        q = q.where(AuditLog.target_id == target_id)
    if since:
        q = q.where(AuditLog.occurred_at >= since)
    if until:
        q = q.where(AuditLog.occurred_at < until)
    return list((await db.scalars(q)).all())


@router.get("/data-requests", response_model=list[DataRequestOut])
async def list_data_requests(
    _: Owner, db: DbDep, status: Literal["pending", "completed", "rejected"] | None = None, limit: int = 100, offset: int = 0
) -> list[DataRequest]:
    """Customers' access and erasure requests (owner only), oldest first."""
    q = select(DataRequest).order_by(DataRequest.requested_at).limit(max(1, min(limit, 500))).offset(max(0, offset))
    if status:
        q = q.where(DataRequest.status == status)
    return list((await db.scalars(q)).all())


@router.post("/data-requests/{request_id}/complete", response_model=DataRequestCompleted)
async def complete_data_request(request_id: uuid.UUID, principal: Owner, db: DbDep, tenant: TenantDep) -> DataRequestCompleted:
    """Carry out a pending request. Access returns the export; erasure anonymises the customer (409 while they have an open booking)."""
    request, export = await compliance.complete_request(db, tenant, request_id, principal.user_id, principal.role, dt.datetime.now(dt.UTC))
    return DataRequestCompleted(request=DataRequestOut.model_validate(request), export=export)


@router.post("/data-requests/{request_id}/reject", response_model=DataRequestOut)
async def reject_data_request(request_id: uuid.UUID, body: DataRequestReject, principal: Owner, db: DbDep) -> DataRequest:
    return await compliance.reject_request(db, request_id, principal.user_id, body.note, dt.datetime.now(dt.UTC))


@router.get("/data-requests/{request_id}/export")
async def export_data_request(request_id: uuid.UUID, principal: Owner, db: DbDep, tenant: TenantDep) -> dict[str, Any]:
    """The export for an ``access`` request, again (each download is written to the audit log)."""
    request = await db.get(DataRequest, request_id)
    if request is None:
        raise NotFound("Data request not found")
    if request.kind != "access":
        raise ValidationFailed("Only an access request has an export")
    audit.record(db, tenant_id=tenant.id, action="customer.data.export", actor_id=principal.user_id, role=principal.role,
                 target_type="customer", target_id=request.customer_id, detail={"request_id": str(request.id)})
    return await compliance.export_customer(db, tenant, request.customer_id)


# ------------------------------------------------------------------ automotive
@router.post("/bookings/{booking_id}/resume", response_model=BookingAdminOut)
async def resume_booking(booking_id: uuid.UUID, body: ResumeRequest, principal: Staff, db: DbDep, tenant: TenantDep) -> BookingAdminOut:
    """Reschedule a job that is ``waiting-for-parts``: allocates mechanic + bay again and confirms it."""
    await _own_job_only(principal, db, booking_id)
    b = await booking_service.resume_booking(
        db, tenant, booking_id, body.start_at, resource_id=body.resource_id, service_ids=body.service_ids, actor_id=principal.user_id
    )
    return await _admin_view(db, b)


@router.get("/supported-vehicles", response_model=list[SupportedVehicleOut])
async def list_supported_vehicles(_: Manager, db: DbDep) -> list[SupportedVehicle]:
    return list((await db.scalars(select(SupportedVehicle).order_by(SupportedVehicle.make, SupportedVehicle.model))).all())


@router.post("/supported-vehicles", response_model=SupportedVehicleOut, status_code=201)
async def create_supported_vehicle(body: SupportedVehicleIn, _: Manager, db: DbDep, tenant: TenantDep) -> SupportedVehicle:
    sv = SupportedVehicle(tenant_id=tenant.id, **body.model_dump())
    db.add(sv)
    await db.flush()
    return sv


@router.delete("/supported-vehicles/{vehicle_id}", status_code=204)
async def delete_supported_vehicle(vehicle_id: uuid.UUID, _: Manager, db: DbDep) -> None:
    sv = await db.get(SupportedVehicle, vehicle_id)
    if sv is None:
        raise NotFound("Supported vehicle not found")
    await db.delete(sv)


@router.get("/products", response_model=list[ProductOut])
async def list_products(_: Manager, db: DbDep) -> list[Product]:
    return list((await db.scalars(select(Product).order_by(Product.name))).all())


@router.post("/products", response_model=ProductOut, status_code=201)
async def create_product(body: ProductIn, _: Manager, db: DbDep, tenant: TenantDep) -> Product:
    product = Product(tenant_id=tenant.id, **body.model_dump())
    db.add(product)
    await db.flush()
    return product


@router.put("/products/{product_id}", response_model=ProductOut)
async def replace_product(product_id: uuid.UUID, body: ProductIn, _: Manager, db: DbDep) -> Product:
    """Full update, e.g. flipping a part to ``available_on_order`` with its lead time."""
    product = await db.get(Product, product_id)
    if product is None:
        raise NotFound("Product not found")
    for k, v in body.model_dump().items():
        setattr(product, k, v)
    await db.flush()
    await db.refresh(product)
    return product


@router.put("/services/{service_id}/parts", response_model=list[uuid.UUID])
async def replace_service_parts(service_id: uuid.UUID, body: ServicePartsIn, _: Manager, db: DbDep, tenant: TenantDep) -> list[uuid.UUID]:
    """Set the parts a service needs; on-order parts trigger the customer lead-time disclaimer."""
    if await db.get(Service, service_id) is None:
        raise NotFound("Service not found")
    ids = list(dict.fromkeys(body.product_ids))
    found = set((await db.scalars(select(Product.id).where(Product.id.in_(ids)))).all()) if ids else set()
    if found != set(ids):
        raise NotFound("Unknown product in product_ids")
    await db.execute(delete(ServiceProduct).where(ServiceProduct.service_id == service_id))
    db.add_all(ServiceProduct(tenant_id=tenant.id, service_id=service_id, product_id=p) for p in ids)
    await db.flush()
    return ids


# ------------------------------------------------------------------ field service (professional)
@router.get("/business-location", response_model=BusinessLocationOut)
async def get_business_location(_: Manager, tenant: TenantDep) -> BusinessLocationOut:
    return BusinessLocationOut(latitude=tenant.latitude, longitude=tenant.longitude, service_radius_km=tenant.service_radius_km)


@router.put("/business-location", response_model=BusinessLocationSaved)
async def put_business_location(
    body: BusinessLocationIn, response: Response, _: Manager, db: DbDep, tenant: TenantDep
) -> BusinessLocationSaved:
    """Where the business is and how far on-site services travel (used for the PostGIS radius check).

    Give the coordinates, the address to look up (a precise match is required; the response shows how the provider wrote it so it can be checked),
    or the business's Google Maps link: the point is read from it (a share link is followed to Google, see ``app.services.maps_links``), and the link
    becomes the profile's ``google_maps_url`` for directions.
    """
    formatted: str | None = None
    maps_url: str | None = None
    if body.maps_url is not None:
        maps_url = body.maps_url.strip()
        reading = await maps_links.resolve(maps_url, timeout=get_settings().maps_link_timeout_seconds)
        if reading.point is not None:
            latitude, longitude = reading.point.latitude, reading.point.longitude
        elif reading.words is not None:  # a place search: looked up like a typed address, so the same precision rule applies
            found = await geocoding.locate_exact(reading.words, countries.get(tenant.country).region)
            latitude, longitude, formatted = found.latitude, found.longitude, found.formatted_address
        else:
            raise maps_links.unreadable()
        profile = await db.scalar(select(BusinessProfile))
        if profile is None:
            db.add(BusinessProfile(tenant_id=tenant.id, business_name=tenant.name, google_maps_url=maps_url))
        else:
            profile.google_maps_url = maps_url
    elif body.address is not None:
        found = await geocoding.locate_exact(body.address, countries.get(tenant.country).region)
        latitude, longitude, formatted = found.latitude, found.longitude, found.formatted_address
    else:
        if body.latitude is None or body.longitude is None:  # (the schema sees to it; this holds even if assertions were stripped, so a business never loses its location)
            raise ValidationFailed("send either latitude and longitude, or an address to look up")
        latitude, longitude = body.latitude, body.longitude
    await db.execute(update(Tenant).where(Tenant.id == tenant.id).values(latitude=latitude, longitude=longitude, service_radius_km=body.service_radius_km))
    _flag_header(response, await flagging.reevaluate(db, tenant, ["outside_service_area"]))
    return BusinessLocationSaved(latitude=latitude, longitude=longitude, service_radius_km=body.service_radius_km, formatted_address=formatted, maps_url=maps_url)


@router.post("/bookings/{booking_id}/follow-up", response_model=BookingAdminOut, status_code=201)
async def create_follow_up(booking_id: uuid.UUID, body: FollowUpRequest, principal: Staff, db: DbDep, tenant: TenantDep) -> BookingAdminOut:
    """Two-step workflow: book the standard service that follows an inspection."""
    await _own_job_only(principal, db, booking_id)
    b = await field_service.create_follow_up(
        db, tenant, booking_id, body.service_id, body.start_at, resource_id=body.resource_id, actor_id=principal.user_id
    )
    return await _admin_view(db, b)


@router.patch("/bookings/{booking_id}/actual-duration", response_model=OverrunResult)
async def patch_actual_duration(booking_id: uuid.UUID, body: ActualDurationIn, principal: Staff, db: DbDep, tenant: TenantDep) -> OverrunResult:
    """Report how long an in-progress job really took; overruns that collide with the next job are flagged."""
    await _own_job_only(principal, db, booking_id)
    b, conflicts = await field_service.record_actual_duration(db, tenant, booking_id, body.minutes)
    return OverrunResult(booking=await _admin_view(db, b), conflicting_booking_ids=conflicts)


@router.post("/bookings/{booking_id}/resolve-conflict", response_model=BookingAdminOut)
async def resolve_conflict(booking_id: uuid.UUID, _: Manager, db: DbDep) -> BookingAdminOut:
    return await _admin_view(db, await field_service.resolve_schedule_conflict(db, booking_id))


@router.post("/bookings/{booking_id}/clear-flags", response_model=BookingAdminOut)
async def clear_flags(booking_id: uuid.UUID, _: Manager, db: DbDep) -> BookingAdminOut:
    """Dismiss the review flags once a manager has looked at the booking and decided to keep it as it is."""
    b = await booking_service.get_booking(db, booking_id)
    b.flags = []
    await db.flush()
    return await _admin_view(db, b)


@router.patch("/bookings/{booking_id}/meeting-link", response_model=BookingAdminOut)
async def patch_meeting_link(booking_id: uuid.UUID, body: MeetingLinkIn, principal: Staff, db: DbDep, tenant: TenantDep) -> BookingAdminOut:
    """Attach a join link by hand (there is no video-conferencing provider integration yet)."""
    await _own_job_only(principal, db, booking_id)
    return await _admin_view(db, await field_service.set_meeting_link(db, tenant, booking_id, body.url))


# ------------------------------------------------------------------ salon: on-the-spot upsell
@router.post("/bookings/{booking_id}/items", response_model=UpsellResult)
async def add_ticket_items(booking_id: uuid.UUID, body: UpsellRequest, principal: Staff, db: DbDep, tenant: TenantDep) -> UpsellResult:
    """Add add-ons and/or products to an in-progress appointment.

    An add-on stretches the stylist's reservation; if that runs into their next appointment the response
    lists it under ``conflicts`` (an alert) and both appointments are flagged instead of being forced.
    """
    await _own_job_only(principal, db, booking_id)
    outcome = await upsell.add_to_ticket(
        db, tenant, booking_id, body.service_ids, [(p.product_id, p.quantity) for p in body.products]
    )
    return UpsellResult(
        booking=await _admin_view(db, outcome.booking), extended=outcome.extended, added_minutes=outcome.added_minutes,
        conflicts=[ClashAlert(**c.__dict__) for c in outcome.clashes],
    )


# ------------------------------------------------------------------ automotive / professional: parts on the job order
def _parts_out(lines: list[parts_service.PartLine]) -> PartsOut:
    return PartsOut(
        parts=[TicketPartOut(**line.__dict__) for line in lines], needs_ordering=any(line.needs_ordering for line in lines),
        total=sum((line.unit_price * line.quantity for line in lines), Decimal(0)),
    )


@router.get("/bookings/{booking_id}/parts", response_model=PartsOut)
async def get_job_parts(booking_id: uuid.UUID, principal: Staff, db: DbDep, tenant: TenantDep) -> PartsOut:
    """The parts on a job order, with what the shelf holds and which ones still have to be ordered."""
    await _own_job_only(principal, db, booking_id)
    return _parts_out(await parts_service.get_parts(db, tenant, booking_id))


@router.put("/bookings/{booking_id}/parts", response_model=PartsOut)
async def put_job_parts(booking_id: uuid.UUID, body: PartsIn, principal: Staff, db: DbDep, tenant: TenantDep) -> PartsOut:
    """Set the parts on a job order (the list sent replaces the list there). Allowed while the job is confirmed, running or waiting for parts.

    Stock is only checked, not taken: a part the shelf cannot cover is flagged ``needs_ordering`` (mark the job ``waiting-for-parts``), and the
    stock is deducted when the job is completed.
    """
    await _own_job_only(principal, db, booking_id)
    return _parts_out(await parts_service.set_parts(db, tenant, booking_id, [(p.product_id, p.quantity) for p in body.parts]))


# ------------------------------------------------------------------ customer notifications
@router.get("/notification-placeholders")
async def notification_placeholders(_: Manager) -> dict[str, str]:
    """What a template may refer to (``$name`` or ``${name}``) and what each one stands for."""
    return notifications_service.PLACEHOLDERS


@router.get("/notification-templates", response_model=list[TemplateOut])
async def list_notification_templates(_: Manager, db: DbDep) -> list[TemplateOut]:
    """What customers receive for each event and channel: the business's own wording, else the built-in email default."""
    custom = {(t.event, t.channel): t for t in (await db.scalars(select(NotificationTemplate))).all()}
    out: list[TemplateOut] = []
    for event in notifications_service.DEFAULTS:
        for channel in CHANNELS:
            row = custom.get((event, channel))
            if row is not None:
                out.append(TemplateOut(event=event, channel=channel, subject=row.subject, body=row.body, active=row.active,
                                       external_template=row.external_template, source="custom"))
            elif channel in notifications_service.DEFAULT_CHANNELS:
                subject, body = notifications_service.DEFAULTS[event]
                out.append(TemplateOut(event=event, channel=channel, subject=subject, body=body, active=True,
                                       external_template=None, source="default"))
    return out


@router.put("/notification-templates/{event}/{channel}", response_model=TemplateOut)
async def put_notification_template(event: NotifiableEvent, channel: Channel, body: TemplateIn, _: Manager, db: DbDep, tenant: TenantDep) -> TemplateOut:
    """Write the wording for one event on one channel (switching that channel on), or ``active: false`` to switch it off."""
    if channel == "email" and not body.subject:
        raise ValidationFailed("An email needs a subject")
    if channel != "email" and body.subject:
        raise ValidationFailed("Only emails have a subject")
    if channel != "whatsapp" and body.external_template:
        raise ValidationFailed("external_template applies to WhatsApp only")
    row = await db.scalar(select(NotificationTemplate).where(NotificationTemplate.event == event, NotificationTemplate.channel == channel))
    if row is None:
        row = NotificationTemplate(tenant_id=tenant.id, event=event, channel=channel, body=body.body)
        db.add(row)
    row.subject, row.body, row.active, row.external_template = body.subject, body.body, body.active, body.external_template
    await db.flush()
    return TemplateOut(event=event, channel=channel, subject=row.subject, body=row.body, active=row.active,
                       external_template=row.external_template, source="custom")


@router.delete("/notification-templates/{event}/{channel}", status_code=204)
async def delete_notification_template(event: NotifiableEvent, channel: Channel, _: Manager, db: DbDep) -> Response:
    """Go back to the built-in wording for this event and channel (none for SMS / WhatsApp: that switches it off)."""
    result = await db.execute(delete(NotificationTemplate).where(NotificationTemplate.event == event, NotificationTemplate.channel == channel))
    if not result.rowcount:  # type: ignore[attr-defined]
        raise NotFound("No custom template for that event and channel")
    return Response(status_code=204)


@router.get("/notifications", response_model=list[NotificationLogOut])
async def list_notifications(
    _: Manager, db: DbDep, booking_id: uuid.UUID | None = None, status: Literal["sending", "sent", "failed", "skipped", "refused"] | None = None,
    event: str | None = None, limit: int = 100, offset: int = 0,
) -> list[NotificationLog]:
    """What was sent to customers, newest first. Shows a masked recipient and the outcome, never the message text."""
    q = select(NotificationLog).order_by(NotificationLog.created_at.desc(), NotificationLog.id).limit(max(1, min(limit, 500))).offset(max(0, offset))
    if booking_id:
        q = q.where(NotificationLog.booking_id == booking_id)
    if status:
        q = q.where(NotificationLog.status == status)
    if event:
        q = q.where(NotificationLog.event == event)
    return list((await db.scalars(q)).all())


# ------------------------------------------------------------------ reminder lead times
def _reminder_view(tenant: Tenant) -> ReminderSettingsOut:
    default = sorted(set(get_settings().reminder_hours), reverse=True)
    custom = tenant.reminder_hours
    return ReminderSettingsOut(
        hours=default if custom is None else sorted(custom, reverse=True), source="default" if custom is None else "custom", default_hours=default,
    )


@router.get("/reminder-settings", response_model=ReminderSettingsOut)
async def get_reminder_settings(_: Manager, db: DbDep, tenant: TenantDep) -> ReminderSettingsOut:
    """How long before a confirmed booking customers are reminded (the reminder wording is a notification template: ``booking.reminder``)."""
    return _reminder_view(await _own_tenant(db, tenant))


@router.put("/reminder-settings", response_model=ReminderSettingsOut)
async def put_reminder_settings(body: ReminderSettingsIn, _: Manager, db: DbDep, tenant: TenantDep) -> ReminderSettingsOut:
    """Choose this business's reminder lead times, in hours (``[]`` = no reminders). Bookings already reminded are not reminded again for a lead time they got."""
    row = await _own_tenant(db, tenant)
    row.reminder_hours = body.hours
    await db.flush()
    return _reminder_view(row)


@router.delete("/reminder-settings", response_model=ReminderSettingsOut)
async def reset_reminder_settings(_: Manager, db: DbDep, tenant: TenantDep) -> ReminderSettingsOut:
    """Go back to the server's default lead times."""
    row = await _own_tenant(db, tenant)
    row.reminder_hours = None
    await db.flush()
    return _reminder_view(row)


async def _own_tenant(db: DbDep, tenant: Tenant) -> Tenant:
    row = await db.get(Tenant, tenant.id, populate_existing=True)
    if row is None:  # pragma: no cover - the tenant was resolved a moment ago
        raise NotFound("Tenant not found")
    return row
