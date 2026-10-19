"""Review invitations: the link in the completed-visit message, and reviewing through it without signing in (blueprint §13.4: "review request sent")."""
from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import select

from app.core import ratelimit
from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.exceptions import Unauthorized
from app.core.security import REVIEW_TOKEN_TYPE, decode_jwt, decode_review_token, issue_jwt, issue_review_token
from app.models import Customer, OutboxEvent, Review, Tenant
from app.services import notifications as notes
from tests.conftest import Recorder, Seed, at, make_seed
from tests.test_customer_cancel import PHONE
from tests.test_customer_identity import EMAIL, booked_with_email
from tests.test_notifications import put_template, run_outbox

PAGE = "https://shop.example.test/review?t={token}"
LINK = re.compile(r"https://\S+/review\?t=([A-Za-z0-9._-]+)")


async def set_page(s: Seed, url: str | None = PAGE, name: str = "Glow Spa") -> None:
    r = await s.client.put("/v1/admin/business-profile", json={"business_name": name, "review_url": url}, headers=s.admin)
    assert r.status_code == 200, r.text


async def complete(s: Seed, bid: str) -> None:
    r = await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "completed"}, headers=s.admin)
    assert r.status_code == 200, r.text


def token_from(mail: Recorder) -> str:
    match = LINK.search(mail.sent[-1].body)
    assert match, mail.sent[-1].body
    return match.group(1)


async def lookup(s: Seed, token: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return await s.client.post("/v1/public/review-invitation", json={"token": token}, headers=s.pk if headers is None else headers)


async def submit(s: Seed, token: str, rating: Any = 5, comment: str | None = "Lovely", headers: dict[str, str] | None = None) -> httpx.Response:
    body: dict[str, Any] = {"token": token, "rating": rating}
    if comment is not None:
        body["comment"] = comment
    return await s.client.post("/v1/public/review-invitation/review", json=body, headers=s.pk if headers is None else headers)


# --------------------------------------------------------------------- the token
def test_a_review_token_names_one_booking_of_one_business() -> None:
    tenant, booking = uuid.uuid4(), uuid.uuid4()
    assert decode_review_token(issue_review_token(tenant_id=tenant, booking_id=booking)) == (tenant, booking)
    other = decode_review_token(issue_review_token(tenant_id=tenant, booking_id=uuid.uuid4()))
    assert other[0] == tenant and other[1] != booking


def test_a_review_token_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    token = issue_review_token(tenant_id=uuid.uuid4(), booking_id=uuid.uuid4())
    monkeypatch.setattr(get_settings(), "review_link_days", -1)  # (the setting itself refuses less than a day; this forges a token that is already old)
    expired = issue_review_token(tenant_id=uuid.uuid4(), booking_id=uuid.uuid4())
    decode_review_token(token)
    with pytest.raises(Unauthorized, match="invalid or has expired"):
        decode_review_token(expired)
    claims = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 30 * 86400  # the default lifetime


def test_the_lifetime_follows_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "review_link_days", 7)
    claims = jwt.decode(issue_review_token(tenant_id=uuid.uuid4(), booking_id=uuid.uuid4()), get_settings().jwt_secret, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 7 * 86400


def test_only_a_genuine_review_token_is_accepted_as_one() -> None:
    tenant, booking = uuid.uuid4(), uuid.uuid4()
    secret = get_settings().jwt_secret
    good = issue_review_token(tenant_id=tenant, booking_id=booking)
    forged = jwt.encode({"typ": REVIEW_TOKEN_TYPE, "sub": str(booking), "tid": str(tenant), "exp": 4102444800}, "another-secret-0123456789-abcdefghijk", algorithm="HS256")
    other_kind = jwt.encode({"typ": "access", "sub": str(booking), "tid": str(tenant), "exp": 4102444800}, secret, algorithm="HS256")
    no_type = jwt.encode({"sub": str(booking), "tid": str(tenant), "exp": 4102444800}, secret, algorithm="HS256")
    no_expiry = jwt.encode({"typ": REVIEW_TOKEN_TYPE, "sub": str(booking), "tid": str(tenant)}, secret, algorithm="HS256")
    not_ids = jwt.encode({"typ": REVIEW_TOKEN_TYPE, "sub": "booking-1", "tid": str(tenant), "exp": 4102444800}, secret, algorithm="HS256")
    unsigned = jwt.encode({"typ": REVIEW_TOKEN_TYPE, "sub": str(booking), "tid": str(tenant), "exp": 4102444800}, None, algorithm="none")
    for bad in (good[:-3] + "abc", forged, other_kind, no_type, no_expiry, not_ids, unsigned, "", "not.a.token", issue_jwt(tenant_id=tenant, subject=str(booking), role="owner")):
        with pytest.raises(Unauthorized):
            decode_review_token(bad)


def test_a_review_token_is_no_login_token() -> None:
    token = issue_review_token(tenant_id=uuid.uuid4(), booking_id=uuid.uuid4())
    with pytest.raises(Unauthorized):
        decode_jwt(token)


async def test_a_review_token_opens_no_other_door(seeded: Seed) -> None:
    token = issue_review_token(tenant_id=seeded.tenant_id, booking_id=uuid.uuid4())
    bearer = {"Authorization": f"Bearer {token}"}
    assert (await seeded.client.get("/v1/admin/bookings", headers=bearer)).status_code == 401
    assert (await seeded.client.get("/v1/public/me/bookings", headers={**seeded.pk, **bearer})).status_code == 401
    assert (await seeded.client.get("/v1/public/me/profile", headers={**seeded.pk, **bearer})).status_code == 401


def test_the_review_endpoints_are_rate_limited_like_other_public_writes() -> None:
    assert ratelimit.classify("POST", "/v1/public/review-invitation") == "public_write"
    assert ratelimit.classify("POST", "/v1/public/review-invitation/review") == "public_write"


# --------------------------------------------------------------------- the business's review page
async def test_the_review_page_is_a_profile_setting_that_must_carry_the_token(seeded: Seed) -> None:
    for bad in ("https://shop.example.test/review", "javascript:alert({token})", "ftp://shop.example.test/{token}", "https://shop.example.test/a b/{token}"):
        r = await seeded.client.put("/v1/admin/business-profile", json={"business_name": "Glow Spa", "review_url": bad}, headers=seeded.admin)
        assert r.status_code == 422, bad
    await set_page(seeded)
    shown = (await seeded.client.get("/v1/admin/business-profile", headers=seeded.admin)).json()
    assert shown["review_url"] == PAGE
    await set_page(seeded, None)  # the whole profile is replaced: leaving it out clears it
    assert (await seeded.client.get("/v1/admin/business-profile", headers=seeded.admin)).json()["review_url"] is None


async def test_the_public_profile_does_not_publish_the_review_page(seeded: Seed) -> None:
    await set_page(seeded)
    public = (await seeded.client.get("/v1/public/business", headers=seeded.pk)).json()
    assert public["business_name"] == "Glow Spa" and public.get("review_url") is None


def test_the_platform_default_review_page_must_carry_the_token() -> None:
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError, match="REVIEW_URL"):
        Settings(_env_file=None, review_url="https://shop.example.test/review")  # type: ignore[call-arg]
    assert Settings(_env_file=None, review_url=PAGE).review_url == PAGE  # type: ignore[call-arg]
    assert Settings(_env_file=None).review_url == ""  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, review_link_days=0)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, review_link_days=366)  # type: ignore[call-arg]


# --------------------------------------------------------------------- the message
async def test_the_completed_visit_message_links_to_the_review_page(seeded: Seed, mail: Recorder) -> None:
    await set_page(seeded)
    bid = await booked_with_email(seeded)
    await complete(seeded, bid)
    await run_outbox(seeded)
    sent = mail.sent[-1]
    assert sent.subject == "Thank you for visiting us" and sent.recipient == EMAIL
    assert "How was your visit? Rate it here: https://shop.example.test/review?t=" in sent.body
    assert decode_review_token(token_from(mail)) == (seeded.tenant_id, uuid.UUID(bid))
    assert "when you sign in" not in sent.body


async def test_without_a_review_page_the_message_keeps_its_old_wording(seeded: Seed, mail: Recorder) -> None:
    bid = await booked_with_email(seeded)
    await complete(seeded, bid)
    await run_outbox(seeded)
    body = mail.sent[-1].body
    assert "We hope Massage went well. You can rate your visit when you sign in." in body and "http" not in body


async def test_the_platform_default_page_is_used_when_the_business_has_none_and_the_business_overrides_it(seeded: Seed, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "review_url", "https://platform.example.test/r/{token}")
    first = await booked_with_email(seeded, hour=10)
    await complete(seeded, first)
    await run_outbox(seeded)
    assert "https://platform.example.test/r/" in mail.sent[-1].body
    await set_page(seeded)
    second = await booked_with_email(seeded, hour=12)
    await complete(seeded, second)
    await run_outbox(seeded)
    assert "https://shop.example.test/review?t=" in mail.sent[-1].body and "platform.example.test" not in mail.sent[-1].body


async def test_other_messages_carry_no_review_link(seeded: Seed, mail: Recorder) -> None:
    await set_page(seeded)
    assert (await put_template(seeded, "booking.confirmed", "email", subject="Hi", body="Rate: [$review_link] [$review_prompt]")).status_code == 200
    await booked_with_email(seeded)
    await run_outbox(seeded)
    assert mail.sent[-1].body == "Rate: [] []"


async def test_a_business_can_use_the_link_in_its_own_wording(seeded: Seed, mail: Recorder) -> None:
    await set_page(seeded)
    assert (await put_template(seeded, "booking.completed", "email", subject="How did we do?", body="Tell us: $review_link")).status_code == 200
    bid = await booked_with_email(seeded)
    await complete(seeded, bid)
    await run_outbox(seeded)
    assert mail.sent[-1].subject == "How did we do?" and mail.sent[-1].body.startswith("Tell us: https://shop.example.test/review?t=")
    assert decode_review_token(token_from(mail))[1] == uuid.UUID(bid)
    placeholders = (await seeded.client.get("/v1/admin/notification-placeholders", headers=seeded.admin)).json()
    assert "review_link" in placeholders and "review_prompt" in placeholders


async def test_a_whatsapp_template_gets_the_link_as_its_fifth_variable_only_when_there_is_one(seeded: Seed, mail: Recorder) -> None:
    assert (await put_template(seeded, "booking.completed", "whatsapp", body="x", external_template="visit_done")).status_code == 200
    plain = await booked_with_email(seeded, hour=10)
    await complete(seeded, plain)
    await run_outbox(seeded)
    whatsapp = [m for m in mail.sent if m.channel == "whatsapp"]
    assert len(whatsapp) == 1 and len(whatsapp[0].params) == 4  # no review page: the four variables an approved template already has
    await set_page(seeded)
    linked = await booked_with_email(seeded, hour=12)
    await complete(seeded, linked)
    await run_outbox(seeded)
    whatsapp = [m for m in mail.sent if m.channel == "whatsapp"]
    assert len(whatsapp) == 2 and len(whatsapp[1].params) == 5 and whatsapp[1].params[4].startswith("https://shop.example.test/review?t=")
    assert decode_review_token(whatsapp[1].params[4].split("t=")[1])[1] == uuid.UUID(linked)


async def test_no_link_once_the_visit_was_reviewed_or_the_customer_erased(seeded: Seed) -> None:
    await set_page(seeded)
    bid = await booked_with_email(seeded)
    await complete(seeded, bid)
    async with tenant_session(seeded.tenant_id) as db:
        tenant = await db.get(Tenant, seeded.tenant_id)
        from app.models import Booking

        booking = await db.get(Booking, uuid.UUID(bid))
        assert tenant is not None and booking is not None
        customer = await db.get(Customer, booking.customer_id)
        assert customer is not None
        assert (await notes.review_link_for(db, tenant, booking, customer)).startswith("https://shop.example.test/review?t=")
        db.add(Review(tenant_id=tenant.id, booking_id=booking.id, customer_id=customer.id, rating=4))
        await db.flush()
        assert await notes.review_link_for(db, tenant, booking, customer) == ""  # already reviewed: do not ask again
        await db.execute(Review.__table__.delete())
        assert (await notes.review_link_for(db, tenant, booking, customer)) != ""
        import datetime as dt

        customer.erased_at = dt.datetime.now(dt.UTC)
        assert await notes.review_link_for(db, tenant, booking, customer) == ""


# --------------------------------------------------------------------- reviewing through the link
async def test_the_whole_journey_from_the_email_to_a_published_review(seeded: Seed, mail: Recorder) -> None:
    await set_page(seeded)
    bid = await booked_with_email(seeded)
    served_by = (await seeded.client.get(f"/v1/admin/bookings/{bid}", headers=seeded.admin)).json()["resource_ids"]
    before = await lookup(seeded, issue_review_token(tenant_id=seeded.tenant_id, booking_id=uuid.UUID(bid)))
    assert before.status_code == 200 and before.json()["status"] == "not_completed"  # the visit has not happened yet
    await complete(seeded, bid)
    await run_outbox(seeded)
    token = token_from(mail)
    page = await lookup(seeded, token)
    assert page.status_code == 200
    assert set(page.json()) == {"business_name", "service_names", "start_at", "status"}  # nothing about the customer
    assert page.json()["business_name"] == "Glow Spa" and page.json()["service_names"] == ["Massage"] and page.json()["status"] == "open"
    done = await submit(seeded, token, rating=5, comment="  Lovely  ")
    assert done.status_code == 201, done.text
    assert done.json()["rating"] == 5 and done.json()["comment"] == "Lovely" and done.json()["reviewer"] == "Aisha"
    assert [done.json()["resource_id"]] == served_by  # attributed to the staff member who served it
    assert (await lookup(seeded, token)).json()["status"] == "already_reviewed"
    again = await submit(seeded, token)
    assert again.status_code == 409 and "already reviewed" in again.json()["detail"]  # the link works once
    summary = (await seeded.client.get("/v1/public/reviews/summary", headers=seeded.pk)).json()
    assert summary == {"count": 1, "average": 5.0}
    async with tenant_session(seeded.tenant_id) as db:
        events = (await db.scalars(select(OutboxEvent).where(OutboxEvent.event_type == "review.created"))).all()
    assert len(events) == 1 and events[0].payload["rating"] == 5


async def test_a_visit_that_is_not_completed_cannot_be_reviewed(seeded: Seed) -> None:
    bid = await booked_with_email(seeded)
    token = issue_review_token(tenant_id=seeded.tenant_id, booking_id=uuid.UUID(bid))
    refused = await submit(seeded, token)
    assert refused.status_code == 409 and "completed" in refused.json()["detail"]
    r = await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=seeded.admin)
    assert r.status_code == 200
    assert (await lookup(seeded, token)).json()["status"] == "not_completed"
    assert (await submit(seeded, token)).status_code == 409


async def test_a_token_from_another_business_or_for_nothing_is_refused(client: httpx.AsyncClient) -> None:
    mine, other = await make_seed(client), await make_seed(client)
    bid = await booked_with_email(mine)
    await complete(mine, bid)
    token = issue_review_token(tenant_id=mine.tenant_id, booking_id=uuid.UUID(bid))
    assert (await lookup(other, token)).status_code == 401  # presented at a different business
    assert (await submit(other, token)).status_code == 401
    ghost = issue_review_token(tenant_id=other.tenant_id, booking_id=uuid.UUID(bid))  # claims to be theirs, names my booking
    assert (await lookup(other, ghost)).status_code == 404  # row-level security: it does not exist for them
    assert (await submit(other, ghost)).status_code == 404
    assert (await lookup(mine, issue_review_token(tenant_id=mine.tenant_id, booking_id=uuid.uuid4()))).status_code == 404
    assert (await seeded_reviews(mine)) == 0


async def seeded_reviews(s: Seed) -> int:
    async with tenant_session(s.tenant_id) as db:
        return len((await db.scalars(select(Review))).all())


async def test_bad_tokens_and_bad_input_are_refused(seeded: Seed) -> None:
    bid = await booked_with_email(seeded)
    await complete(seeded, bid)
    token = issue_review_token(tenant_id=seeded.tenant_id, booking_id=uuid.UUID(bid))
    for bad in (token[:-4] + "abcd", "x" * 40, jwt.encode({"typ": "review"}, "k" * 32, algorithm="HS256")):
        assert (await lookup(seeded, bad)).status_code == 401
        assert (await submit(seeded, bad)).status_code == 401
    assert (await seeded.client.post("/v1/public/review-invitation", json={"token": "short"}, headers=seeded.pk)).status_code == 422
    assert (await seeded.client.post("/v1/public/review-invitation", json={}, headers=seeded.pk)).status_code == 422
    for rating in (0, 6, "5", 4.5, None):
        assert (await submit(seeded, token, rating=rating)).status_code == 422, rating
    assert (await submit(seeded, token, comment="x" * 2001)).status_code == 422
    assert (await seeded.client.post("/v1/public/review-invitation/review", json={"token": token, "rating": 5, "extra": 1}, headers=seeded.pk)).status_code == 422
    assert (await lookup(seeded, token, headers={})).status_code == 401  # the site key is still required
    assert await seeded_reviews(seeded) == 0
    assert (await submit(seeded, token, comment=None)).status_code == 201  # a rating alone is enough


async def test_an_erased_customer_cannot_be_reviewed_for(seeded: Seed) -> None:
    bid = await booked_with_email(seeded)
    await complete(seeded, bid)
    token = issue_review_token(tenant_id=seeded.tenant_id, booking_id=uuid.UUID(bid))
    async with tenant_session(seeded.tenant_id) as db:
        from app.models import Booking

        booking = await db.get(Booking, uuid.UUID(bid))
        assert booking is not None
        customer = await db.get(Customer, booking.customer_id)
        assert customer is not None
        import datetime as dt

        customer.erased_at = dt.datetime.now(dt.UTC)
    assert (await lookup(seeded, token)).status_code == 404
    assert (await submit(seeded, token)).status_code == 404
    assert await seeded_reviews(seeded) == 0


async def test_the_page_shows_when_the_visit_was_and_the_public_sees_only_a_first_name(seeded: Seed, mail: Recorder) -> None:
    staff = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
        at(seeded.day, 10), staff, customer={"name": "Aisha Binti Karim", "phone": PHONE, "email": EMAIL}))
    assert r.status_code == 201
    bid = r.json()["id"]
    await complete(seeded, bid)
    token = issue_review_token(tenant_id=seeded.tenant_id, booking_id=uuid.UUID(bid))
    page = await lookup(seeded, token)
    assert dt.datetime.fromisoformat(page.json()["start_at"]) == at(seeded.day, 10) and page.json()["service_names"] == ["Massage"]
    done = await submit(seeded, token)
    assert done.status_code == 201 and done.json()["reviewer"] == "Aisha"  # never the whole name
    listed = (await seeded.client.get("/v1/public/reviews", headers=seeded.pk)).json()
    assert [x["reviewer"] for x in listed] == ["Aisha"]
    assert mail.sent == []


async def test_the_lookup_takes_nothing_but_the_token(seeded: Seed) -> None:
    token = issue_review_token(tenant_id=seeded.tenant_id, booking_id=uuid.uuid4())
    r = await seeded.client.post("/v1/public/review-invitation", json={"token": token, "booking_id": str(uuid.uuid4())}, headers=seeded.pk)
    assert r.status_code == 422
