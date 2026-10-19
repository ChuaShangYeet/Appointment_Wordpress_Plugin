"""A customer's email: typing it into a booking proves nothing, so it does not sign in as the customer until the phone's owner confirms it.

The hole this closes: a public booking made with somebody else's phone number and the attacker's own email used to replace the customer's email, and the
emailed sign-in link then signed the attacker in as that customer (bookings, saved addresses and vehicles, data requests) and redirected their messages.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.security import decode_jwt
from app.models import Booking, Customer, Tenant
from app.services import booking as booking_module
from app.services import compliance
from tests.conftest import Recorder, Seed, at, make_seed
from tests.test_customer_cancel import PHONE, cancel, login, otp  # noqa: F401
from tests.test_customer_identity import ask, booked_with_email, customers_of, links, redeem  # noqa: F401
from tests.test_notifications import log_rows, put_template, run_outbox  # noqa: F401

VICTIM = "victim@example.com"
ATTACKER = "attacker@example.com"
OTHER_PHONE = "+60199990001"


async def email_state(s: Seed, phone: str = PHONE) -> tuple[str | None, dt.datetime | None]:
    async with tenant_session(s.tenant_id) as db:
        row = (await db.execute(text("SELECT email, email_verified_at FROM customers WHERE phone = :p"), {"p": phone})).one()
        return row[0], row[1]


async def contact_emails(s: Seed) -> dict[str, str | None]:
    async with tenant_session(s.tenant_id) as db:
        return {str(i): e for i, e in (await db.execute(text("SELECT id, contact_email FROM bookings"))).all()}


def bearer(s: Seed, token: str) -> dict[str, str]:
    return {**s.pk, "Authorization": f"Bearer {token}"}


async def link_session(s: Seed, links: list[str], email: str, **extra: Any) -> dict[str, str]:
    """Ask for the emailed sign-in link for ``email`` and follow it."""
    before = len(links)
    assert (await ask(s, email, **extra)).status_code == 202
    assert len(links) == before + 1, "no link was issued"
    r = await redeem(s, links[-1])
    assert r.status_code == 200, r.text
    return bearer(s, r.json()["access_token"])


async def confirm_email(s: Seed, links: list[str], phone: str, email: str) -> None:
    """The customer signs in with their phone and confirms ``email`` through the emailed link."""
    me = await login(s, phone)
    r = await s.client.post("/v1/public/auth/email", json={"email": email}, headers=me)
    assert r.status_code == 202, r.text
    done = await s.client.post("/v1/public/auth/email/confirm", json={"token": links[-1]}, headers=s.pk)
    assert done.status_code == 200 and done.json() == {"status": "confirmed"}, done.text


# --------------------------------------------------------------------- the attack, step by step
async def test_a_booking_with_somebody_elses_phone_does_not_replace_their_email(seeded: Seed) -> None:
    await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    await booked_with_email(seeded, PHONE, ATTACKER, hour=12)
    assert await customers_of(seeded) == [(PHONE, VICTIM)]


async def test_each_booking_keeps_the_email_it_was_made_with(seeded: Seed) -> None:
    a = await booked_with_email(seeded, PHONE, "Mixed.Case@Example.COM", hour=10)
    b = await booked_with_email(seeded, PHONE, ATTACKER, hour=12)
    mei = await seeded.add_staff("Mei")
    c = (await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, 14), mei, customer={"name": "A", "phone": PHONE}))).json()["id"]
    assert await contact_emails(seeded) == {a: "mixed.case@example.com", b: ATTACKER, c: None}


async def test_an_email_is_only_filled_in_when_the_record_has_none(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    first = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "A", "phone": PHONE}))
    assert first.status_code == 201 and await customers_of(seeded) == [(PHONE, None)]
    await booked_with_email(seeded, PHONE, VICTIM, hour=12)
    assert await customers_of(seeded) == [(PHONE, VICTIM)]  # nothing there: it is taken
    await booked_with_email(seeded, PHONE, ATTACKER, hour=14)
    assert await customers_of(seeded) == [(PHONE, VICTIM)]  # something there: it stays


async def test_an_unconfirmed_email_link_opens_only_the_bookings_made_with_that_address(seeded: Seed, links: list[str]) -> None:
    victim_booking = await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    attacker_booking = await booked_with_email(seeded, PHONE, ATTACKER, hour=12)
    attacker = await link_session(seeded, links, ATTACKER)
    mine = (await seeded.client.get("/v1/public/me/bookings", headers=attacker)).json()
    assert [b["id"] for b in mine] == [attacker_booking] and victim_booking not in {b["id"] for b in mine}
    claims = decode_jwt(attacker["Authorization"].removeprefix("Bearer "))
    assert claims["em"] == ATTACKER and claims["role"] == "customer"
    victim = await link_session(seeded, links, VICTIM)  # and the victim's own address opens theirs
    assert [b["id"] for b in (await seeded.client.get("/v1/public/me/bookings", headers=victim)).json()] == [victim_booking]


async def test_the_address_is_matched_in_any_case_and_bookings_in_the_past_are_listed_on_request(seeded: Seed, links: list[str]) -> None:
    bid = await booked_with_email(seeded, PHONE, "Aisha.Karim@Example.com", hour=10)
    session = await link_session(seeded, links, "AISHA.KARIM@example.COM")
    assert [b["id"] for b in (await seeded.client.get("/v1/public/me/bookings", headers=session)).json()] == [bid]
    assert [b["id"] for b in (await seeded.client.get("/v1/public/me/bookings?include_past=true", headers=session)).json()] == [bid]


async def test_a_limited_session_cannot_touch_the_other_addresses_bookings(seeded: Seed, links: list[str]) -> None:
    victim_booking = await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    attacker_booking = await booked_with_email(seeded, PHONE, ATTACKER, hour=12)
    attacker = await link_session(seeded, links, ATTACKER)
    assert (await cancel(seeded, attacker, victim_booking)).status_code == 404
    moved = await seeded.client.post(f"/v1/public/bookings/{victim_booking}/reschedule", json={"start_at": at(seeded.day, 15).isoformat()}, headers=attacker)
    assert moved.status_code == 404
    assert (await seeded.client.post(f"/v1/public/bookings/{victim_booking}/review", json={"rating": 5}, headers=attacker)).status_code == 404
    own = await seeded.client.post(f"/v1/public/bookings/{attacker_booking}/reschedule", json={"start_at": at(seeded.day, 16).isoformat()}, headers=attacker)
    assert own.status_code == 200, own.text
    assert (await cancel(seeded, attacker, attacker_booking)).status_code == 200
    status = {b["id"]: b["status"] for b in (await seeded.client.get("/v1/admin/bookings", headers=seeded.admin)).json()}
    assert status[victim_booking] == "confirmed" and status[attacker_booking] == "cancelled"


async def test_a_limited_session_can_review_its_own_completed_visit(seeded: Seed, links: list[str]) -> None:
    bid = await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    assert (await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "completed"}, headers=seeded.admin)).status_code == 200
    session = await link_session(seeded, links, ATTACKER)
    assert (await seeded.client.post(f"/v1/public/bookings/{bid}/review", json={"rating": 4}, headers=session)).status_code == 201


# --------------------------------------------------------------------- what a limited session may not do
FULL_ONLY: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/v1/public/me/profile", None),
    ("PATCH", "/v1/public/me/profile", {"gender": "female"}),
    ("POST", "/v1/public/me/data-requests", {"kind": "access"}),
    ("GET", "/v1/public/me/data-requests", None),
    ("GET", "/v1/public/me/addresses", None),
    ("POST", "/v1/public/me/addresses", {"line1": "1 Road", "latitude": 3.1, "longitude": 101.6}),
    ("PATCH", f"/v1/public/me/addresses/{uuid.uuid4()}", {"city": "KL"}),
    ("DELETE", f"/v1/public/me/addresses/{uuid.uuid4()}", None),
    ("GET", "/v1/public/me/vehicles", None),
    ("POST", "/v1/public/me/vehicles", {"plate_no": "ABC1", "supported_vehicle_id": str(uuid.uuid4())}),
    ("PATCH", f"/v1/public/me/vehicles/{uuid.uuid4()}", {"year": 2020}),
    ("DELETE", f"/v1/public/me/vehicles/{uuid.uuid4()}", None),
    ("POST", "/v1/public/auth/email", {"email": "new@example.com"}),
]


@pytest.mark.parametrize(("method", "path", "body"), FULL_ONLY, ids=[f"{m} {p.split('/v1/public/')[1][:22]}" for m, p, _ in FULL_ONLY])
async def test_saved_details_profile_and_data_requests_need_a_full_sign_in(seeded: Seed, links: list[str], method: str, path: str, body: dict[str, Any] | None) -> None:
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    limited = await link_session(seeded, links, ATTACKER)
    r = await seeded.client.request(method, path, json=body, headers=limited)
    assert r.status_code == 403 and "phone number" in r.json()["detail"], r.text
    full = await login(seeded, PHONE)
    assert (await seeded.client.request(method, path, json=body, headers=full)).status_code != 403  # the same call is allowed to a full session


def test_every_customer_route_is_decided_here() -> None:
    """A new ``/public/me/...`` route fails this until someone decides whether a limited session may use it."""
    import re

    from app.main import app

    def shape(path: str) -> str:
        return re.sub(r"\{[a-z_]+\}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "{id}", path)

    found = {
        (method.upper(), shape(path)) for path, item in app.openapi()["paths"].items() for method in item
        if "/public/me/" in path or path.endswith("/public/auth/email")
    }
    assert found == {(m, shape(p)) for m, p, _ in FULL_ONLY} | {("GET", "/v1/public/me/bookings")}


async def test_a_limited_session_cannot_sign_in_as_staff_and_still_needs_the_site_key(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    limited = await link_session(seeded, links, ATTACKER)
    assert (await seeded.client.get("/v1/admin/bookings", headers={"Authorization": limited["Authorization"], **seeded.sk})).status_code == 403
    assert (await seeded.client.get("/v1/public/me/bookings", headers={"Authorization": limited["Authorization"]})).status_code == 401


async def test_a_token_claiming_an_empty_or_odd_scope_is_refused(seeded: Seed) -> None:
    from app.core.security import issue_jwt

    mei = await seeded.add_staff("Mei")
    assert (await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "A", "phone": PHONE}))).status_code == 201
    async with tenant_session(seeded.tenant_id) as db:
        cid = str((await db.execute(text("SELECT id FROM customers"))).scalar_one())
    import jwt as pyjwt

    base = decode_jwt(issue_jwt(tenant_id=seeded.tenant_id, subject=cid, role="customer"))
    for odd in ("", 5, ["a"], {"x": 1}):
        forged = pyjwt.encode({**base, "em": odd}, get_settings().jwt_secret, algorithm=get_settings().jwt_algorithm)
        assert (await seeded.client.get("/v1/public/me/bookings", headers=bearer(seeded, forged))).status_code == 401, odd
    full = pyjwt.encode(base, get_settings().jwt_secret, algorithm=get_settings().jwt_algorithm)
    assert (await seeded.client.get("/v1/public/me/profile", headers=bearer(seeded, full))).status_code == 200


def test_a_booking_is_visible_to_its_own_customer_and_to_its_own_address_only() -> None:
    cid, other = uuid.uuid4(), uuid.uuid4()
    mine = Booking(customer_id=cid, contact_email="a@example.com")
    none_given = Booking(customer_id=cid, contact_email=None)
    assert booking_module.visible_to_customer(mine, cid, None) and booking_module.visible_to_customer(none_given, cid, None)
    assert booking_module.visible_to_customer(mine, cid, "a@example.com")
    assert not booking_module.visible_to_customer(mine, cid, "b@example.com")
    assert not booking_module.visible_to_customer(none_given, cid, "a@example.com")  # a limited session never sees a booking made without an email
    assert not booking_module.visible_to_customer(mine, other, None) and not booking_module.visible_to_customer(mine, other, "a@example.com")


# --------------------------------------------------------------------- where the messages go
async def test_a_cancellation_goes_to_the_address_the_booking_was_made_with(seeded: Seed, mail: Recorder) -> None:
    victim_booking = await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    attacker_booking = await booked_with_email(seeded, PHONE, ATTACKER, hour=12)
    await run_outbox(seeded)
    mail.sent.clear()
    for bid in (victim_booking, attacker_booking):
        assert (await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=seeded.admin)).status_code == 200
    await run_outbox(seeded)
    assert sorted(m.recipient for m in mail.sent) == [ATTACKER, VICTIM]
    by_booking = {r.booking_id: r.recipient for r in await log_rows(seeded) if r.event == "booking.cancelled"}
    assert by_booking[uuid.UUID(victim_booking)] == "v***@example.com" and by_booking[uuid.UUID(attacker_booking)] == "a***@example.com"


async def test_an_unconfirmed_customer_email_is_not_used_for_a_booking_without_one(seeded: Seed, mail: Recorder) -> None:
    await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    mei = await seeded.add_staff("Lin")
    r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, 14), mei, customer={"name": "A", "phone": PHONE}))
    bid = r.json()["id"]
    await run_outbox(seeded)
    assert [m.recipient for m in mail.sent] == [VICTIM]  # only the booking that carried an email got one
    (row,) = [x for x in await log_rows(seeded, bid)]
    assert (row.status, row.error) == ("skipped", "no email address on file")


async def test_a_confirmed_customer_email_is_used_for_a_booking_without_one(seeded: Seed, links: list[str], mail: Recorder) -> None:
    await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    await confirm_email(seeded, links, PHONE, VICTIM)
    await run_outbox(seeded)
    mail.sent.clear()
    mei = await seeded.add_staff("Lin")
    r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, 14), mei, customer={"name": "A", "phone": PHONE}))
    assert r.status_code == 201
    await run_outbox(seeded)
    assert [(m.channel, m.recipient) for m in mail.sent if m.subject == "Your booking is confirmed"] == [("email", VICTIM)]


async def test_a_booking_email_beats_the_confirmed_one_for_that_booking(seeded: Seed, links: list[str], mail: Recorder) -> None:
    await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    await confirm_email(seeded, links, PHONE, VICTIM)
    await run_outbox(seeded)
    await booked_with_email(seeded, PHONE, "work@example.com", hour=12)
    mail.sent.clear()
    await run_outbox(seeded)
    assert [m.recipient for m in mail.sent] == ["work@example.com"]  # not the confirmed address on the record


async def test_text_and_whatsapp_messages_still_go_to_the_phone(seeded: Seed, mail: Recorder) -> None:
    assert (await put_template(seeded, "booking.confirmed", "sms", body="Hi $customer_name")).status_code == 200
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    await run_outbox(seeded)
    assert {(m.channel, m.recipient) for m in mail.sent} == {("email", ATTACKER), ("sms", PHONE)}


# --------------------------------------------------------------------- confirming an email
async def test_a_confirmed_email_signs_in_as_the_whole_customer(seeded: Seed, links: list[str]) -> None:
    mine = await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    other = await booked_with_email(seeded, PHONE, "work@example.com", hour=12)
    await confirm_email(seeded, links, PHONE, VICTIM)
    email, confirmed = await email_state(seeded)
    assert email == VICTIM and confirmed is not None
    full = await link_session(seeded, links, VICTIM)
    assert "em" not in decode_jwt(full["Authorization"].removeprefix("Bearer "))
    assert {b["id"] for b in (await seeded.client.get("/v1/public/me/bookings", headers=full)).json()} == {mine, other}  # the address on file opens all of them
    profile = await seeded.client.get("/v1/public/me/profile", headers=full)
    assert profile.status_code == 200 and profile.json()["email"] == VICTIM and profile.json()["email_verified_at"] is not None
    assert (await seeded.client.get("/v1/public/me/addresses", headers=full)).status_code == 200


async def test_confirming_replaces_an_email_somebody_else_typed_in(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)  # the record's first email is the attacker's
    assert (await email_state(seeded))[0] == ATTACKER
    await confirm_email(seeded, links, PHONE, VICTIM)
    email, confirmed = await email_state(seeded)
    assert email == VICTIM and confirmed is not None
    limited = await link_session(seeded, links, ATTACKER)  # their address is no longer on the record: it opens only what it was used for
    assert (await seeded.client.get("/v1/public/me/profile", headers=limited)).status_code == 403


async def test_changing_to_another_confirmed_address_works_and_the_old_one_stops_signing_in_fully(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    await confirm_email(seeded, links, PHONE, VICTIM)
    await confirm_email(seeded, links, PHONE, "new@example.com")
    assert (await email_state(seeded))[0] == "new@example.com"
    old = await link_session(seeded, links, VICTIM)
    assert (await seeded.client.get("/v1/public/me/profile", headers=old)).status_code == 403  # now only the bookings made with it


async def test_the_confirmed_address_of_one_customer_wins_over_another_customers_booking_email(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    await confirm_email(seeded, links, PHONE, VICTIM)
    await booked_with_email(seeded, OTHER_PHONE, VICTIM, hour=12)  # a second person typed the same address into a booking
    full = await link_session(seeded, links, VICTIM)
    assert (await seeded.client.get("/v1/public/me/profile", headers=full)).json()["phone"] == PHONE


async def test_a_confirmed_address_shared_by_two_customers_needs_the_phone(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded, PHONE, "family@example.com", hour=10)
    await booked_with_email(seeded, OTHER_PHONE, "family@example.com", hour=12)
    await confirm_email(seeded, links, PHONE, "family@example.com")
    await confirm_email(seeded, links, OTHER_PHONE, "family@example.com")
    before = len(links)
    await ask(seeded, "family@example.com")
    assert len(links) == before  # ambiguous: no link rather than a guess (not even a limited one)
    await ask(seeded, "family@example.com", phone=OTHER_PHONE)
    assert len(links) == before + 1
    session = bearer(seeded, (await redeem(seeded, links[-1])).json()["access_token"])
    assert (await seeded.client.get("/v1/public/me/profile", headers=session)).json()["phone"] == OTHER_PHONE


async def test_a_phone_that_does_not_match_gets_no_limited_link_either(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    await ask(seeded, ATTACKER, phone="+60177770000")
    assert links == []
    await ask(seeded, ATTACKER, phone=PHONE)
    assert len(links) == 1


async def test_two_customers_who_typed_the_same_unconfirmed_address_need_the_phone(seeded: Seed, links: list[str]) -> None:
    mine = await booked_with_email(seeded, PHONE, "shared@example.com", hour=10)
    theirs = await booked_with_email(seeded, OTHER_PHONE, "shared@example.com", hour=12)
    await ask(seeded, "shared@example.com")
    assert links == []
    session = await link_session(seeded, links, "shared@example.com", phone=OTHER_PHONE)
    assert [b["id"] for b in (await seeded.client.get("/v1/public/me/bookings", headers=session)).json()] == [theirs] and mine != theirs


async def test_asking_for_the_confirmation_needs_a_sign_in_and_gives_nothing_away(seeded: Seed, links: list[str]) -> None:
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=seeded.pk)).status_code == 401
    mei = await seeded.add_staff("Mei")
    assert (await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "A", "phone": PHONE}))).status_code == 201
    me = await login(seeded, PHONE)
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": "not-an-email"}, headers=me)).status_code == 422
    assert (await seeded.client.post("/v1/public/auth/email", json={}, headers=me)).status_code == 422
    assert links == [] and (await email_state(seeded)) == (None, None)  # asking changes nothing until the link is followed


async def test_asking_sends_one_email_to_the_new_address_with_the_link(seeded: Seed, links: list[str], mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "email_confirm_url", "https://shop.example.com/confirm?t={token}")
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    me = await login(seeded, PHONE)
    mail.sent.clear()
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": "Victim@Example.com"}, headers=me)).status_code == 202
    (message,) = mail.sent
    assert (message.channel, message.recipient, message.subject) == ("email", VICTIM, f"Confirm your email for {seeded_name(message)}")
    assert f"https://shop.example.com/confirm?t={links[-1]}" in message.body and "ignore this email" in message.body
    assert await email_state(seeded) == (ATTACKER, None)
    rows = [r for r in await log_rows(seeded) if r.event == "auth.email_confirm"]
    assert [(r.status, r.recipient) for r in rows] == [("sent", "v***@example.com")]


def seeded_name(message: Any) -> str:
    return message.subject.removeprefix("Confirm your email for ")


async def test_without_a_template_the_email_carries_the_bare_token(seeded: Seed, links: list[str], mail: Recorder) -> None:
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    me = await login(seeded, PHONE)
    mail.sent.clear()
    await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)
    assert f"Your confirmation token: {links[-1]}" in mail.sent[0].body


async def test_a_confirmation_link_works_once_and_only_the_newest(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    me = await login(seeded, PHONE)
    await seeded.client.post("/v1/public/auth/email", json={"email": "first@example.com"}, headers=me)
    await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)  # the test settings have no cool-down
    assert len(links) == 2
    assert (await seeded.client.post("/v1/public/auth/email/confirm", json={"token": links[0]}, headers=seeded.pk)).status_code == 401
    assert (await seeded.client.post("/v1/public/auth/email/confirm", json={"token": links[1]}, headers=seeded.pk)).status_code == 200
    assert (await seeded.client.post("/v1/public/auth/email/confirm", json={"token": links[1]}, headers=seeded.pk)).status_code == 401
    assert (await email_state(seeded))[0] == VICTIM


async def test_a_confirmation_link_expires_and_belongs_to_one_business(client: httpx.AsyncClient, links: list[str], otp: Any) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await booked_with_email(a, PHONE, ATTACKER, hour=10)
    me = await login(a, PHONE)
    await a.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)
    ttl = [await otp.ttl(k) for k in await otp.keys("emailconfirm:*:t:*")]
    assert ttl and all(0 < t <= get_settings().magic_link_ttl_seconds for t in ttl)
    stored = [str(await otp.get(k)) for k in await otp.keys("*") if await otp.type(k) == "string"] + await otp.keys("*")
    assert not any(links[-1] in item for item in stored)  # the token is kept hashed
    assert (await b.client.post("/v1/public/auth/email/confirm", json={"token": links[-1]}, headers=b.pk)).status_code == 401
    await otp.flushall()  # time passed
    assert (await a.client.post("/v1/public/auth/email/confirm", json={"token": links[-1]}, headers=a.pk)).status_code == 401
    assert (await email_state(a))[0] == ATTACKER


async def test_a_customer_erased_before_following_the_link_stays_erased(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    me = await login(seeded, PHONE)
    await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE customers SET erased_at = now(), email = NULL"))
    assert (await seeded.client.post("/v1/public/auth/email/confirm", json={"token": links[-1]}, headers=seeded.pk)).status_code == 401
    assert (await email_state(seeded))[0] is None
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)).status_code == 404


async def test_an_address_that_is_already_confirmed_is_not_sent_again(seeded: Seed, links: list[str], mail: Recorder) -> None:
    await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    await confirm_email(seeded, links, PHONE, VICTIM)
    me = await login(seeded, PHONE)
    mail.sent.clear()
    r = await seeded.client.post("/v1/public/auth/email", json={"email": "VICTIM@example.com"}, headers=me)
    assert r.status_code == 409 and mail.sent == []
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": ATTACKER}, headers=me)).status_code == 202  # a different one is fine


async def test_asking_is_rate_limited_and_a_failed_send_gives_the_place_back(seeded: Seed, links: list[str], mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    me = await login(seeded, PHONE)
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 60)
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)).status_code == 202
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)).status_code == 429
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 0)
    monkeypatch.setattr(get_settings(), "sign_in_requests_per_day", 3)
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)).status_code == 202  # 2 of 3 today
    mail.failing["email"] = RuntimeError("smtp down")
    failed = await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)  # 3 of 3, but it fails
    assert failed.status_code == 503
    assert (await seeded.client.post("/v1/public/auth/email/confirm", json={"token": links[-1]}, headers=seeded.pk)).status_code == 401  # nobody got it
    mail.failing.clear()
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)).status_code == 202  # the failed one did not use the day up
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)).status_code == 429
    assert [r.status for r in await log_rows(seeded) if r.event == "auth.email_confirm"].count("failed") == 1


# --------------------------------------------------------------------- erasure and export
async def test_erasing_a_customer_clears_the_contact_emails_and_the_confirmation(seeded: Seed, links: list[str]) -> None:
    bid = await booked_with_email(seeded, PHONE, VICTIM, hour=10)
    await confirm_email(seeded, links, PHONE, VICTIM)
    assert (await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=seeded.admin)).status_code == 200
    async with tenant_session(seeded.tenant_id) as db:
        tenant = await db.get(Tenant, seeded.tenant_id)
        customer = (await db.scalars(select(Customer))).one()
        assert tenant is not None
        export = await compliance.export_customer(db, tenant, customer.id)
        assert export["customer"]["email_confirmed_at"] is not None and [b["contact_email"] for b in export["bookings"]] == [VICTIM]
        await compliance.erase_customer(db, tenant, customer.id, dt.datetime.now(dt.UTC))
    assert await contact_emails(seeded) == {bid: None}
    async with tenant_session(seeded.tenant_id) as db:
        row = (await db.execute(text("SELECT email, email_verified_at FROM customers"))).one()
    assert tuple(row) == (None, None)
    before = len(links)
    await ask(seeded, VICTIM)
    assert len(links) == before  # nothing is left to open


async def test_one_customer_with_several_bookings_made_with_one_address_is_one_match(seeded: Seed, links: list[str]) -> None:
    first = await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    second = await booked_with_email(seeded, PHONE, ATTACKER, hour=12)
    session = await link_session(seeded, links, ATTACKER)
    assert {b["id"] for b in (await seeded.client.get("/v1/public/me/bookings", headers=session)).json()} == {first, second}


async def test_a_failed_send_does_not_keep_the_cool_down(seeded: Seed, links: list[str], mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    await booked_with_email(seeded, PHONE, ATTACKER, hour=10)
    me = await login(seeded, PHONE)
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 60)
    mail.failing["email"] = RuntimeError("smtp down")
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)).status_code == 503
    mail.failing.clear()
    assert (await seeded.client.post("/v1/public/auth/email", json={"email": VICTIM}, headers=me)).status_code == 202  # nothing was sent, so no wait
