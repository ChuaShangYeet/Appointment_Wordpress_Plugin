"""A customer's saved addresses and vehicles (``/public/me/addresses``, ``/public/me/vehicles``).

Both belong to one customer and are only ever reached through that customer's own token: someone else's row is a plain
404. Deleting one that an *open* booking still uses is refused, and so is *changing* it (409): the booking was accepted for that place and that car, so moving the
address outside the service radius, or swapping the vehicle, after booking would get round the checks made when booking. On finished bookings the reference is cleared
(the same rule PDPA erasure follows), so the customer's wish to delete is honoured without breaking a live appointment.
"""
from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.models import Booking, Customer, CustomerAddress, SupportedVehicle, Tenant, Vehicle
from app.services import geocoding
from app.services.locations import clean_postcode, country_of
from app.schemas.public import AddressIn, AddressUpdate, ProfileUpdate, VehicleIn, VehicleUpdate
from app.services.automotive import normalise_plate, supported_model, year_allowed

#: bookings that still hold a place in the calendar: the address / vehicle on them cannot be removed
OPEN_STATUSES = ("pending", "confirmed", "in-progress", "waiting-for-parts")
#: a ceiling so an account cannot be used to store unbounded rows
MAX_SAVED = 20


# --------------------------------------------------------------------------- addresses
async def list_addresses(db: AsyncSession, customer_id: uuid.UUID) -> list[CustomerAddress]:
    q = select(CustomerAddress).where(CustomerAddress.customer_id == customer_id).order_by(CustomerAddress.created_at, CustomerAddress.id)
    return list((await db.scalars(q)).all())


async def _own_address(db: AsyncSession, customer_id: uuid.UUID, address_id: uuid.UUID) -> CustomerAddress:
    address = await db.get(CustomerAddress, address_id)
    if address is None or address.customer_id != customer_id:
        raise NotFound("Address not found")
    return address


async def _count(db: AsyncSession, model: type[CustomerAddress] | type[Vehicle], customer_id: uuid.UUID) -> int:
    return await db.scalar(select(func.count()).select_from(model).where(model.customer_id == customer_id)) or 0


async def add_address(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID, data: AddressIn) -> CustomerAddress:
    if await _count(db, CustomerAddress, customer_id) >= MAX_SAVED:
        raise Conflict(f"At most {MAX_SAVED} addresses can be saved; remove one first")
    postcode = clean_postcode(tenant, data.postcode)
    latitude, longitude = data.latitude, data.longitude
    if latitude is None or longitude is None:  # no pin: look the typed address up (and refuse one that is too vague to place)
        found = await geocoding.locate_exact(geocoding.address_text(data.line1, data.city, postcode), country_of(tenant).region)
        latitude, longitude = found.latitude, found.longitude
    address = CustomerAddress(
        tenant_id=tenant.id, customer_id=customer_id, line1=data.line1, city=data.city, postcode=postcode,
        latitude=latitude, longitude=longitude,
    )
    db.add(address)
    await db.flush()
    return address


async def update_address(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID, address_id: uuid.UUID, data: AddressUpdate) -> CustomerAddress:
    address = await _own_address(db, customer_id, address_id)
    changes = data.model_dump(exclude_unset=True)
    if "postcode" in changes:
        changes["postcode"] = clean_postcode(tenant, changes["postcode"])
    if any(value != getattr(address, key) for key, value in changes.items()):  # (sending what is already saved changes nothing)
        await _refuse_change_while_used(db, Booking.address_id, address.id, "address")
    moved = any(key in changes and changes[key] != getattr(address, key) for key in ("line1", "city", "postcode"))
    if moved and "latitude" not in changes and geocoding.get_provider() is not None:
        # the words changed and no new pin came with them: the old coordinates would now point at the wrong place, so look the new address up
        found = await geocoding.locate_exact(geocoding.address_text(
            changes.get("line1", address.line1), changes["city"] if "city" in changes else address.city,
            changes["postcode"] if "postcode" in changes else address.postcode,
        ), country_of(tenant).region)
        changes["latitude"], changes["longitude"] = found.latitude, found.longitude
    for field, value in changes.items():
        setattr(address, field, value)
    await db.flush()
    return address


async def delete_address(db: AsyncSession, customer_id: uuid.UUID, address_id: uuid.UUID) -> None:
    address = await _own_address(db, customer_id, address_id)
    await _release_references(db, Booking.address_id, address.id, "address")
    await db.delete(address)
    await db.flush()


async def _refuse_change_while_used(db: AsyncSession, column: InstrumentedAttribute[uuid.UUID | None], target_id: uuid.UUID, what: Literal["address", "vehicle"]) -> None:
    """409 while an open booking uses the row: it was accepted for this place or this car."""
    open_count = await db.scalar(
        select(func.count()).select_from(Booking).where(column == target_id, Booking.status.in_(OPEN_STATUSES))
    ) or 0
    if open_count:
        raise Conflict(
            f"This {what} is used by {open_count} upcoming booking(s); cancel or finish them first, or save a new {what} instead of changing this one",
            extra={"open_bookings": open_count},
        )


async def _release_references(db: AsyncSession, column: InstrumentedAttribute[uuid.UUID | None], target_id: uuid.UUID, what: Literal["address", "vehicle"]) -> None:
    """Refuse while an open booking uses the row; otherwise forget it on the finished ones."""
    await _refuse_change_while_used(db, column, target_id, what)
    await db.execute(update(Booking).where(column == target_id).values({column.key: None}))


# --------------------------------------------------------------------------- vehicles
def _require_workshop(tenant: Tenant) -> None:
    if tenant.business_type != "automotive":
        raise ValidationFailed("Vehicles only apply to automotive businesses")


async def list_vehicles(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID) -> list[tuple[Vehicle, bool]]:
    """``(vehicle, serviceable)``: whether the workshop still services it (its model may have left the supported list)."""
    _require_workshop(tenant)
    vehicles = list((await db.scalars(select(Vehicle).where(Vehicle.customer_id == customer_id).order_by(Vehicle.created_at, Vehicle.id))).all())
    supported = {s.id: s for s in (await db.scalars(select(SupportedVehicle))).all()}
    return [(v, v.supported_vehicle_id in supported and year_allowed(supported[v.supported_vehicle_id], v.year)) for v in vehicles]


async def _own_vehicle(db: AsyncSession, customer_id: uuid.UUID, vehicle_id: uuid.UUID) -> Vehicle:
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None or vehicle.customer_id != customer_id:
        raise NotFound("Vehicle not found")
    return vehicle


async def add_vehicle(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID, data: VehicleIn) -> Vehicle:
    _require_workshop(tenant)
    model = await supported_model(db, data.supported_vehicle_id, data.year)
    plate = normalise_plate(data.plate_no)
    if not plate:
        raise ValidationFailed("That is not a valid plate number")
    if await _count(db, Vehicle, customer_id) >= MAX_SAVED:
        raise Conflict(f"At most {MAX_SAVED} vehicles can be saved; remove one first")
    vehicle = Vehicle(
        tenant_id=tenant.id, customer_id=customer_id, plate_no=plate, supported_vehicle_id=model.id,
        make=model.make, model=model.model, year=data.year,
    )
    await _save_unique_plate(db, vehicle)
    return vehicle


async def _save_unique_plate(db: AsyncSession, vehicle: Vehicle) -> None:
    try:
        async with db.begin_nested():
            db.add(vehicle)
            await db.flush()
    except IntegrityError as exc:
        raise Conflict("A vehicle with that plate number is already saved") from exc


async def update_vehicle(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID, vehicle_id: uuid.UUID, data: VehicleUpdate) -> Vehicle:
    _require_workshop(tenant)
    vehicle = await _own_vehicle(db, customer_id, vehicle_id)
    changes = data.model_dump(exclude_unset=True)
    supported_id = changes.get("supported_vehicle_id", vehicle.supported_vehicle_id)
    year = changes["year"] if "year" in changes else vehicle.year
    if supported_id is None:
        raise ValidationFailed("Choose one of the models this workshop services")
    model = await supported_model(db, supported_id, year)  # a change of year or model must still be one the workshop accepts
    new_plate = normalise_plate(changes["plate_no"]) if changes.get("plate_no") is not None else vehicle.plate_no
    if (model.id, year, new_plate) != (vehicle.supported_vehicle_id, vehicle.year, vehicle.plate_no):  # (a job order was accepted for this car)
        await _refuse_change_while_used(db, Booking.vehicle_id, vehicle.id, "vehicle")
    vehicle.supported_vehicle_id, vehicle.make, vehicle.model, vehicle.year = model.id, model.make, model.model, year
    if changes.get("plate_no") is not None:
        plate = normalise_plate(changes["plate_no"])
        if not plate:
            raise ValidationFailed("That is not a valid plate number")
        vehicle.plate_no = plate
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise Conflict("A vehicle with that plate number is already saved") from exc
    return vehicle


async def delete_vehicle(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID, vehicle_id: uuid.UUID) -> None:
    _require_workshop(tenant)
    vehicle = await _own_vehicle(db, customer_id, vehicle_id)
    await _release_references(db, Booking.vehicle_id, vehicle.id, "vehicle")
    await db.delete(vehicle)
    await db.flush()


# --------------------------------------------------------------------------- the customer's own record
#: Businesses that collect a customer's gender (blueprint §8-9, "Customer Extensions: customer_gender").
GENDER_BUSINESS_TYPES = frozenset({"spa", "salon"})


def ensure_gender_allowed(tenant: Tenant, gender: str | None) -> None:
    """Saying one's gender only makes sense where the business asks for it; ``None`` (nothing said, or removing it) is always fine."""
    if gender is not None and tenant.business_type not in GENDER_BUSINESS_TYPES:
        raise ValidationFailed("Gender is only collected by spa and salon businesses")


async def get_profile(db: AsyncSession, customer_id: uuid.UUID) -> Customer:
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise NotFound("Customer not found")
    return customer


async def update_profile(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID, data: ProfileUpdate) -> Customer:
    """Change the fields sent (today only ``gender``); ``null`` removes it."""
    customer = await get_profile(db, customer_id)
    if "gender" in data.model_fields_set:
        ensure_gender_allowed(tenant, data.gender)
        customer.gender = data.gender
    await db.flush()
    return customer
