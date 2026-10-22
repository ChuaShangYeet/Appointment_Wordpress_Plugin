"""Customer addresses for on-site appointments."""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import countries
from app.core.countries import AddressProblem, Country
from app.core.exceptions import NotFound, ValidationFailed
from app.models import CustomerAddress, Tenant
from app.schemas.public import BookingCreate
from app.services import geocoding


def country_of(tenant: Tenant) -> Country:
    return countries.get(tenant.country)


def clean_postcode(tenant: Tenant, raw: str | None) -> str | None:
    """A customer's postcode in the shape the business's country uses (``app.core.countries.normalise_postcode``); nothing typed stays nothing."""
    if raw is None or not raw.strip():
        return None
    try:
        return countries.normalise_postcode(country_of(tenant), raw)
    except AddressProblem as exc:
        raise ValidationFailed(str(exc)) from exc


async def resolve_address(
    db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID, data: BookingCreate
) -> tuple[uuid.UUID, float, float]:
    """Return ``(address_id, latitude, longitude)`` for an on-site booking.

    Exactly one of ``address_id`` (a saved address of this customer) or ``address`` (created now) must be
    given. A new address carries the coordinates of the customer's map pin; without them it is looked up
    (:mod:`app.services.geocoding`), and only a precise result is accepted.
    """
    if (data.address_id is None) == (data.address is None):
        raise ValidationFailed("Provide exactly one of address_id or address for an on-site service")

    if data.address_id is not None:
        saved = await db.get(CustomerAddress, data.address_id)
        if saved is None or saved.customer_id != customer_id:
            raise NotFound("Address not found")
        if saved.latitude is None or saved.longitude is None:
            raise ValidationFailed("That saved address has no map coordinates; drop a pin to continue")
        return saved.id, saved.latitude, saved.longitude

    assert data.address is not None
    postcode = clean_postcode(tenant, data.address.postcode)
    latitude, longitude = data.address.latitude, data.address.longitude
    if latitude is None or longitude is None:
        found = await geocoding.locate_exact(geocoding.address_text(data.address.line1, data.address.city, postcode), country_of(tenant).region)
        latitude, longitude = found.latitude, found.longitude
    created = CustomerAddress(
        tenant_id=tenant.id, customer_id=customer_id, line1=data.address.line1, city=data.address.city,
        postcode=postcode, latitude=latitude, longitude=longitude,
    )
    db.add(created)
    await db.flush()
    return created.id, latitude, longitude
