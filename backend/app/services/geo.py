"""Service-radius check for on-site services, done in PostGIS (blueprint §11.3).

Distances are geodesic (``geography``, WGS-84), so a radius of 25 km means 25 km on the ground rather
than in degrees. Coordinates are stored as plain latitude/longitude columns; PostGIS is used only to
measure them, which keeps the schema free of geometry columns.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import OutsideServiceArea, PostgisUnavailable, ValidationFailed
from app.models import Tenant

UNDEFINED_FUNCTION = "42883"

_MEASURE = text(
    """
    SELECT ST_Distance(a, b) / 1000.0 AS km, ST_DWithin(a, b, CAST(:radius_m AS float8)) AS within
    FROM (
        SELECT ST_SetSRID(ST_MakePoint(CAST(:base_lon AS float8), CAST(:base_lat AS float8)), 4326)::geography AS a,
               ST_SetSRID(ST_MakePoint(CAST(:lon AS float8), CAST(:lat AS float8)), 4326)::geography AS b
    ) AS points
    """
)


def _sqlstate(exc: BaseException) -> str | None:
    seen: BaseException | None = exc
    while seen is not None:
        code = getattr(seen, "sqlstate", None) or getattr(seen, "pgcode", None)
        if code:
            return str(code)
        seen = seen.__cause__ or getattr(seen, "orig", None)
    return None


async def measure_km(
    db: AsyncSession, base: tuple[float, float], point: tuple[float, float], radius_km: float
) -> tuple[float, bool]:
    """Return ``(distance in km, within radius)`` between two ``(lat, lon)`` points using PostGIS."""
    try:
        async with db.begin_nested():  # a missing extension must not poison the caller's transaction
            row = (await db.execute(_MEASURE, {
                "base_lat": base[0], "base_lon": base[1], "lat": point[0], "lon": point[1], "radius_m": radius_km * 1000.0,
            })).one()
    except DBAPIError as exc:
        if _sqlstate(exc) == UNDEFINED_FUNCTION:
            raise PostgisUnavailable("On-site radius checks need the PostGIS extension, which is not enabled") from exc
        raise
    return float(row.km), bool(row.within)


async def ensure_within_service_area(db: AsyncSession, tenant: Tenant, latitude: float, longitude: float) -> float:
    """Raise unless ``(latitude, longitude)`` is inside the tenant's service radius; return the distance in km."""
    if tenant.latitude is None or tenant.longitude is None or tenant.service_radius_km is None:
        raise ValidationFailed("This business has not configured its location and service radius for on-site visits")
    km, within = await measure_km(db, (tenant.latitude, tenant.longitude), (latitude, longitude), tenant.service_radius_km)
    if not within:
        raise OutsideServiceArea(
            f"That address is {km:.1f} km away; this business serves up to {tenant.service_radius_km:g} km",
            extra={"distance_km": round(km, 2), "service_radius_km": tenant.service_radius_km},
        )
    return km
