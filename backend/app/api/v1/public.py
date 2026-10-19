"""Public headless endpoints, authenticated by the publishable ``X-Site-Key``."""
from __future__ import annotations

import calendar
import datetime as dt
import logging
import uuid
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import availability_cache, countries
from app.api.deps import DbDep, Principal, get_tenant, require_captcha, require_customer, require_full_customer, resolve_tenant
from app.core.exceptions import PostgisUnavailable, ValidationFailed
from app.models import (
    Booking,
    BookingItem,
    BusinessProfile,
    Customer,
    CustomerAddress,
    DataRequest,
    Product,
    Resource,
    Review,
    Service,
    SupportedVehicle,
    Tenant,
    Vehicle,
)
from app.schemas.business import (
    BusinessLocationPublic,
    BusinessProfileOut,
    BusinessPublicOut,
    ReviewByInvitation,
    ReviewCreate,
    ReviewInvitationIn,
    ReviewInvitationOut,
    ReviewPublicOut,
    ReviewSummary,
)
from app.schemas.catalog import ProductPublicOut, ServiceOut, SupportedVehicleOut
from app.schemas.public import (
    AvailabilityResponse,
    BookingConfirmation,
    BookingCreate,
    CustomerBookingOut,
    AddressIn,
    AddressUpdate,
    RescheduleRequest,
    SavedAddressOut,
    SavedVehicleOut,
    VehicleIn,
    VehicleUpdate,
    DaySummary,
    GeocodeIn,
    GeocodeOut,
    MonthAvailabilityResponse,
    PartNotice,
    ProfileOut,
    ProfileUpdate,
)
from app.schemas.compliance import DataRequestCreate, DataRequestPublicOut
from app.schemas.scheduling import ResourceOut
from app.services import booking as booking_service
from app.services import compliance, geocoding, geo
from app.services import customer_profile
from app.services import reviews as reviews_service
from app.services.allocators import get_allocator
from app.services.automotive import lead_time_notice
from app.services.availability_view import (
    DayQuery,
    Prepared,
    cache_payload,
    compute_day,
    nothing_bookable,
    prepare,
    read_cached,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/public", tags=["public"], dependencies=[Depends(resolve_tenant)])
TenantDep = Annotated[Tenant, Depends(get_tenant)]
#: Any signed-in customer, including one whose emailed link was for an unconfirmed address (``email_scope``): only that address's bookings are theirs.
CustomerDep = Annotated[Principal, Depends(require_customer)]
#: A customer who signed in as themselves (phone code, or a confirmed email): saved details, profile and data requests need this.
FullCustomerDep = Annotated[Principal, Depends(require_full_customer)]


@router.get("/services", response_model=list[ServiceOut])
async def list_services(db: DbDep) -> list[Service]:
    return list((await db.scalars(select(Service).where(Service.active.is_(True)).order_by(Service.name))).all())


@router.get("/products", response_model=list[ProductPublicOut])
async def list_products(db: DbDep) -> list[Product]:
    """Inventory catalog for the booking page (browse only: retail is added to the ticket in the salon)."""
    return list((await db.scalars(select(Product).order_by(Product.category, Product.name))).all())


@router.get("/supported-vehicles", response_model=list[SupportedVehicleOut])
async def list_supported_vehicles(db: DbDep) -> list[SupportedVehicle]:
    """Makes/models the workshop services; customers choose strictly from this list."""
    q = select(SupportedVehicle).order_by(SupportedVehicle.make, SupportedVehicle.model)
    return list((await db.scalars(q)).all())


@router.get("/resources", response_model=list[ResourceOut])
async def list_resources(db: DbDep, type: str = "staff") -> list[Resource]:
    q = select(Resource).where(Resource.status == "active", Resource.type == type).order_by(Resource.name)
    return list((await db.scalars(q)).all())


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _day_query(
    tenant: Tenant, service_ids: list[uuid.UUID], resource_id: uuid.UUID | None, party_size: int | None, table_type: str,
    allow_separate_seating: bool, latitude: float | None, longitude: float | None,
) -> DayQuery:
    return DayQuery(
        tenant=tenant, allocator=get_allocator(tenant.business_type), service_ids=service_ids, resource_id=resource_id,
        party_size=party_size, table_type=table_type, allow_separate_seating=allow_separate_seating,
        latitude=latitude, longitude=longitude,
    )


@router.get("/availability", response_model=AvailabilityResponse)
async def availability(
    db: DbDep,
    tenant: TenantDep,
    response: Response,
    date: dt.date,
    service_ids: Annotated[list[uuid.UUID], Query(default_factory=list)],
    resource_id: uuid.UUID | None = None,
    party_size: Annotated[int | None, Query(ge=1)] = None,
    table_type: Literal["normal", "vip"] = "normal",
    allow_separate_seating: bool = False,
    #: On-site services: where the visit would be (checked against the business's service radius).
    latitude: Annotated[float | None, Query(ge=-90, le=90)] = None,
    longitude: Annotated[float | None, Query(ge=-180, le=180)] = None,
) -> AvailabilityResponse:
    q = _day_query(tenant, service_ids, resource_id, party_size, table_type, allow_separate_seating, latitude, longitude)
    # Cached per tenant and request (blueprint §13.1).
    cached = await availability_cache.lookup(tenant.id, q.cache_params(date)) if q.cacheable else availability_cache.Lookup(None, None)
    hit = read_cached(cached.payload, _utcnow())
    if hit is not None:
        response.headers["X-Cache"] = "HIT"
        return hit
    response.headers["X-Cache"] = "MISS" if cached.key else "BYPASS"

    prep = await prepare(db, q)
    result = await compute_day(db, q, prep, date)
    await availability_cache.store(cached.key, cache_payload(prep.policy, result))
    return result


@router.get("/availability/month", response_model=MonthAvailabilityResponse)
async def availability_month(
    db: DbDep,
    tenant: TenantDep,
    response: Response,
    month: Annotated[str, Query(pattern=r"^(19[7-9]\d|2[01]\d\d)-(0[1-9]|1[0-2])$", description="YYYY-MM, in the business's timezone")],
    service_ids: Annotated[list[uuid.UUID], Query(default_factory=list)],
    resource_id: uuid.UUID | None = None,
    party_size: Annotated[int | None, Query(ge=1)] = None,
    table_type: Literal["normal", "vip"] = "normal",
    allow_separate_seating: bool = False,
    latitude: Annotated[float | None, Query(ge=-90, le=90)] = None,
    longitude: Annotated[float | None, Query(ge=-180, le=180)] = None,
) -> MonthAvailabilityResponse:
    """Per date of ``month``: is anything bookable, and how many slots. The same question as the day view asked for
    every date, answered from the same cache entries; dates that cannot hold a bookable slot (already past, or beyond
    the booking window) are not computed at all."""
    year, mon = (int(part) for part in month.split("-"))
    days = [dt.date(year, mon, d) for d in range(1, calendar.monthrange(year, mon)[1] + 1)]
    q = _day_query(tenant, service_ids, resource_id, party_size, table_type, allow_separate_seating, latitude, longitude)
    now = _utcnow()
    tz = ZoneInfo(tenant.timezone)

    if q.cacheable:
        lookups = await availability_cache.lookup_many(tenant.id, [q.cache_params(d) for d in days])
    else:
        lookups = [availability_cache.Lookup(None, None) for _ in days]
    answers: dict[dt.date, AvailabilityResponse] = {}
    missing: list[tuple[dt.date, availability_cache.Lookup]] = []
    for day, lk in zip(days, lookups, strict=True):
        hit = read_cached(lk.payload, now)
        if hit is not None:
            answers[day] = hit
        else:
            missing.append((day, lk))

    prep: Prepared | None = None
    computed = False
    if missing:
        prep = await prepare(db, q)  # also validates the services / policy, as the day view does
        for day, lk in missing:
            if nothing_bookable(day, tz, prep.policy, now):
                answers[day] = AvailabilityResponse(date=day, timezone=tenant.timezone, slots=[])
                continue
            answers[day] = await compute_day(db, q, prep, day, now)
            computed = True
            await availability_cache.store(lk.key, cache_payload(prep.policy, answers[day]))
    response.headers["X-Cache"] = "BYPASS" if lookups[0].key is None else ("MISS" if computed else "HIT")

    notice = next((a.lead_time_notice for a in answers.values() if a.lead_time_notice), [])
    if not notice and q.allocator.uses_vehicles and not any(a.slots for a in answers.values()):
        prep = prep or await prepare(db, q)  # a month with no bookable day still tells the customer which parts are on order
        notice = [PartNotice(**n) for n in await lead_time_notice(db, prep.services)]
    return MonthAvailabilityResponse(
        month=month, timezone=tenant.timezone,
        days=[DaySummary(date=d, available=bool(answers[d].slots), slot_count=len(answers[d].slots)) for d in days],
        lead_time_notice=notice,
    )


@router.post("/bookings", response_model=BookingConfirmation, status_code=201, dependencies=[Depends(require_captcha("booking"))])
async def create_booking(
    db: DbDep, tenant: TenantDep, body: BookingCreate, response: Response, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> BookingConfirmation:
    """Reserve a slot atomically. 409 (with refreshed ``slots``) if it was taken concurrently. When a bot check is configured (``GET /captcha``) the widget's
    token (``action`` = ``booking``) goes in the ``X-Captcha-Token`` header; a request made with a secret API key needs none.

    Send an ``Idempotency-Key`` header (8-100 characters; a fresh UUID per *submission*, kept for its retries) so that sending the same request twice - a double click, a retry after a
    time-out - makes one booking: the second call returns the first booking (same body and status, header ``Idempotent-Replayed: true``). The same key with a different request is
    409 ``idempotency_key_reused``; a request that failed made nothing, so its key may be tried again. Without the header every call makes its own booking."""
    if idempotency_key is None:
        booking: Booking = await booking_service.create_booking(db, tenant, body)
    else:
        booking, replayed = await booking_service.create_booking_once(db, tenant, body, booking_service.clean_idempotency_key(idempotency_key))
        if replayed:
            response.headers["Idempotent-Replayed"] = "true"
    return BookingConfirmation(
        id=booking.id, status=booking.status, start_at=booking.start_at, end_at=booking.end_at,
        hold_expires_at=booking.hold_expires_at, resource_ids=[a.resource_id for a in booking.allocations],
        minimum_spend_agreed=booking.minimum_spend_agreed, meeting_link=booking.meeting_link,
    )


# ------------------------------------------------------------------ the signed-in customer's own bookings
async def _customer_view(db: AsyncSession, bookings: list[Booking]) -> list[CustomerBookingOut]:
    now = dt.datetime.now(dt.UTC)
    names: dict[uuid.UUID, list[str]] = {}
    if bookings:
        rows = await db.execute(
            select(BookingItem.booking_id, Service.name)
            .join(Service, Service.id == BookingItem.service_id)
            .where(BookingItem.booking_id.in_([b.id for b in bookings]))
            .order_by(Service.name)
        )
        for booking_id, name in rows.all():
            names.setdefault(booking_id, []).append(name)
    return [
        CustomerBookingOut(
            id=b.id, status=b.status, start_at=b.start_at, end_at=b.end_at, service_names=names.get(b.id, []),
            party_size=b.party_size, meeting_link=b.meeting_link, can_cancel=booking_service.can_cancel_online(b, now),
            cancel_deadline=booking_service.cancel_deadline(b), can_reschedule=booking_service.can_reschedule_online(b, now),
        )
        for b in bookings
    ]


@router.get("/me/bookings", response_model=list[CustomerBookingOut])
async def my_bookings(db: DbDep, customer: CustomerDep, include_past: bool = False, limit: int = 50) -> list[CustomerBookingOut]:
    """The signed-in customer's bookings, soonest first (upcoming only unless ``include_past``)."""
    q = select(Booking).where(Booking.customer_id == uuid.UUID(customer.user_id)).order_by(Booking.start_at).limit(max(1, min(limit, 200)))
    if customer.email_scope is not None:
        q = q.where(func.lower(Booking.contact_email) == customer.email_scope)
    if not include_past:
        q = q.where(Booking.end_at >= dt.datetime.now(dt.UTC))
    return await _customer_view(db, list((await db.scalars(q)).unique().all()))


@router.get("/me/profile", response_model=ProfileOut)
async def my_profile(db: DbDep, customer: FullCustomerDep) -> Customer:
    return await customer_profile.get_profile(db, uuid.UUID(customer.user_id))


@router.patch("/me/profile", response_model=ProfileOut)
async def update_my_profile(body: ProfileUpdate, db: DbDep, tenant: TenantDep, customer: FullCustomerDep) -> Customer:
    """Change your own record. Today that is ``gender`` (spa and salon businesses only; ``null`` removes it)."""
    return await customer_profile.update_profile(db, tenant, uuid.UUID(customer.user_id), body)


@router.post("/me/data-requests", response_model=DataRequestPublicOut, status_code=201)
async def create_data_request(body: DataRequestCreate, db: DbDep, tenant: TenantDep, customer: FullCustomerDep) -> DataRequest:
    """Ask to see (``access``) or delete (``erasure``) your personal data (PDPA). The business's owner then handles it."""
    return await compliance.open_request(db, tenant, uuid.UUID(customer.user_id), body.kind)


@router.get("/me/data-requests", response_model=list[DataRequestPublicOut])
async def my_data_requests(db: DbDep, customer: FullCustomerDep) -> list[DataRequest]:
    q = select(DataRequest).where(DataRequest.customer_id == uuid.UUID(customer.user_id)).order_by(DataRequest.requested_at.desc())
    return list((await db.scalars(q)).all())


@router.post("/geocode", response_model=GeocodeOut)
async def geocode_address(body: GeocodeIn, db: DbDep, tenant: TenantDep) -> GeocodeOut:
    """Where is this typed address? For businesses that go to the customer: the page shows the result, tells the customer whether it is inside the
    service area, and carries the coordinates into the availability and booking calls (so the lookup is paid for once). A vague result (an area, a
    partial match) comes back with ``exact: false``; a booking refuses it. 422 when nothing matches or no provider is configured, 503 when the lookup fails.
    Limited tightly (the ``geocode`` rate-limit bucket): every call is a paid lookup.
    """
    if not get_allocator(tenant.business_type).uses_locations:
        raise ValidationFailed("This business does not go to the customer's address")
    found = await geocoding.locate(body.address, countries.get(tenant.country).region)
    within: bool | None = None
    distance: float | None = None
    if tenant.latitude is not None and tenant.longitude is not None and tenant.service_radius_km is not None:
        try:
            km, within = await geo.measure_km(db, (tenant.latitude, tenant.longitude), (found.latitude, found.longitude), tenant.service_radius_km)
            distance = round(km, 2)
        except PostgisUnavailable:
            within = None
    return GeocodeOut(
        latitude=found.latitude, longitude=found.longitude, formatted_address=found.formatted_address, exact=found.exact,
        within_service_area=within, distance_km=distance,
    )


# ------------------------------------------------------------------ the business page and reviews
@router.get("/business", response_model=BusinessPublicOut)
async def business(db: DbDep, tenant: TenantDep) -> BusinessPublicOut:
    """The business profile for the booking page (blueprint §14). Before one is saved: the name, type and timezone."""
    profile = await db.scalar(select(BusinessProfile))
    base = BusinessProfileOut.model_validate(profile, from_attributes=True) if profile else BusinessProfileOut(business_name=tenant.name)
    location = (
        BusinessLocationPublic(latitude=tenant.latitude, longitude=tenant.longitude, service_radius_km=tenant.service_radius_km)
        if tenant.latitude is not None and tenant.longitude is not None else None
    )
    return BusinessPublicOut(
        **base.model_dump(exclude={"review_url"}), business_type=tenant.business_type, timezone=tenant.timezone,
        country=tenant.country, currency=countries.get(tenant.country).currency, location=location,
    )


def _published(resource_id: uuid.UUID | None) -> Any:
    q = Review.status == "published"
    return q & (Review.resource_id == resource_id) if resource_id else q


@router.get("/reviews", response_model=list[ReviewPublicOut])
async def list_reviews(db: DbDep, resource_id: uuid.UUID | None = None, limit: int = 20, offset: int = 0) -> list[ReviewPublicOut]:
    """Published reviews, newest first; ``resource_id`` limits them to one staff member."""
    q = (
        select(Review, Customer.name, Customer.erased_at).join(Customer, Customer.id == Review.customer_id)
        .where(_published(resource_id)).order_by(Review.created_at.desc(), Review.id).limit(max(1, min(limit, 100))).offset(max(0, offset))
    )
    return [
        ReviewPublicOut(id=r.id, rating=r.rating, comment=r.comment, reviewer=reviews_service.first_name(name, erased is not None),
                        resource_id=r.resource_id, created_at=r.created_at)
        for r, name, erased in (await db.execute(q)).all()
    ]


@router.get("/reviews/summary", response_model=ReviewSummary)
async def reviews_summary(db: DbDep, resource_id: uuid.UUID | None = None) -> ReviewSummary:
    count, average = (await db.execute(select(func.count(Review.id), func.avg(Review.rating)).where(_published(resource_id)))).one()
    return ReviewSummary(count=count, average=round(float(average), 1) if average is not None else None)


@router.post("/bookings/{booking_id}/review", response_model=ReviewPublicOut, status_code=201)
async def review_booking(booking_id: uuid.UUID, body: ReviewCreate, db: DbDep, tenant: TenantDep, customer: CustomerDep) -> ReviewPublicOut:
    """Rate a completed appointment of your own, once."""
    review = await reviews_service.create_review(db, tenant, booking_id, uuid.UUID(customer.user_id), body.rating, body.comment, customer.email_scope)
    name = await db.scalar(select(Customer.name).where(Customer.id == review.customer_id)) or ""
    return ReviewPublicOut(id=review.id, rating=review.rating, comment=review.comment, reviewer=reviews_service.first_name(name, False),
                           resource_id=review.resource_id, created_at=review.created_at)


@router.post("/review-invitation", response_model=ReviewInvitationOut)
async def review_invitation(body: ReviewInvitationIn, db: DbDep, tenant: TenantDep) -> ReviewInvitationOut:
    """What the review page behind the link in the completed-visit message shows: the business, the services, the date, and whether it can still be reviewed.

    The token (from the link) goes in the body, not the URL, so it stays out of access logs. 401 for a forged, expired or other business's token.
    """
    return await reviews_service.invitation(db, tenant, body.token)


@router.post("/review-invitation/review", response_model=ReviewPublicOut, status_code=201)
async def review_by_invitation(body: ReviewByInvitation, db: DbDep, tenant: TenantDep) -> ReviewPublicOut:
    """Rate the visit the link was sent for, without signing in. Once per booking (409 after that); only a completed visit (409 otherwise)."""
    booking, customer = await reviews_service.invited_booking(db, tenant, body.token)
    review = await reviews_service.create_review(db, tenant, booking.id, booking.customer_id, body.rating, body.comment)
    return ReviewPublicOut(id=review.id, rating=review.rating, comment=review.comment, reviewer=reviews_service.first_name(customer.name, False),
                           resource_id=review.resource_id, created_at=review.created_at)


@router.post("/bookings/{booking_id}/cancel", response_model=CustomerBookingOut)
async def cancel_booking(booking_id: uuid.UUID, db: DbDep, tenant: TenantDep, customer: CustomerDep) -> CustomerBookingOut:
    """Cancel one of the customer's own bookings, subject to that booking's cancellation policy.

    A pending request can be withdrawn at any time; a confirmed booking only until its cancellation
    notice runs out (422 ``too_late_to_cancel`` with the deadline). Started or finished bookings need
    the business. The slot is released immediately.
    """
    booking = await booking_service.cancel_as_customer(db, tenant, booking_id, uuid.UUID(customer.user_id), email_scope=customer.email_scope)
    return (await _customer_view(db, [booking]))[0]


@router.post("/bookings/{booking_id}/reschedule", response_model=CustomerBookingOut)
async def reschedule_booking(booking_id: uuid.UUID, body: RescheduleRequest, db: DbDep, tenant: TenantDep, customer: CustomerDep) -> CustomerBookingOut:
    """Move one of the customer's own bookings to another time (same services), under that booking's own policy.

    Allowed while the booking could still be cancelled online (422 ``too_late_to_reschedule`` with the deadline); the new
    time must be one that could be booked now, otherwise 409 and nothing changes. See ``booking.reschedule_as_customer``.
    """
    booking = await booking_service.reschedule_as_customer(
        db, tenant, booking_id, uuid.UUID(customer.user_id), body.start_at,
        resource_id=body.resource_id, any_available=body.any_available, accept_minimum_spend=body.accept_minimum_spend,
        accepted_minimum_spend=body.accepted_minimum_spend, email_scope=customer.email_scope,
    )
    return (await _customer_view(db, [booking]))[0]


# ------------------------------------------------------------------ saved addresses and vehicles
@router.get("/me/addresses", response_model=list[SavedAddressOut])
async def my_addresses(db: DbDep, customer: FullCustomerDep) -> list[CustomerAddress]:
    return await customer_profile.list_addresses(db, uuid.UUID(customer.user_id))


@router.post("/me/addresses", response_model=SavedAddressOut, status_code=201)
async def add_address(body: AddressIn, db: DbDep, tenant: TenantDep, customer: FullCustomerDep) -> CustomerAddress:
    """Save an address (with the map pin's coordinates) to pick again when booking an on-site visit."""
    return await customer_profile.add_address(db, tenant, uuid.UUID(customer.user_id), body)


@router.patch("/me/addresses/{address_id}", response_model=SavedAddressOut)
async def update_address(address_id: uuid.UUID, body: AddressUpdate, db: DbDep, tenant: TenantDep, customer: FullCustomerDep) -> CustomerAddress:
    return await customer_profile.update_address(db, tenant, uuid.UUID(customer.user_id), address_id, body)


@router.delete("/me/addresses/{address_id}", status_code=204)
async def delete_address(address_id: uuid.UUID, db: DbDep, customer: FullCustomerDep) -> Response:
    """Remove a saved address; refused (409) while an upcoming booking still uses it."""
    await customer_profile.delete_address(db, uuid.UUID(customer.user_id), address_id)
    return Response(status_code=204)


@router.get("/me/vehicles", response_model=list[SavedVehicleOut])
async def my_vehicles(db: DbDep, tenant: TenantDep, customer: FullCustomerDep) -> list[SavedVehicleOut]:
    rows = await customer_profile.list_vehicles(db, tenant, uuid.UUID(customer.user_id))
    return [_vehicle_out(v, ok) for v, ok in rows]


def _vehicle_out(v: Vehicle, serviceable: bool) -> SavedVehicleOut:
    return SavedVehicleOut(id=v.id, plate_no=v.plate_no, make=v.make, model=v.model, year=v.year,
                           supported_vehicle_id=v.supported_vehicle_id, serviceable=serviceable)


@router.post("/me/vehicles", response_model=SavedVehicleOut, status_code=201)
async def add_vehicle(body: VehicleIn, db: DbDep, tenant: TenantDep, customer: FullCustomerDep) -> SavedVehicleOut:
    """Save a vehicle (workshops only). It must be a make/model the workshop services (422 ``incompatible_vehicle``)."""
    return _vehicle_out(await customer_profile.add_vehicle(db, tenant, uuid.UUID(customer.user_id), body), True)


@router.patch("/me/vehicles/{vehicle_id}", response_model=SavedVehicleOut)
async def update_vehicle(vehicle_id: uuid.UUID, body: VehicleUpdate, db: DbDep, tenant: TenantDep, customer: FullCustomerDep) -> SavedVehicleOut:
    return _vehicle_out(await customer_profile.update_vehicle(db, tenant, uuid.UUID(customer.user_id), vehicle_id, body), True)


@router.delete("/me/vehicles/{vehicle_id}", status_code=204)
async def delete_vehicle(vehicle_id: uuid.UUID, db: DbDep, tenant: TenantDep, customer: FullCustomerDep) -> Response:
    """Remove a saved vehicle; refused (409) while an upcoming job order still uses it."""
    await customer_profile.delete_vehicle(db, tenant, uuid.UUID(customer.user_id), vehicle_id)
    return Response(status_code=204)
