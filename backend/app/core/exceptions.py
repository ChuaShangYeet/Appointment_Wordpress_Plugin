"""Domain exceptions and their HTTP mapping."""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class; carries an HTTP status and machine-readable code."""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, extra: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra or {}
        #: Extra HTTP response headers (``Retry-After`` and the like).
        self.headers = headers or {}


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_failed"


class DisposableEmail(ValidationFailed):
    """The email address belongs to a throw-away mailbox service, which sign-up does not accept."""

    code = "disposable_email"


class InvalidPhone(ValidationFailed):
    """A customer's number is not a mobile number of the chosen country; the message says what one looks like."""

    code = "invalid_phone"


class CalendarNotConfigured(DomainError):
    """The server has no Google OAuth client, so no calendar can be connected (``GOOGLE_OAUTH_CLIENT_ID`` and friends)."""

    status_code = 503
    code = "calendar_not_configured"


class CaptchaRequired(DomainError):
    """The request needs the bot check's token (``X-Captcha-Token``) and has none."""

    status_code = 403
    code = "captcha_required"


class CaptchaFailed(DomainError):
    """The bot check's token was refused (wrong, expired, already used, or made for another form): the visitor must complete the check again."""

    status_code = 403
    code = "captcha_failed"


class CaptchaUnavailable(DomainError):
    """The bot check could not be made (Cloudflare unreachable): nobody is let through unchecked."""

    status_code = 503
    code = "captcha_unavailable"


class SignupDisabled(DomainError):
    """Self-service sign-up is not switched on on this server (``SELF_SERVICE_SIGNUP``)."""

    status_code = 403
    code = "signup_disabled"


class PlanLimitReached(DomainError):
    """The business's plan does not allow one more of this (``limit``, ``allowed``, ``used`` and ``plan`` are in the answer)."""

    status_code = 402
    code = "plan_limit_reached"


class OutsideBookingWindow(ValidationFailed):
    code = "outside_booking_window"


class MinimumSpendNotAccepted(ValidationFailed):
    """VIP booking submitted without accepting the minimum-spend agreement."""

    code = "minimum_spend_not_accepted"


class IncompatibleVehicle(ValidationFailed):
    """The vehicle is not on the workshop's supported list (make/model/year)."""

    code = "incompatible_vehicle"


class LeadTimeNotAcknowledged(ValidationFailed):
    """The booking needs on-order parts and the customer has not accepted the lead-time disclaimer."""

    code = "lead_time_not_acknowledged"


class TooLateToCancel(ValidationFailed):
    """The booking's cancellation notice has passed."""

    code = "too_late_to_cancel"


class TooLateToReschedule(ValidationFailed):
    """The booking's change notice has passed (rescheduling follows the cancellation notice)."""

    code = "too_late_to_reschedule"


class TooManyRequests(DomainError):
    status_code = 429
    code = "too_many_requests"


class Conflict(DomainError):
    """The request is fine but the current state does not allow it (already done, already open)."""

    status_code = 409
    code = "conflict"


class IdempotencyKeyReused(Conflict):
    """An ``Idempotency-Key`` that already made a booking was sent again with a different request."""

    code = "idempotency_key_reused"


class ErasureBlocked(Conflict):
    """A customer cannot be erased while they still have an upcoming or running booking."""

    code = "erasure_blocked"


class InsufficientStock(DomainError):
    """Not enough of a retail product left to put on the ticket."""

    status_code = 409
    code = "insufficient_stock"


class OutsideServiceArea(ValidationFailed):
    """On-site address is farther from the business than its service radius."""

    code = "outside_service_area"


class AddressNotFound(ValidationFailed):
    """Nothing matches the address the customer typed."""

    code = "address_not_found"


class AddressNotPrecise(ValidationFailed):
    """The address only matched an area (or part of it): too vague to measure a service radius with."""

    code = "address_not_precise"


class GeocodingUnavailable(ValidationFailed):
    """No geocoding provider is configured, so a typed address cannot be turned into coordinates."""

    code = "geocoding_unavailable"


class GeocodingFailed(DomainError):
    """The geocoding provider could not answer (down, over quota, refused, unreadable)."""

    status_code = 503
    code = "geocoding_failed"


class MapsLinkInvalid(ValidationFailed):
    """What was pasted as the business's Google Maps link is not one (only Google Maps links over HTTPS are opened)."""

    code = "maps_link_invalid"


class MapsLinkUnreadable(ValidationFailed):
    """A Google Maps link, but no map point (and no place name to look up) could be read from it."""

    code = "maps_link_unreadable"


class MapsLinkFailed(DomainError):
    """Google could not be reached to follow a share link (``maps.app.goo.gl``)."""

    status_code = 503
    code = "maps_link_failed"


class PostgisUnavailable(DomainError):
    """The distance check needs the PostGIS extension, which this database does not have."""

    status_code = 503
    code = "postgis_unavailable"


class SlotUnavailable(DomainError):
    """Requested slot is not offered (not a race; e.g. outside shift)."""

    status_code = 409
    code = "slot_unavailable"


class SlotConflict(DomainError):
    """The exclusion constraint rejected the reservation (lost a race)."""

    status_code = 409
    code = "slot_conflict"


class InvalidTransition(DomainError):
    status_code = 409
    code = "invalid_transition"


class Unauthorized(DomainError):
    status_code = 401
    code = "unauthorized"


class Forbidden(DomainError):
    status_code = 403
    code = "forbidden"
