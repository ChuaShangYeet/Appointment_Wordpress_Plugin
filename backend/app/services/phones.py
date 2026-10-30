"""A customer's mobile number as given to a business: the country it is typed in, and the check it must pass.

The customer picks the country code in front of the number (``phone_country``); left out, it is the business's country.
A number new to the business must be a mobile of that country (``app.core.phone.mobile_number``). A number already on
file for the business is taken as it is: customers saved before numbers were checked per country keep signing in and
booking with the number they always used.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import countries
from app.core.exceptions import ValidationFailed
from app.core.phone import mobile_number, normalise_phone
from app.models import Customer, Tenant


def region(tenant: Tenant, phone_country: str | None) -> str:
    """The country a number is read in: the one chosen in front of it, else the business's own."""
    if phone_country is None:
        return tenant.country
    if phone_country not in countries.COUNTRIES:
        raise ValidationFailed(f"{phone_country} is not a country code we know")
    return phone_country


def spelled(raw: str, where: str) -> str:
    """``raw`` the standard way without the strict check, for finding a customer already on file."""
    try:
        return mobile_number(raw, where)
    except ValidationFailed:
        return normalise_phone(raw, countries.COUNTRIES[where].calling_code)


async def customer_phone(db: AsyncSession, tenant: Tenant, raw: str, phone_country: str | None) -> str:
    """The number to book or sign in with: a mobile of the chosen country, or a number this business already has."""
    where = region(tenant, phone_country)
    try:
        return mobile_number(raw, where)
    except ValidationFailed:
        try:
            known = normalise_phone(raw, countries.COUNTRIES[where].calling_code)
        except ValidationFailed:
            known = None
        if known is not None and await db.scalar(
            select(Customer.id).where(Customer.tenant_id == tenant.id, Customer.phone == known)
        ) is not None:
            return known
        raise
