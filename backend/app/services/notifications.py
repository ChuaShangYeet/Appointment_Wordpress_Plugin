"""Telling customers what happens to their bookings, and delivering sign-in codes (blueprint §15).

* **Booking messages** are driven by the outbox: :class:`NotificationPublisher` is the outbox's event publisher. For each
  booking event it works out which channels are switched on (the business's own templates, else the built-in email
  default), renders the text for that booking and hands it to the channel's provider (:mod:`app.services.delivery`).
  Delivery is *at least once* (a crash between the provider accepting a message and the log being written sends it again), but
  the log's unique ``(event, channel)`` row means a re-delivered event never re-sends what already went out, and a failed channel is
  retried on its own with the outbox's back-off until ``NOTIFY_MAX_ATTEMPTS``, after which it stays ``failed`` in the log.
* **Sign-in messages** (OTP code, magic link) are sent directly, not through the outbox: the secret must never be stored in an
  event. They are logged like everything else, without the secret.
* **Templates** use ``$name`` / ``${name}`` placeholders from a fixed list; unknown ones are refused when saving, text a customer
  controls (their name) has control characters stripped before it is substituted, and nothing is ever evaluated.
* **Staff alerts**: a schedule collision (``booking.schedule_conflict``) is turned into a message to the staff member concerned, see ``_alert_staff``.
* **Privacy**: the log keeps a masked recipient and no message text; erasing a customer deletes their log rows.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass
from string import Template
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.exceptions import DomainError, ValidationFailed
from app.core.security import issue_review_token
from app.models import Booking, BookingAllocation, BookingItem, BusinessProfile, Customer, Resource, Service, Tenant
from app.models.calendar import CalendarConnection
from app.services import plans
from app.models.business import Review
from app.models.events import OutboxEvent
from app.models.webhooks import WebhookEndpoint
from app.services.events import SCHEDULE_CONFLICT_EVENT
from app.services.calendar import CONNECTION_LOST_EVENT
from app.services.plans import LABELS, LIMIT_WARNING_EVENT
from app.services.webhooks import ENDPOINT_DISABLED_EVENT
from app.models.notifications import CHANNELS, NOTIFIABLE_EVENTS, NotificationLog, NotificationTemplate
from app.services.delivery import Message, PermanentDeliveryError, deliver, mask_recipient

log = logging.getLogger(__name__)

#: What a template may refer to.
PLACEHOLDERS: dict[str, str] = {
    "business_name": "The business's name",
    "customer_name": "The customer's name",
    "services": "The booked services, comma separated",
    "date": "Date of the appointment, e.g. Sat 04 Oct 2026",
    "time": "Start time, e.g. 10:00",
    "end_time": "End time",
    "party_size": "Restaurant: number of guests",
    "hours_before": "Reminders: how many hours before the appointment this message goes out",
    "previous_date": "Rescheduled: the date it used to be",
    "previous_time": "Rescheduled: the time it used to be",
    "review_link": "Completed: the link to the review page for this visit (empty when the business has no review page set)",
    "review_prompt": "Completed: a ready-made sentence inviting the customer to rate the visit, with the link when there is one",
}

#: Built-in wording, used on the email channel unless the business wrote its own (or switched the event off).
DEFAULT_CHANNELS = ("email",)
DEFAULTS: dict[str, tuple[str, str]] = {
    "booking.created": (
        "We received your booking request",
        "Hi $customer_name,\n\nWe received your request for $services on $date at $time and will confirm it shortly.\n\n$business_name",
    ),
    "booking.confirmed": (
        "Your booking is confirmed",
        "Hi $customer_name,\n\nYour booking for $services on $date at $time is confirmed.\n\nSee you then,\n$business_name",
    ),
    "booking.rejected": (
        "We could not confirm your booking",
        "Hi $customer_name,\n\nSorry, we could not confirm your booking for $services on $date at $time. "
        "Please book another time.\n\n$business_name",
    ),
    "booking.cancelled": (
        "Your booking was cancelled",
        "Hi $customer_name,\n\nYour booking for $services on $date at $time has been cancelled.\n\n$business_name",
    ),
    "booking.rescheduled": (
        "Your booking was moved",
        "Hi $customer_name,\n\nYour booking for $services has moved from $previous_date at $previous_time "
        "to $date at $time.\n\n$business_name",
    ),
    "booking.waiting_for_parts": (
        "We are waiting for parts for your vehicle",
        "Hi $customer_name,\n\nWe are waiting for parts for $services. We will contact you to book the remaining work.\n\n$business_name",
    ),
    "booking.reminder": (
        "Reminder: your booking on $date",
        "Hi $customer_name,\n\nA reminder that your booking for $services is on $date at $time.\n\n$business_name",
    ),
    "booking.completed": (
        "Thank you for visiting us",
        "Hi $customer_name,\n\nThank you for choosing $business_name. We hope $services went well.$review_prompt\n\n$business_name",
    ),
}
assert set(DEFAULTS) == set(NOTIFIABLE_EVENTS)

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
#: log statuses that end an event's business on a channel: a re-delivery of the event does not touch it again
FINAL = ("sent", "skipped", "refused")


class NotificationFailed(Exception):
    """At least one channel could not be delivered; the outbox retries the event."""


class DeliveryFailed(DomainError):
    """A sign-in message could not be sent, so the sign-in cannot continue."""

    status_code = 503
    code = "delivery_failed"


# --------------------------------------------------------------------------- templates
def placeholders_in(text: str) -> set[str]:
    """Names used in ``text``; raises ``ValidationFailed`` for a stray ``$`` (write ``$$`` for a literal dollar sign)."""
    names: set[str] = set()
    for match in Template.pattern.finditer(text):
        if match.group("invalid") is not None:
            raise ValidationFailed("A '$' must start a placeholder such as $date, or be written '$$' for a literal dollar sign")
        name = match.group("named") or match.group("braced")
        if name:
            names.add(name)
    return names


def validate_template(subject: str | None, body: str) -> None:
    unknown = (placeholders_in(body) | (placeholders_in(subject) if subject else set())) - set(PLACEHOLDERS)
    if unknown:
        raise ValidationFailed(
            f"Unknown placeholder(s): {', '.join(sorted(unknown))}", extra={"allowed": sorted(PLACEHOLDERS)}
        )


def render(text: str, context: dict[str, str]) -> str:
    return Template(text).safe_substitute(context)


def clean(value: object) -> str:
    """A value that came from a person, safe to put into one line of a message or header."""
    return _CONTROL.sub(" ", str(value)).strip()


@dataclass(frozen=True)
class Effective:
    channel: str
    subject: str | None
    body: str
    external_template: str | None
    custom: bool


async def effective_templates(db: AsyncSession, event_type: str) -> list[Effective]:
    """The channels an event goes out on: the business's active templates, plus the default email unless that is overridden or off."""
    rows = {
        t.channel: t for t in (await db.scalars(select(NotificationTemplate).where(NotificationTemplate.event == event_type))).all()
    }
    result: list[Effective] = []
    for channel in CHANNELS:
        row = rows.get(channel)
        if row is not None:
            if row.active:
                result.append(Effective(channel, row.subject, row.body, row.external_template, True))
        elif channel in DEFAULT_CHANNELS:
            subject, body = DEFAULTS[event_type]
            result.append(Effective(channel, subject, body, None, False))
    return result


# --------------------------------------------------------------------------- booking messages
async def business_name(db: AsyncSession, tenant: Tenant) -> str:
    return await db.scalar(select(BusinessProfile.business_name)) or tenant.name


async def review_link_for(db: AsyncSession, tenant: Tenant, booking: Booking, customer: Customer) -> str:
    """The link to the business's review page for this visit, with a signed token for this booking; ``""`` when there is nowhere to send them
    (no review page set, in the business profile or in ``REVIEW_URL``), when the customer was erased, or when the visit has been reviewed already."""
    if customer.erased_at is not None:
        return ""
    page = await db.scalar(select(BusinessProfile.review_url)) or get_settings().review_url
    if not page or await db.scalar(select(Review.id).where(Review.booking_id == booking.id)) is not None:
        return ""
    return page.replace("{token}", issue_review_token(tenant_id=tenant.id, booking_id=booking.id))


async def booking_context(
    db: AsyncSession, tenant: Tenant, booking: Booking, customer: Customer, payload: dict[str, Any], event_type: str = "",
) -> dict[str, str]:
    tz = ZoneInfo(tenant.timezone)
    start, end = booking.start_at.astimezone(tz), booking.end_at.astimezone(tz)
    names = (await db.scalars(
        select(Service.name).join(BookingItem, BookingItem.service_id == Service.id)
        .where(BookingItem.booking_id == booking.id).order_by(BookingItem.seq)
    )).all()
    previous = payload.get("previous_start_at")
    before = dt.datetime.fromisoformat(previous).astimezone(tz) if isinstance(previous, str) else None
    completed = event_type == "booking.completed"
    review_link = await review_link_for(db, tenant, booking, customer) if completed else ""
    if review_link:
        review_prompt = f" How was your visit? Rate it here: {review_link}"
    else:
        review_prompt = " You can rate your visit when you sign in." if completed else ""
    return {
        "business_name": clean(await business_name(db, tenant)), "customer_name": clean(customer.name),
        "services": clean(", ".join(names)), "date": start.strftime("%a %d %b %Y"), "time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"), "party_size": str(booking.party_size or ""),
        "previous_date": before.strftime("%a %d %b %Y") if before else "", "previous_time": before.strftime("%H:%M") if before else "",
        "hours_before": str(payload.get("hours_before", "")), "review_link": review_link, "review_prompt": review_prompt,
    }


def _recipient(channel: str, customer: Customer, booking: Booking) -> str | None:
    """Where a message about ``booking`` goes: the email given with that booking, else the customer's email if they confirmed it (an unconfirmed one may be anyone's), or the phone."""
    if channel != "email":
        return customer.phone
    return booking.contact_email or (customer.email if customer.email_verified_at is not None else None)


def _fail_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _stale_reason(event: OutboxEvent, booking: Booking, now: dt.datetime) -> str | None:
    """Why a *reminder* should no longer go out: the world moved on between queueing it and sending it."""
    if event.event_type != "booking.reminder":
        return None
    if booking.status != "confirmed":
        return f"booking is {booking.status}"
    queued_for = event.payload.get("start_at")
    if not isinstance(queued_for, str) or dt.datetime.fromisoformat(queued_for) != booking.start_at:
        return "booking was moved after the reminder was scheduled"
    if booking.start_at <= now:
        return "appointment already started"
    return None


async def _notify(event: OutboxEvent) -> list[str]:
    """Send what is due for ``event`` in its own transaction; returns the failures (the log is committed either way)."""
    failures: list[str] = []
    async with tenant_session(event.tenant_id) as db:
        tenant = await db.get(Tenant, event.tenant_id)
        booking = await db.get(Booking, event.aggregate_id)
        if tenant is None or booking is None:
            return []
        if event.event_type == "booking.created" and event.payload.get("status") != "pending":
            return []  # a booking that starts out confirmed is announced by booking.confirmed instead
        customer = await db.get(Customer, booking.customer_id)
        if customer is None:
            return []
        stale = _stale_reason(event, booking, _utcnow())
        context = await booking_context(db, tenant, booking, customer, event.payload, event.event_type)
        logged = {
            row.channel: row for row in (await db.scalars(select(NotificationLog).where(NotificationLog.outbox_event_id == event.id))).all()
        }
        now = _utcnow()
        for tpl in await effective_templates(db, event.event_type):
            row = logged.get(tpl.channel)
            if row is not None and row.status in FINAL:
                continue  # this event already reached (or was ruled out for) this channel: a re-delivery sends nothing twice
            if row is None:
                row = NotificationLog(
                    tenant_id=tenant.id, event=event.event_type, channel=tpl.channel, outbox_event_id=event.id,
                    booking_id=booking.id, customer_id=customer.id, status="failed", attempts=0,
                )
                db.add(row)
            recipient = _recipient(tpl.channel, customer, booking)
            row.attempts += 1
            if stale is not None:
                row.status, row.error = "skipped", stale
                continue
            if customer.erased_at is not None or not recipient:
                row.status, row.error = "skipped", "customer erased" if customer.erased_at is not None else f"no {'email address' if tpl.channel == 'email' else 'phone number'} on file"
                continue
            if not await plans.message_allowed(db, tenant, tpl.channel, now):
                row.status, row.error = "skipped", f"plan limit: this month's {tpl.channel} messages are used up"
                continue
            row.recipient = mask_recipient(tpl.channel, recipient)
            message = Message(
                channel=tpl.channel, recipient=recipient, body=render(tpl.body, context),
                subject=render(tpl.subject, context) if tpl.subject else None, external_template=tpl.external_template,
                # a WhatsApp template is filled in order; a completed visit's template may take the review link as its fifth variable
                params=tuple(context[k] for k in ("customer_name", "services", "date", "time")) + ((context["review_link"],) if context["review_link"] else ()),
            )
            try:
                row.provider, row.provider_message_id = await deliver(message)
            except PermanentDeliveryError as exc:  # the provider will never take this one: say so and move on (the event is not held back and this channel is not tried again)
                row.status, row.error = "refused", _fail_text(exc)
            except Exception as exc:  # noqa: BLE001 - any provider failure means "try this channel again later"
                row.status, row.error = "failed", _fail_text(exc)
                failures.append(f"{tpl.channel}: {row.error}")
            else:
                row.status, row.error, row.sent_at = "sent", None, now
        await db.flush()
    return failures


def _webhook_host(url: str) -> str:
    """Where a webhook points, without its path or query: those can carry secrets and do not belong in an email."""
    parts = urlsplit(url)
    return parts.netloc.rpartition("@")[2] or "(unknown)"


async def _alert_owner(event: OutboxEvent) -> list[str]:
    """Tell the business that the system switched one of its webhooks off. Returns the failures (the log is committed either way)."""
    async with tenant_session(event.tenant_id) as db:
        tenant = await db.get(Tenant, event.tenant_id)
        endpoint = await db.get(WebhookEndpoint, event.aggregate_id)
        if tenant is None or endpoint is None:
            return []  # the webhook was deleted in the meantime: nothing left to warn about
        logged = await db.scalar(select(NotificationLog).where(NotificationLog.outbox_event_id == event.id, NotificationLog.channel == "email"))
        if logged is not None and logged.status in FINAL:
            return []
        row = logged or NotificationLog(tenant_id=tenant.id, event=event.event_type, channel="email", outbox_event_id=event.id, status="failed", attempts=0)
        if logged is None:
            db.add(row)
        row.attempts += 1
        if endpoint.active:
            row.status, row.error = "skipped", "the webhook was switched on again"
            return []
        profile = await db.scalar(select(BusinessProfile))
        address = profile.contact_email if profile else None
        if not address:
            row.status, row.error = "skipped", "no business contact email on file"
            return []
        problem = event.payload.get("last_error")
        name = clean(await business_name(db, tenant))
        failures_in_a_row = event.payload.get("consecutive_failures", endpoint.consecutive_failures)
        body = (
            f"Hello,\n\nThe webhook to {_webhook_host(endpoint.url)} for {name} was switched off because it failed {failures_in_a_row} times in a row"
            f"{f' (most recent problem: {clean(problem)})' if problem else ''}.\n\n"
            "New bookings and changes are no longer being sent to it. Once the receiving server is working again, switch the webhook back on in the admin "
            f"(PATCH /v1/admin/webhooks/{endpoint.id} with {{\"active\": true}}); deliveries that were waiting were marked failed and can be redelivered from there.\n\n{name}"
        )
        row.recipient = mask_recipient("email", address)
        try:
            row.provider, row.provider_message_id = await deliver(Message(channel="email", recipient=address, body=body, subject=f"A webhook for {name} was switched off"))
        except PermanentDeliveryError as exc:  # never going to be accepted: not retried
            row.status, row.error = "refused", _fail_text(exc)
            return []
        except Exception as exc:  # noqa: BLE001
            row.status, row.error = "failed", _fail_text(exc)
            return [f"email: {row.error}"]
        row.status, row.error, row.sent_at = "sent", None, _utcnow()
        return []


async def _alert_plan(event: OutboxEvent) -> list[str]:
    """Tell the business that it is near, or at, one of its plan's limits. Returns the failures (the log is committed either way)."""
    async with tenant_session(event.tenant_id) as db:
        tenant = await db.get(Tenant, event.tenant_id)
        if tenant is None:
            return []
        logged = await db.scalar(select(NotificationLog).where(NotificationLog.outbox_event_id == event.id, NotificationLog.channel == "email"))
        if logged is not None and logged.status in FINAL:
            return []
        row = logged or NotificationLog(tenant_id=tenant.id, event=event.event_type, channel="email", outbox_event_id=event.id, status="failed", attempts=0)
        if logged is None:
            db.add(row)
        row.attempts += 1
        profile = await db.scalar(select(BusinessProfile))
        address = profile.contact_email if profile else None
        if not address:
            row.status, row.error = "skipped", "no business contact email on file"
            return []
        payload = event.payload
        what = LABELS.get("max_" + str(payload.get("limit")), str(payload.get("limit")))
        used, allowed = payload.get("used"), payload.get("allowed")
        monthly = str(payload.get("limit")).endswith("_per_month")
        name = clean(await business_name(db, tenant))
        plan_name = (await plans.plan_of(db, tenant)).name
        if payload.get("level") == "reached":
            headline = f"{name} has reached its limit of {allowed} {what} on the {plan_name} plan."
            consequence = ("Nothing more of this kind can be added until next month" if monthly else "Nothing more of this kind can be added until something is removed") + ", unless the plan is changed."
            subject = f"{name}: you have reached your limit of {what}"
        else:
            headline = f"{name} has used {used} of its {allowed} {what}{' this month' if monthly else ''} on the {plan_name} plan."
            consequence = "When the limit is reached nothing more of this kind can be added" + (" until next month" if monthly else " until something is removed") + ", unless the plan is changed."
            subject = f"{name}: you are close to your limit of {what}"
        body = f"Hello,\n\n{headline}\n\n{consequence} To lift the limit, ask to move to a bigger plan.\n\n{name}"
        row.recipient = mask_recipient("email", address)
        try:
            row.provider, row.provider_message_id = await deliver(Message(channel="email", recipient=address, body=body, subject=subject))
        except PermanentDeliveryError as exc:  # never going to be accepted: not retried
            row.status, row.error = "refused", _fail_text(exc)
            return []
        except Exception as exc:  # noqa: BLE001
            row.status, row.error = "failed", _fail_text(exc)
            return [f"email: {row.error}"]
        row.status, row.error, row.sent_at = "sent", None, _utcnow()
        return []


async def _alert_calendar(event: OutboxEvent) -> list[str]:
    """Tell the business that a Google calendar stopped syncing and must be connected again. Returns the failures (the log is committed either way)."""
    async with tenant_session(event.tenant_id) as db:
        tenant = await db.get(Tenant, event.tenant_id)
        connection = await db.get(CalendarConnection, event.aggregate_id)
        if tenant is None or connection is None:
            return []  # disconnected in the meantime: nothing left to warn about
        logged = await db.scalar(select(NotificationLog).where(NotificationLog.outbox_event_id == event.id, NotificationLog.channel == "email"))
        if logged is not None and logged.status in FINAL:
            return []
        row = logged or NotificationLog(tenant_id=tenant.id, event=event.event_type, channel="email", outbox_event_id=event.id, status="failed", attempts=0)
        if logged is None:
            db.add(row)
        row.attempts += 1
        if connection.status == "active":
            row.status, row.error = "skipped", "the calendar was connected again"
            return []
        profile = await db.scalar(select(BusinessProfile))
        address = profile.contact_email if profile else None
        if not address:
            row.status, row.error = "skipped", "no business contact email on file"
            return []
        name = clean(await business_name(db, tenant))
        whose = f"{clean(await db.scalar(select(Resource.name).where(Resource.id == connection.resource_id)) or 'a staff member')}'s" if connection.resource_id else "the business's"
        body = (
            f"Hello,\n\nBookings are no longer being copied to {whose} Google Calendar for {name}: {clean(event.payload.get('reason', 'Google stopped accepting the connection'))}.\n\n"
            "New and changed bookings are not reaching that calendar until it is connected again. Open the calendar settings in the admin and connect it again "
            "(POST /v1/admin/calendar/connect); the bookings that belong in it are then written to it.\n\n" + name
        )
        row.recipient = mask_recipient("email", address)
        try:
            row.provider, row.provider_message_id = await deliver(Message(channel="email", recipient=address, body=body, subject=f"Your Google Calendar for {name} stopped syncing"))
        except PermanentDeliveryError as exc:  # never going to be accepted: not retried
            row.status, row.error = "refused", _fail_text(exc)
            return []
        except Exception as exc:  # noqa: BLE001
            row.status, row.error = "failed", _fail_text(exc)
            return [f"email: {row.error}"]
        row.status, row.error, row.sent_at = "sent", None, _utcnow()
        return []


def _span(start: dt.datetime, end: dt.datetime, tz: ZoneInfo) -> str:
    start, end = start.astimezone(tz), end.astimezone(tz)
    return f"{start.strftime('%a %d %b')} {start.strftime('%H:%M')}-{end.strftime('%H:%M')}"


async def _alert_staff(event: OutboxEvent) -> list[str]:
    """Tell the staff member whose appointment ran into their next one (an add-on or an overrun that does not fit). Returns the failures.

    The message goes to the contact details a manager saved on the staff member (email, and SMS to the phone) and says what happened and what the
    next appointment is - the customer's first name, the services and the times - because that is what the person needs to act. Nothing was moved:
    both appointments are flagged for a human. Nothing is sent once the flag has been cleared. A staff member with no contact details gets one
    ``skipped`` row in the log saying so, so the manager can see why nobody was told. One staff member per event; the log keeps no message text.
    """
    failures: list[str] = []
    async with tenant_session(event.tenant_id) as db:
        tenant = await db.get(Tenant, event.tenant_id)
        booking = await db.get(Booking, event.aggregate_id)
        if tenant is None or booking is None:
            return []
        logged = {row.channel: row for row in (await db.scalars(select(NotificationLog).where(NotificationLog.outbox_event_id == event.id))).all()}
        staff = await db.scalar(
            select(Resource).join(BookingAllocation, BookingAllocation.resource_id == Resource.id)
            .where(BookingAllocation.booking_id == booking.id, Resource.type == "staff").order_by(func.lower(BookingAllocation.period)).limit(1)
        )
        targets = [(channel, value) for channel, value in (("email", staff.contact_email if staff else None), ("sms", staff.contact_phone if staff else None)) if value]
        skip_reason: str | None = None
        if not booking.schedule_conflict:
            skip_reason = "the conflict was already resolved"
        elif staff is None:
            skip_reason = "no staff member on the booking"
        elif not targets:
            skip_reason = "no contact details saved for the staff member"
        if skip_reason is not None:
            targets = [("email", "")]
        tz = ZoneInfo(tenant.timezone)
        name = clean(await business_name(db, tenant))
        wanted = event.payload.get("wanted_end_at")
        clash_ids = [uuid.UUID(i) for i in event.payload.get("conflicting_booking_ids", [])]
        lines: list[str] = []
        if skip_reason is None and clash_ids:
            for other, customer in (await db.execute(
                select(Booking, Customer.name).join(Customer, Customer.id == Booking.customer_id).where(Booking.id.in_(clash_ids)).order_by(Booking.start_at)
            )).all():
                names = (await db.scalars(
                    select(Service.name).join(BookingItem, BookingItem.service_id == Service.id).where(BookingItem.booking_id == other.id).order_by(BookingItem.seq)
                )).all()
                first = (customer.split() or [""])[0]
                lines.append(f"- {_span(other.start_at, other.end_at, tz)}: {clean(', '.join(names))}" + (f" for {clean(first)}" if first else ""))
        running = ", ".join(clean(n) for n in (await db.scalars(
            select(Service.name).join(BookingItem, BookingItem.service_id == Service.id).where(BookingItem.booking_id == booking.id).order_by(BookingItem.seq)
        )).all())
        until = f" until {dt.datetime.fromisoformat(wanted).astimezone(tz).strftime('%H:%M')}" if isinstance(wanted, str) else " longer"
        why = "an extra service was added" if event.payload.get("cause") == "upsell" else "the visit is running over"
        body = (
            f"{name}: your {_span(booking.start_at, booking.end_at, tz)} appointment ({running}) needs to go on{until} because {why}, "
            "and that runs into your next appointment:\n" + "\n".join(lines) +
            "\nNothing was moved. Please speak to your manager; both appointments are marked for review."
        )
        for channel, recipient in targets:
            row = logged.get(channel)
            if row is not None and row.status in FINAL:
                continue
            if row is None:
                row = NotificationLog(tenant_id=tenant.id, event=event.event_type, channel=channel, outbox_event_id=event.id, booking_id=booking.id, status="failed", attempts=0)
                db.add(row)
            row.attempts += 1
            if skip_reason is not None:
                row.status, row.error = "skipped", skip_reason
                continue
            if not await plans.message_allowed(db, tenant, channel):
                row.status, row.error = "skipped", f"plan limit: this month's {channel} messages are used up"
                continue
            row.recipient = mask_recipient(channel, recipient)
            try:
                row.provider, row.provider_message_id = await deliver(Message(
                    channel=channel, recipient=recipient, body=body, subject=f"{name}: your next appointment is in the way" if channel == "email" else None,
                ))
            except PermanentDeliveryError as exc:  # never going to be accepted: not retried
                row.status, row.error = "refused", _fail_text(exc)
            except Exception as exc:  # noqa: BLE001
                row.status, row.error = "failed", _fail_text(exc)
                failures.append(f"{channel}: {row.error}")
            else:
                row.status, row.error, row.sent_at = "sent", None, _utcnow()
        await db.flush()
    return failures


class NotificationPublisher:
    """The outbox's event publisher: turns booking events into customer messages, schedule collisions into a message to the staff member, and
    webhook problems into a message to the business."""

    async def publish(self, event: OutboxEvent) -> None:
        if event.event_type == ENDPOINT_DISABLED_EVENT:
            failures = await _alert_owner(event)
        elif event.event_type == CONNECTION_LOST_EVENT:
            failures = await _alert_calendar(event)
        elif event.event_type == LIMIT_WARNING_EVENT:
            failures = await _alert_plan(event)
        elif event.event_type == SCHEDULE_CONFLICT_EVENT:
            failures = await _alert_staff(event)
        elif event.event_type in NOTIFIABLE_EVENTS:
            failures = await _notify(event)
        else:
            return
        if not failures:
            return
        if event.attempts + 1 >= get_settings().notify_max_attempts:
            log.error("giving up notifying for event %s (%s): %s", event.id, event.event_type, "; ".join(failures))
            return  # the log rows stay 'failed'; the event is not retried again
        raise NotificationFailed("; ".join(failures))


# --------------------------------------------------------------------------- sign-in messages
async def _record_direct(
    tenant_id: uuid.UUID, event: str, channel: str, recipient: str, customer_id: uuid.UUID | None,
    provider: str | None, message_id: str | None, error: str | None,
) -> None:
    """Log a sign-in message in its own transaction, so a failed attempt is kept even though the request is rolled back."""
    async with tenant_session(tenant_id) as db:
        db.add(NotificationLog(
            tenant_id=tenant_id, event=event, channel=channel, customer_id=customer_id, recipient=mask_recipient(channel, recipient),
            status="failed" if error else "sent", provider=provider, provider_message_id=message_id, error=error, attempts=1,
            sent_at=None if error else _utcnow(),
        ))


async def _reserve_code(tenant: Tenant, channel: str, recipient: str) -> uuid.UUID:
    """Take one of the plan's messages for a sign-in code *before* it goes to the gateway, and say so in the log (status ``sending``).

    Committed at once, so the next sender (the worker, or another customer's code) counts it too. The business's message lock is held only for this short transaction,
    never across the call to the gateway. Raises :class:`PlanLimitReached` - and logs nothing - when the allowance is used up.
    """
    async with tenant_session(tenant.id) as db:
        await plans.ensure_can_send(db, tenant, channel)
        row = NotificationLog(
            tenant_id=tenant.id, event="auth.otp", channel=channel, recipient=mask_recipient(channel, recipient), status="sending", attempts=1, sent_at=_utcnow(),
        )
        db.add(row)
        await db.flush()
        return row.id


async def _settle_direct(tenant_id: uuid.UUID, row_id: uuid.UUID, provider: str | None, message_id: str | None, error: str | None) -> None:
    """Say how a reserved sign-in code ended, in a transaction of its own (a failure is kept even though the request is rolled back). A failure gives the place back."""
    async with tenant_session(tenant_id) as db:
        row = await db.get(NotificationLog, row_id)
        if row is None:
            return
        row.provider, row.provider_message_id = provider, message_id
        if error:
            row.status, row.error, row.sent_at = "failed", error, None
        else:
            row.status, row.error = "sent", None


async def _send_direct(
    tenant: Tenant, event: str, channel: str, recipient: str, subject: str | None, body: str, customer_id: uuid.UUID | None,
    *, external_template: str | None = None, params: tuple[str, ...] = (), button_param: str | None = None, reservation: uuid.UUID | None = None,
) -> None:
    """Hand a sign-in message to the gateway and log it: as a new row, or - when its place was reserved first (``reservation``) - by settling that row."""
    provider: str | None = None
    try:
        provider, message_id = await deliver(Message(
            channel=channel, recipient=recipient, body=body, subject=subject, external_template=external_template, params=params, button_param=button_param,
        ))
    except Exception as exc:  # noqa: BLE001
        if reservation is None:
            await _record_direct(tenant.id, event, channel, recipient, customer_id, provider, None, _fail_text(exc))
        else:
            await _settle_direct(tenant.id, reservation, provider, None, _fail_text(exc))
        raise DeliveryFailed("We could not send the message; please try again shortly") from exc
    if reservation is None:
        await _record_direct(tenant.id, event, channel, recipient, customer_id, provider, message_id, None)
    else:
        await _settle_direct(tenant.id, reservation, provider, message_id, None)


async def send_email_confirmation(tenant: Tenant, customer_id: uuid.UUID, email: str, token: str) -> None:
    """Email the link that confirms ``email`` as a customer's own. Raises :class:`DeliveryFailed` if it could not be sent."""
    settings = get_settings()
    link = settings.email_confirm_url.replace("{token}", token) if settings.email_confirm_url else f"Your confirmation token: {token}"
    minutes = max(1, settings.magic_link_ttl_seconds // 60)
    name = await _name(tenant)
    body = (f"Use this link to confirm this email address for your {name} account: {link}\n\nIt works once and expires in {minutes} minutes. "
            "If you did not ask for it, ignore this email and nothing changes.")
    await _send_direct(tenant, "auth.email_confirm", "email", email, f"Confirm your email for {name}", body, customer_id)


async def _name(tenant: Tenant) -> str:
    async with tenant_session(tenant.id) as db:
        return clean(await business_name(db, tenant))


async def send_login_code(tenant: Tenant, phone: str, code: str) -> None:
    """Deliver an OTP code over ``OTP_CHANNEL``. Raises :class:`DeliveryFailed` if it could not be sent."""
    settings = get_settings()
    minutes = max(1, settings.otp_ttl_seconds // 60)
    body = f"{await _name(tenant)}: your sign-in code is {code}. It expires in {minutes} minutes. Do not share it with anyone."
    # a code costs a message: refuse (402) once the plan's are used up, so the customer signs in by email link instead - and take the place now, before the gateway is called
    reservation = await _reserve_code(tenant, settings.otp_channel, phone)
    if settings.otp_channel == "whatsapp" and settings.whatsapp_otp_template:
        # WhatsApp only lets a business open a conversation with an approved template: the code is its one variable (and its copy-code button)
        await _send_direct(
            tenant, "auth.otp", "whatsapp", phone, None, body, None, external_template=settings.whatsapp_otp_template, params=(code,),
            button_param=code if settings.whatsapp_otp_button else None, reservation=reservation,
        )
        return
    await _send_direct(tenant, "auth.otp", settings.otp_channel, phone, None, body, None, reservation=reservation)


async def send_magic_link(tenant: Tenant, customer_id: uuid.UUID, email: str, token: str) -> None:
    """Email a sign-in link. Raises :class:`DeliveryFailed` if it could not be sent."""
    settings = get_settings()
    link = settings.magic_link_url.replace("{token}", token) if settings.magic_link_url else f"Your sign-in token: {token}"
    minutes = max(1, settings.magic_link_ttl_seconds // 60)
    name = await _name(tenant)
    body = f"Use this link to sign in to {name}: {link}\n\nIt works once and expires in {minutes} minutes. If you did not ask for it, ignore this email."
    await _send_direct(tenant, "auth.magic_link", "email", email, f"Your sign-in link for {name}", body, customer_id)
