"""The public business profile (GET /v1/public/business) and customers' reviews of completed appointments."""
from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from tests.conftest import Seed, at, make_seed
from tests.test_compliance import PHONE_A, PHONE_B, audit, ask, post_booking, requests
from tests.test_customer_cancel import login, otp  # noqa: F401  (``otp`` is a fixture)
from tests.test_history_outbox import events
from tests.test_restaurant import make_restaurant
from tests.test_restaurant import book as rbook

PROFILE: dict[str, Any] = {
    "business_name": "Lotus Spa & Wellness",
    "description": "Calm treatments in the heart of KL.",
    "contact_no": "+60312345678",
    "contact_email": "hello@lotus.example.com",
    "logo_image": "https://cdn.example.com/lotus/logo.png",
    "gallery_images": ["https://cdn.example.com/lotus/1.jpg", "https://cdn.example.com/lotus/2.jpg"],
    "social_media_links": {"instagram": "https://instagram.com/lotus", "facebook": "https://facebook.com/lotus"},
    "address": "12 Jalan Ampang, 50450 Kuala Lumpur",
    "google_maps_url": "https://maps.google.com/?q=lotus",
    "details": {},
}


async def put_profile(s: Seed, body: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
    return await s.client.put("/v1/admin/business-profile", json=body, headers=headers or s.admin)


async def public_business(s: Seed) -> dict[str, Any]:
    r = await s.client.get("/v1/public/business", headers=s.pk)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


# --------------------------------------------------------------------- the business profile
async def test_a_business_without_a_saved_profile_still_has_a_public_page(seeded: Seed) -> None:
    page = await public_business(seeded)
    assert page["business_type"] == "spa" and page["timezone"] == "Asia/Kuala_Lumpur" and page["business_name"].startswith("Spa ")
    assert page["gallery_images"] == [] and page["social_media_links"] == {} and page["location"] is None
    assert (await seeded.client.get("/v1/admin/business-profile", headers=seeded.admin)).json()["business_name"] == page["business_name"]


async def test_saving_a_profile_publishes_it_and_replaces_it_wholesale(seeded: Seed) -> None:
    r = await put_profile(seeded, PROFILE)
    assert r.status_code == 200, r.text
    page = await public_business(seeded)
    assert {k: page[k] for k in PROFILE} == PROFILE and page["business_type"] == "spa"
    assert (await seeded.client.get("/v1/admin/business-profile", headers=seeded.admin)).json()["contact_email"] == "hello@lotus.example.com"
    await put_profile(seeded, {"business_name": "Lotus"})  # a full replace: what is left out is cleared
    page = await public_business(seeded)
    assert page["business_name"] == "Lotus" and page["description"] is None and page["gallery_images"] == [] and page["social_media_links"] == {}
    entry = next(e for e in await audit(seeded) if e["action"] == "PUT /v1/admin/business-profile")
    assert entry["actor_id"] == "wp-1" and "Lotus" not in json.dumps(entry)


async def test_the_page_shows_the_business_location_once_it_is_set(seeded: Seed) -> None:
    assert (await public_business(seeded))["location"] is None
    loc = {"latitude": 3.139, "longitude": 101.6869, "service_radius_km": 25}
    assert (await seeded.client.put("/v1/admin/business-location", json=loc, headers=seeded.admin)).status_code == 200
    assert (await public_business(seeded))["location"] == loc


@pytest.mark.parametrize(("field", "value"), [
    ("logo_image", "javascript:alert(1)"), ("logo_image", "ftp://files.example.com/logo.png"), ("logo_image", "https://exa mple.com/x.png"),
    ("logo_image", "https://example.com/" + "a" * 500), ("google_maps_url", "data:text/html,<script>"),
    ("gallery_images", ["https://ok.example.com/1.jpg", "//no-scheme.example.com/2.jpg"]),
    ("gallery_images", [f"https://example.com/{i}.jpg" for i in range(21)]),
    ("social_media_links", {"Face Book": "https://facebook.com/x"}), ("social_media_links", {"instagram": "not a url"}),
    ("social_media_links", {f"site{i}": "https://example.com" for i in range(11)}),
    ("contact_email", "not-an-email"), ("business_name", ""), ("business_name", "x" * 151), ("description", "x" * 2001),
    ("details", {"cuisine_type": "Thai"}),  # a spa has no cuisine
    ("unknown_field", "x"),
])
async def test_profile_input_is_validated(seeded: Seed, field: str, value: Any) -> None:
    assert (await put_profile(seeded, {**PROFILE, field: value})).status_code == 422
    assert (await public_business(seeded))["business_name"].startswith("Spa ")  # nothing was saved


async def test_only_details_that_suit_the_kind_of_business_are_accepted(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    ok = await put_profile(rest, {"business_name": "Bistro", "details": {"cuisine_type": "Peranakan"}})
    assert ok.status_code == 200 and (await public_business(rest))["details"] == {"cuisine_type": "Peranakan"}
    assert (await put_profile(rest, {"business_name": "Bistro", "details": {"workshop_category": "tyres"}})).status_code == 422
    assert (await put_profile(rest, {"business_name": "Bistro", "details": {"cuisine_type": "x" * 101}})).status_code == 422


async def test_profile_access_rules_and_isolation(client: httpx.AsyncClient) -> None:
    a, b = await make_seed(client), await make_seed(client)
    assert (await put_profile(a, PROFILE, headers=await a.token("staff"))).status_code == 403
    assert (await put_profile(a, PROFILE, headers=await a.token("manager"))).status_code == 200
    assert (await a.client.get("/v1/public/business")).status_code == 401  # needs a site key
    assert (await public_business(b))["business_name"].startswith("Spa ") and (await public_business(a))["business_name"] == PROFILE["business_name"]


# --------------------------------------------------------------------- reviews
async def completed(s: Seed, staff: uuid.UUID, hour: int, phone: str = PHONE_A) -> str:
    bid = await post_booking(s, staff, hour, phone)
    assert (await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "completed"}, headers=s.admin)).status_code == 200
    return bid


async def review(s: Seed, headers: dict[str, str], bid: str, rating: Any = 5, **extra: Any) -> httpx.Response:
    return await s.client.post(f"/v1/public/bookings/{bid}/review", json={"rating": rating, **extra}, headers=headers)


async def public_reviews(s: Seed, **params: Any) -> list[dict[str, Any]]:
    r = await s.client.get("/v1/public/reviews", params=params, headers=s.pk)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def summary(s: Seed, **params: Any) -> dict[str, Any]:
    return (await s.client.get("/v1/public/reviews/summary", params=params, headers=s.pk)).json()  # type: ignore[no-any-return]


async def test_a_customer_reviews_a_completed_appointment_and_it_is_attributed_to_the_stylist(seeded: Seed, otp: Any) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    bid = await completed(seeded, mei, 10)
    await completed(seeded, lin, 11, PHONE_B)
    me = await login(seeded, PHONE_A)
    r = await review(seeded, me, bid, 5, comment="  Wonderful massage!  ")
    assert r.status_code == 201, r.text
    assert (r.json()["rating"], r.json()["comment"], r.json()["reviewer"], r.json()["resource_id"]) == (5, "Wonderful massage!", "Aisha", str(mei))
    listing = await public_reviews(seeded)
    assert [(x["rating"], x["reviewer"], x["resource_id"]) for x in listing] == [(5, "Aisha", str(mei))]
    assert "Karim" not in json.dumps(listing) and PHONE_A not in json.dumps(listing)  # first name only
    assert await public_reviews(seeded, resource_id=str(lin)) == []
    assert len(await public_reviews(seeded, resource_id=str(mei))) == 1
    published = [e for e in await events(seeded) if e.event_type == "review.created"]
    assert len(published) == 1 and set(published[0].payload) == {"review_id", "booking_id", "resource_id", "rating"}


async def test_who_may_review_what_and_how(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    done = await completed(seeded, mei, 10, PHONE_A)
    open_booking = await post_booking(seeded, mei, 12, PHONE_A)
    a, b = await login(seeded, PHONE_A), await login(seeded, PHONE_B)
    assert (await review(seeded, a, open_booking)).status_code == 409  # not completed yet
    assert (await review(seeded, b, done)).status_code == 404  # someone else's booking looks like a missing one
    assert (await review(seeded, a, str(uuid.uuid4()))).status_code == 404
    assert (await seeded.client.post(f"/v1/public/bookings/{done}/review", json={"rating": 5}, headers=seeded.pk)).status_code == 401
    for bad in (0, 6, "5", 4.5, None, True):
        assert (await review(seeded, a, done, bad)).status_code == 422, bad
    assert (await review(seeded, a, done, 5, comment="x" * 2001)).status_code == 422
    assert (await review(seeded, a, done, 5, extra="nope")).status_code == 422
    blank = await review(seeded, a, done, 4, comment="   ")
    assert blank.status_code == 201 and blank.json()["comment"] is None
    assert (await review(seeded, a, done, 5)).status_code == 409  # once per appointment


async def test_managers_can_hide_a_review_and_publish_it_again(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await completed(seeded, mei, 10)
    rid = (await review(seeded, await login(seeded, PHONE_A), bid, 2, comment="Too cold")).json()["id"]
    mod = lambda status, h=None: seeded.client.patch(f"/v1/admin/reviews/{rid}", json={"status": status}, headers=h or seeded.admin)  # noqa: E731
    assert (await mod("hidden")).json()["status"] == "hidden"
    assert await public_reviews(seeded) == [] and await summary(seeded) == {"count": 0, "average": None}
    listing = await seeded.client.get("/v1/admin/reviews", params={"status": "hidden"}, headers=seeded.admin)
    assert [x["id"] for x in listing.json()] == [rid] and listing.json()[0]["comment"] == "Too cold"
    assert (await seeded.client.get("/v1/admin/reviews", params={"status": "published"}, headers=seeded.admin)).json() == []
    assert (await mod("published")).status_code == 200 and len(await public_reviews(seeded)) == 1
    assert (await mod("hidden", await seeded.token("staff"))).status_code == 403
    assert (await mod("banned")).status_code == 422
    assert (await seeded.client.patch(f"/v1/admin/reviews/{uuid.uuid4()}", json={"status": "hidden"}, headers=seeded.admin)).status_code == 404
    assert [e["target_id"] for e in await audit(seeded) if e["action"] == "PATCH /v1/admin/reviews/{review_id}"][0] == rid
    assert (await seeded.client.get("/v1/admin/reviews", headers=await seeded.token("staff"))).status_code == 403


async def test_reviews_are_listed_newest_first_and_summarised_to_one_decimal(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    assert await summary(seeded) == {"count": 0, "average": None}
    ids = []
    for hour, (rating, phone) in zip((9, 10, 11), ((5, PHONE_A), (4, PHONE_B), (4, "+60125550003")), strict=True):
        bid = await completed(seeded, mei, hour, phone)
        await review(seeded, await login(seeded, phone), bid, rating)
        ids.append(rating)
    assert await summary(seeded) == {"count": 3, "average": 4.3}  # 13 / 3 = 4.33...
    assert [x["rating"] for x in await public_reviews(seeded)] == [4, 4, 5]  # newest first
    assert [x["rating"] for x in await public_reviews(seeded, limit=1, offset=1)] == [4]
    assert len(await public_reviews(seeded, limit=1000)) == 3 and await summary(seeded, resource_id=str(mei)) == {"count": 3, "average": 4.3}


async def test_a_restaurant_review_belongs_to_the_business_not_to_a_staff_member(client: httpx.AsyncClient, otp: Any) -> None:
    rest = await make_restaurant(client)
    r = await rbook(rest, at(rest.day, 19), 2, customer={"name": "Aisha Karim", "phone": PHONE_A})
    bid = r.json()["id"]
    assert (await rest.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "completed"}, headers=rest.admin)).status_code == 200
    out = await review(rest, await login(rest, PHONE_A), bid, 4)
    assert out.status_code == 201 and out.json()["resource_id"] is None


async def test_erasing_a_customer_clears_their_review_text_but_keeps_the_rating_anonymously(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await completed(seeded, mei, 10)
    me = await login(seeded, PHONE_A)
    await review(seeded, me, bid, 3, comment="The therapist was rude to Aisha Karim")
    await ask(seeded, me, "access")
    await ask(seeded, me, "erasure")
    access, erasure = sorted(await requests(seeded), key=lambda r: r["kind"])
    export = (await seeded.client.post(f"/v1/admin/data-requests/{access['id']}/complete", headers=seeded.admin)).json()["export"]
    assert [(r["rating"], r["comment"]) for r in export["reviews"]] == [(3, "The therapist was rude to Aisha Karim")]  # part of what is held
    assert (await seeded.client.post(f"/v1/admin/data-requests/{erasure['id']}/complete", headers=seeded.admin)).status_code == 200
    kept = await public_reviews(seeded)
    assert [(x["rating"], x["comment"], x["reviewer"]) for x in kept] == [(3, None, "Anonymous")]
    assert await summary(seeded) == {"count": 1, "average": 3.0}  # the rating still counts


async def test_reviews_are_private_to_their_tenant(client: httpx.AsyncClient, otp: Any) -> None:
    a, b = await make_seed(client), await make_seed(client)
    bid = await completed(a, await a.add_staff("Mei"), 10)
    await review(a, await login(a, PHONE_A), bid, 5)
    assert await public_reviews(b) == [] and await summary(b) == {"count": 0, "average": None}
    assert (await b.client.get("/v1/admin/reviews", headers=b.admin)).json() == []
    stranger = await login(b, PHONE_A)  # the same phone number is a different customer at another business
    assert (await review(b, stranger, bid, 5)).status_code == 404
