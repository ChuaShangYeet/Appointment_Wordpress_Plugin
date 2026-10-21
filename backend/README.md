# JustSimple Booking Engine

Multi-tenant **headless booking backend** (FastAPI · SQLAlchemy 2 async · PostgreSQL 16 + PostGIS · Redis · Alembic): the back end of the JustSimple WordPress plugin, and usable on its own. One deployment serves
many businesses (tenants), each isolated by Row-Level Security, and five kinds of business: **staff-based** (spa, clinic), **restaurant**, **salon** (with on-the-spot add-ons), **automotive workshop** and **field service / professionals**
(on-site visits). Running it for real customers: see *Production setup*.

**Where things stand** (the numbers are from the last full runs; "fake" = tested only against a stand-in for the real service):

| | |
|---|---|
| Built and tested | the free plan's one-website rule (website required at sign-up, origins derived from it, a 30-day wait between address changes, site / domain / key limits); the availability engine and the database-enforced no-double-booking guarantee; all five business types; customer sign-in (phone code, emailed link); cancel and reschedule (customer and admin); customer messages, reminders, review invitations, staff alerts; outgoing signed webhooks; plans and limits (with limits for one business alone, and warnings before a limit); countries, time zones and Australian daylight saving; self-service sign-up (throw-away email addresses refused); owner sign-in by email and lost-key recovery; audit log and PDPA tools; rate limits; start-up safety checks; JSON logging; security headers; API-key list and revoke |
| Built, tested against a **fake** only | WhatsApp Cloud API and Twilio SMS, SMTP email, Google Geocoding, Cloudflare Turnstile (bot check), Google Calendar sync (OAuth and events), Sentry |
| Never run | `docker-compose.yml` and the Docker images (including Redis with its password), a real Redis, the PostGIS 3.4 image, password and TLS login to the database, a reverse proxy |
| Not built | the WordPress plugin, the booking widget and the admin dashboard (they come last); payments, billing and trials; see each section's *Not built* note and *Known limits* |
| Checks | 2,590 tests (ten consecutive clean full runs on 2026-10-19), every feature mutation-tested, `mypy app` clean, migrations 0001-0032 round-tripped with `alembic check` |

Weekdays use the blueprint convention **0 = Sunday … 6 = Saturday**.

## Quick start (development)

```bash
cp .env.example .env          # then fill in JWT_SECRET, PDPA_ENCRYPTION_KEY, PLATFORM_ADMIN_TOKEN
docker compose -f docker-compose.dev.yml up -d   # PostgreSQL+PostGIS and Redis on localhost (one superuser role: development only)
python -m venv .venv && .venv/Scripts/activate   # (source .venv/bin/activate on Linux/macOS)
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload                    # docs at http://localhost:8000/docs (only when ENVIRONMENT is development or test: see *Security review*)
```

`.env.example` says `ENVIRONMENT=development`. **That is the one line to change for a real deployment**: the setting defaults to `production` when it is missing, and `development` / `test` switch the start-up
safety checks off and let sign-in codes be written to the log instead of sent.

`docker-compose.yml` (no `-f`) is the production-shaped stack, with three database roles and a separate migration step: see *Production setup*.

Migrations: `alembic upgrade head` · `alembic downgrade base` · new: `alembic revision --autogenerate -m "msg"`
(the initial migration runs `CREATE EXTENSION IF NOT EXISTS btree_gist` first).

## Tests

Pure-logic tests need nothing. DB tests need a **disposable** PostgreSQL 16 database
(the schema is dropped and recreated every run):

```bash
TEST_DATABASE_URL=postgresql+asyncpg://booking:booking@localhost:5432/booking_test pytest -q
```

Without a reachable database those tests are skipped (not faked). The OTP and availability-cache tests use `fakeredis`, so
no Redis is needed; the test suite runs with the availability cache off (`AVAILABILITY_CACHE_TTL_SECONDS=0`) except in
`tests/test_availability_cache.py`.

On-site radius checks use **PostGIS**, which only a superuser can create. To run those tests also set
`TEST_ADMIN_DATABASE_URL` (a superuser on the same test database); the schema is then rebuilt with PostGIS
enabled. Without it the PostGIS tests are skipped. The `postgis/postgis:16-3.4` compose image already has it
and runs migrations as a superuser; migration `0005` enables the extension when it can and otherwise
continues, in which case an on-site availability/booking request answers 503 `postgis_unavailable`.

How the stand-ins work: `fakeredis` replaces Redis; the delivery providers are replaced by a recording provider (`mail` fixture); the HTTP services (WhatsApp, Twilio, Google, Cloudflare)
by `httpx.MockTransport` classes that behave like the service, including its failures (`tests/test_gateways.py`, `tests/test_geocoding.py`, `tests/test_captcha.py`, `tests/test_calendar.py`). A passing test therefore says our code does what we
believe the service documents - not that the real service agrees. A run that suddenly takes many times longer than the others means the machine slept (connections die): repeat it.

## How the guarantees work

* **No double booking** — `booking_allocations` has
  `EXCLUDE USING gist (resource_id WITH =, period WITH &&) WHERE (state IN ('held','active'))`.
  The service inserts inside a SAVEPOINT; SQLSTATE `23P01` becomes HTTP 409 with refreshed `slots`.
  Ranges are half-open `[start,end)`, so back-to-back bookings are fine. When several requests insert
  overlapping rows at the same instant PostgreSQL may instead abort one as a deadlock victim (`40P01`);
  that loser is treated exactly like a lost race (re-decided, or 409), never a 500.
* **Pending holds** — a `pending` booking holds its slot until `hold_expires_at`. Expired holds are
  swept inside every reservation/transition (`release_expired_holds`), so correctness does not depend
  on a worker; the background sweep (see *Background sweep*) also expires them for quiet tenants.
* **Tenant isolation** — every tenant table has `tenant_id` and an RLS policy
  (`ENABLE` + `FORCE ROW LEVEL SECURITY`) keyed on `app.tenant_id`, set per transaction by
  `set_config('app.tenant_id', …, true)` (parameterised `SET LOCAL`). `tenants`, `sites`, `api_keys`
  are intentionally outside RLS because they are read to *resolve* the tenant.
  **Production note:** superusers and `BYPASSRLS` roles ignore RLS, and so can a table's owner who switches it off. Outside development and tests the API and the worker
  therefore *ask the database* (`row_security_active`) whether the policies apply to their role and refuse to start if not: see *Production setup*.
* **PDPA** — `health_notes` is stored as AES-256-GCM ciphertext (`PDPA_ENCRYPTION_KEY`, 32 bytes base64),
  with the tenant id bound as AAD. Only owner/manager can read it back (`GET /v1/admin/bookings/{id}`).

## Restaurant (Phase 2)

Tenant `business_type: restaurant`. Setup, all under `/v1/admin` (manager+):

1. `PUT /policies` (make it default) and `PUT /operating-hours` — reservations only occur inside opening hours.
2. `PUT /time-slot-settings` — `flexible` (a start every `slot_interval` from opening until
   *closing − slot_duration*) or `fixed` (from `first_slot_time` every interval to `last_slot_time`),
   plus `buffer_minutes`. A table is occupied for `slot_duration + buffer`.
3. `POST /resources` with `type: "table"`, `capacity`, and `attributes` such as
   `{"location": "indoor", "is_vip": true, "minimum_spend": 300}`.
4. Optional `PUT /table-open-rules` — how many tables of a class (`normal`/`vip`) are open for online
   booking per weekday or specific date (a date rule overrides a weekday rule; must not exceed the
   number of active tables of that class).

Customers call `GET /public/availability?date=&party_size=&table_type=&allow_separate_seating=` and
`POST /public/bookings` (no `service_ids`). Rules: the **smallest single table** that fits; if
`allow_separate_seating`, the **fewest tables** whose capacities cover the party (ties: least spare
capacity, then name); VIP and normal tables are **separate pools, never combined**; a VIP slot carries
`minimum_spend` and the booking is refused (422 `minimum_spend_not_accepted`) unless the customer accepted it.
The client should send the amount it showed as `accepted_minimum_spend`. Tables are decided when the request
arrives, not when the slot was shown, so another booking in between can leave only a dearer room (say the
RM 500 room is taken and only the RM 1,200 one is left). The request is then refused with the new amount in
`minimum_spend` (and the accepted one in `accepted_minimum_spend`), never booked at an amount the customer
did not see. A cheaper table is fine, and `minimum_spend_agreed` records what the tables need. The older
yes/no `accept_minimum_spend: true` still works and means *any amount*, so a client that sends only the
yes/no can still be seated in a dearer room; when both are sent, the number decides. The open-table cap limits *simultaneously* reserved tables, and is
enforced under a per-tenant-per-day advisory lock, so parties that pick different tables cannot race
past it. Per-table overlap remains guarded by the GiST constraint.

**Customer table choice** is a per-tenant switch, `allow_table_selection` on `PUT /time-slot-settings`
(default **off**: the system assigns, and a supplied `resource_id` is rejected with 422). When on,
`resource_id` names a table: it must exist (404 otherwise), belong to the requested `table_type`
class, be free and large enough (else the normal `slot_unavailable`), and is never combined with other
tables (`allow_separate_seating` + `resource_id` is a 422). Opening hours, the open-table cap, VIP
minimum spend, locking and the exclusion constraint apply exactly as for auto-assigned tables.
`GET /public/availability?resource_id=` then lists only that table's free slots.

## Automotive (Phase 3)

Tenant `business_type: automotive`. Setup under `/v1/admin` (manager+): opening hours and a policy as
usual; mechanics = `staff` resources with shifts; bays = `POST /resources` with `type: "bay"`;
`POST /supported-vehicles` (make/model, optional `year_from`/`year_to`); services, whose
`resource_ids` may mix mechanics and bays (a service with no links of a given type accepts any
resource of that type); optional `POST /products` + `PUT /services/{id}/parts` for parts.

* **Double constraint** — every segment of a job needs a qualified mechanic **and** a supporting bay,
  both free for the whole segment; each segment writes two allocation rows. Mechanics obey shifts and
  breaks; bays obey opening hours. A slot exists only if both are available.
* **Time stacking** — several services run back to back (each `duration + buffer`). Segments are
  independent in time, so different mechanics/bays may serve different segments; the same mechanic
  and bay are kept across segments when possible. An optional named mechanic (`resource_id`) must be
  qualified for and free during every segment; the bay is always auto-chosen.
* **Vehicles** — a booking needs `vehicle_id` (registered, owned by the booking customer) **or** an inline
  `vehicle {plate_no, supported_vehicle_id, year}` that is registered on the spot. The model must be on
  the workshop's supported list (and inside its year range, if limited): else 422 `incompatible_vehicle`.
  `GET /public/supported-vehicles` lists what is accepted.
* **Lead-time disclosure** — services needing `available_on_order` parts return `lead_time_notice` from
  `GET /public/availability`; the booking is refused (422 `lead_time_not_acknowledged`) unless
  `acknowledge_lead_time: true`, which is recorded on the booking.
* **waiting-for-parts** — `in-progress → waiting-for-parts` truncates the job's allocations at *now*:
  work already done stays on record, the running segment is cut at now, later segments are released.
  `POST /admin/bookings/{id}/resume {start_at, resource_id?, service_ids?}` allocates the remaining
  services again (admin action: notice/window rules are skipped, `start_at` must be in the future) and
  moves the job back to `confirmed`. Leaving the state any other way (except `cancelled`) is refused.

* **Parts on the job order** (`app/services/parts.py`; blueprint `purchased_parts`; also for **professional** businesses, whose appointments have the same list) —
  `PUT /admin/bookings/{id}/parts {"parts": [{"product_id", "quantity"}]}` makes the list exactly what was sent (an empty list clears it; each part once; at most 50 lines; strict
  whole-number quantities 1-1000), `GET .../parts` reads it. Staff, so a mechanic tied to a resource may only touch their own jobs (403 otherwise); allowed while the job is `confirmed`,
  `in-progress` or `waiting-for-parts` (409 otherwise). A line is priced when it is added and keeps that price when its quantity changes. Nothing leaves the shelf while the job runs:
  the shelf is only *checked* - a part whose quantity is more than the shelf holds is flagged `needs_ordering` (the shelf quantity decides, not the *Available on Order* label), which is the
  mechanic's cue to set the job `waiting-for-parts` (the customer is emailed) and, once the parts arrive and the stock is updated (`PUT /admin/products/{id}`), to `/resume` it. The response also
  gives each part's `in_stock`, lead time and the job's parts `total`. **Completing the job deducts the stock** (`inventory.deduct_stock`): never below zero, a part reaching zero shows *Out of Stock*,
  an *Available on Order* label is left alone; cancelled and no-show jobs take nothing. Completing is not blocked by a stale stock count. Salon retail products keep their own flow
  (`/items`; there completing also locks in the duration - for workshops and professionals the duration is not computed, a professional reports it with `/actual-duration`).
  Edits and a completion queue on the booking row, so a part is never added after the stock was deducted. Every change is in the audit log (route and booking, not the list). Not built: reserving
  stock for open jobs (two open jobs can claim the last unit; the second completion leaves the shelf empty), ordering parts from a supplier, quantities received, and mentioning the parts in the waiting-for-parts email.

Decisions run under a per-tenant-per-day advisory lock, so concurrent requests decide one at a time;
the GiST constraint remains the backstop, and a lost race is re-decided (up to 5 attempts) rather than
reported while another bay/mechanic is free.

## Salon / barber (add-ons, upsell, inventory)

Tenant `business_type: salon` (spa/clinic keeps the plain staff allocator). Services carry `is_addon`.

* **Cart** — exactly one *base* service plus optional *add-ons* (`service_ids` on the booking). Add-ons
  extend the interval: the stylist is reserved for base + add-ons back to back. Stylist qualification
  follows the **base service only**; with no stylist named, the least-loaded qualified stylist is assigned.
* **Strictest policy** (all verticals) — a cart whose services have different booking policies (a service
  without one uses the tenant default) gets the strictest combination: longest booking and cancellation
  notice, shortest booking window. Two judgment calls the blueprint leaves open: any `pending` policy makes
  the booking `pending`, and the shortest `pending_timeout` applies. The snapshot records `merged_from`.
* **On-the-spot upsell** — once an appointment is `in-progress`, `POST /admin/bookings/{id}/items
  {service_ids, products}` adds add-ons and/or retail products to the ticket. An add-on **stretches the
  reservation**; if that would run into the stylist's next appointment, the response lists it under
  `conflicts` (customer, times, services) as an alert, the add-on stays on the ticket, nothing is forced into
  the other customer's time, and both appointments get `schedule_conflict`. The booking row is locked for
  the operation so concurrent upsells apply one after another, and a booking slipping into the gap between
  check and write is reported as a clash rather than an error. **The stylist is told** (see *Schedule collisions: telling the stylist*).
* **Products** — `GET /public/products` is the browse-only catalog (a stock *status*, never counts).
  A product goes on the ticket at today's price (snapshotted) after a stock check.
* **Completion** — marking the appointment `completed` deducts the ticket's stock (atomically, never below
  zero; a product reaching zero shows `out_of_stock`, an `available_on_order` flag is left alone) and locks in
  the final duration (base + all add-ons) as `actual_duration_minutes`.

## Field service / professionals (Phase 4)

Tenant `business_type: professional`. Setup under `/v1/admin` (manager+): opening hours, a policy,
professionals (`staff` with shifts), services with `service_category` (`inspection` | `standard`),
`location_type` (`in_office` | `virtual` | `on_site`) and `travel_buffer_minutes`, and
`PUT /business-location {latitude, longitude, service_radius_km}` (or `address`, or `maps_url`: see *A Google Maps link as the location* below).

* **A Google Maps link as the location** (2026-10-19, `app/services/maps_links.py`). `PUT /admin/business-location {maps_url, service_radius_km}` takes the link Google Maps gives for the business, for any kind of business. A full link (browser address bar) carries the point: the place pin `!3d<lat>!4d<lng>` wins, then `q=`/`query=`/`ll=`/`center=`/`destination=` holding `lat,lng`, then the map centre `/@lat,lng`; read from the text, nothing is called. A **share link** (`https://maps.app.goo.gl/...`, or `goo.gl/maps/...`) is followed redirect by redirect (at most 5, `MAPS_LINK_TIMEOUT_SECONDS` each, default 5, no page is read), **only through Google Maps hosts over HTTPS** (`maps.app.goo.gl`, `goo.gl/maps`, `google.<tld>/maps`, `maps.google.<tld>`); Google's consent page is seen through via its `continue` link; a hop anywhere else stops it. A link that only names a place (`?q=Serene+Lotus+Spa`, `/maps/place/<name>`, or a share link leading to one) is looked up like a typed address (*Typed addresses*: same precision rule, needs the geocoding provider). Not a Google Maps link: 422 `maps_link_invalid` (nothing is called); no point or place found: 422 `maps_link_unreadable`; Google unreachable: 503 `maps_link_failed`. On success the link is also saved as the profile's `google_maps_url` (made if there is no profile yet), which `GET /public/business` gives out for directions, and the answer carries `maps_url`. Exactly one of coordinates, `address` and `maps_url` may be sent. Tested against a fake Google only: no real share link has been followed yet.

* **One service per booking.** In-office and virtual services carry no travel time.
* **Radius check (PostGIS)** — an on-site service needs the customer's coordinates: `latitude`/`longitude`
  on `GET /public/availability`, and an `address {line1, latitude, longitude, ...}` (new) or `address_id`
  (saved, must belong to the customer) on the booking. Distances are geodesic (`geography`); outside the
  radius gives 422 `outside_service_area` with the distance. Coordinates come from the client's map pin, **or the
  address is sent as typed and the server looks it up** (see *Typed addresses*).
* **Typed addresses** (`app/services/geocoding.py`; blueprint §11.2 "select or enter an address", §15 "Google Maps: geocoding"). An address with **no coordinates**
  (`address {line1, city?, postcode?}` on a booking, `POST /public/me/addresses`, or `address` instead of coordinates in `PUT /admin/business-location`) is turned into
  coordinates through a provider: `GEOCODING_PROVIDER=google` with `GOOGLE_MAPS_API_KEY` (Geocoding API; each lookup leans towards the country of the business it is for, see
  *Countries*; `GEOCODING_TIMEOUT_SECONDS`). The text sent is `line1, postcode city`. **Only a precise result is accepted**: Google's `ROOFTOP`, `RANGE_INTERPOLATED` or
  `GEOMETRIC_CENTER` with a full match; an area (`APPROXIMATE`) or a partial match is 422 `address_not_precise` (give a fuller address or drop a pin), nothing found is 422
  `address_not_found`, no provider is 422 `geocoding_unavailable` (send the coordinates), and the provider being down, over quota, refusing the key or answering rubbish is 503
  `geocoding_failed`. A refused lookup makes no booking and saves no address. Coordinates sent with the address are used as they are and nothing is looked up; sending only one of the two is 422.
  `POST /public/geocode {address}` (businesses that go to the customer only) is for the booking page: it returns the point, how the provider wrote the address, `exact`, and - when the business
  has a service area (and PostGIS) - `within_service_area` and `distance_km`, so the customer can confirm before choosing a time; a vague match comes back with `exact: false`. The page then carries
  the coordinates into the availability and booking calls so the lookup is paid for once. It has its own rate-limit bucket, `geocode` (10 a minute per address, 120 per business), because every call is paid for.
  Saved addresses: changing `line1`, `city` or `postcode` without new coordinates looks the new text up and moves the pin (when a provider is configured; without one the pin stays as it was);
  a change that cannot be placed leaves the address as it was. The business's own location accepts an address in place of coordinates and shows how the provider wrote it (`formatted_address`),
  so it can be checked. **Privacy:** the typed address is personal data sent to Google to be looked up: name it as a sub-processor. Nothing is cached, logged or put into an error (the key is in the request URL,
  so only the exception's type and the provider's status are ever logged). Not built: caching lookups, a daily quota, autocomplete, a map link generated from the coordinates, another provider.
* **Travel buffer** — an on-site visit reserves `travel_buffer_minutes` *before and after* the appointment
  on the professional's schedule; the whole padded block must fit inside their shift and hours. The
  customer-visible `start_at`/`end_at` exclude the travel time. Travel is modelled per visit (no route
  chaining between consecutive visits). A business that would rather keep no travel time sets 0 and moves the next visit when
  a job runs over (*Admin rescheduling*; the admin's free times for an on-site visit are looked up at its address, see *Where the visit goes*).
* **Price after inspection** (2026-10-21, migration 0033, any kind of business) — `price_after_inspection: true` on a service
  (`POST|PATCH /admin/services`, shown on `GET /public/services`) means the business tells the customer the price once it has seen the
  job (a car nobody knows what is wrong with, a house's wiring). `price` is then what is known up front: 0 when nothing is, or a
  starting price such as an inspection fee. So `price: 0` without the flag still means free. `PATCH /admin/services` can no longer
  empty a setting that always holds a value (`name`, `duration_minutes`, `buffer_minutes`, `price`, `price_after_inspection`, `active`,
  `is_addon`, `location_type`, `travel_buffer_minutes`): sending `null` for one is 422 (it was a 500); leaving it out keeps it.
* **Where the visit goes** (2026-10-21) — `GET /admin/bookings/{id}` carries `visit_address {line1, city, postcode, latitude, longitude}`
  for an on-site visit, to every admin role (a technician sees only their own jobs). The pin is what the admin page needs to look up free
  times when the visit has to move. `null` for other bookings, and once the customer is erased.
* **Auto-assignment** — the least-loaded qualified professional, or a named one (`resource_id`).
* **Virtual appointments** — a service with `location_type: "virtual"` is an online meeting. **The business brings its own meeting service** (decided 2026-10-19: the system
  makes no video rooms). Each consultant can save their own room once (`meeting_link` on the staff member: `POST|PATCH /admin/resources`, a Google Meet, Zoom or Teams
  `http(s)` link; `null` removes it; managers see it in the admin list, never on the public page). When a virtual appointment is confirmed (at once, or when a pending one is
  approved) and has no link yet, it takes the room of the consultant it is allocated to, so the confirmation email carries it. `PATCH /admin/bookings/{id}/meeting-link`
  sets a link for one booking by hand and always wins; on a confirmed booking a new link is emailed to the client ("Your online meeting link", event
  `booking.meeting_link_ready`, which webhooks can subscribe to; setting the same link again sends nothing, and a cancelled booking's queued email is not sent).
  The confirmation, reminder and "moved" emails carry `$meeting_line`; the link is in the Google Calendar event's description too. Only professional businesses get links.
  Not done: changing a consultant's saved room does not update bookings that already took the old one, and a confirmed appointment that moves to another consultant
  keeps its link (set the new one by hand). `app.services.meetings.set_meeting_provider` remains a seam for a video service integrated later (none ships).
* **Two-step workflow** — `POST /admin/bookings/{id}/follow-up {service_id, start_at}`: once an
  *inspection* is in progress (or completed) a *standard* service is booked for the same customer and
  address, linked by `parent_booking_id`, confirmed at once, with the radius re-checked.
* **Overruns** — `PATCH /admin/bookings/{id}/actual-duration {minutes}` on an in-progress job. If the extra
  time is free, the reservation is stretched; if it collides with the next appointment nothing is moved and
  both bookings are flagged `schedule_conflict` (filter with `?schedule_conflict=true`, clear with
  `POST /admin/bookings/{id}/resolve-conflict`). Nothing is auto-cancelled.

## Customer login and cancellation

* **Login** — `POST /public/auth/otp {phone}` sends a 6-digit code; `POST /public/auth/otp/verify {phone, code}`
  returns a customer JWT bound to the tenant (email magic link: see *Customer identity*). Codes are stored hashed in Redis, single use, expire after
  `OTP_TTL_SECONDS`, allow 5 wrong guesses, and only one code can be requested per
  `OTP_RESEND_COOLDOWN_SECONDS` (default 60) per phone; without that limit, asking for a fresh code would have
  reset the guess counter. Customers are keyed by phone, so a guest who booked without an account manages
  their bookings by logging in with **the same phone number they booked with** (any spelling of it: see *Customer identity*).
* **`GET /public/me/bookings`** — the customer's own bookings, soonest first (`?include_past=true` for
  history), with `can_cancel` and `cancel_deadline`; no health notes, policy internals or staff flags.
* **`POST /public/bookings/{id}/cancel`** — only the owner (someone else's booking is a plain 404). A
  **pending** request can be withdrawn any time; a **confirmed** booking only until `start - min_notice_cancel`
  of the policy *snapshotted on that booking* (422 `too_late_to_cancel` with the deadline; the deadline itself
  is still allowed). Started or finished bookings (`in-progress`, `waiting-for-parts`, `completed`, ...) need the
  business. The slot is released immediately.
* **Concurrency** — status transitions lock the booking row (`SELECT ... FOR UPDATE` with a forced refresh),
  so an approval racing a cancellation, or two cancellations, run one after the other and can never leave a
  status that disagrees with the booking's allocations. Overrun reports, resume and upsell lock the same way.

## Notifications (Phase 5)

What exists: the **provider seam**, an **SMTP** email provider, a **WhatsApp Cloud API** provider and a **Twilio SMS** provider, **booking messages driven by
the outbox**, **sign-in messages**, editable **templates** and a **log**. Google Calendar sync has a section of its own (*Google Calendar sync*). Online meetings use the business's own meeting service (*Virtual appointments*). No live message
has ever been sent: the gateways were exercised against a fake HTTP transport only.

* **WhatsApp Cloud API** (`WhatsAppCloudProvider`; `WHATSAPP_ACCESS_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID`, both needed). A business can only *start* a conversation with a
  Meta-approved template, so a message with an `external_template` is sent as that template (`POST /{version}/{phone_number_id}/messages`, bearer token; body variables in
  order from the template's `params`, and a copy-code button value for authentication templates); a message without one is sent as plain text, which Meta delivers only
  inside the 24 hours after the customer last wrote - otherwise the API's refusal (its code and text, never the request) lands in the notification log. **Sign-in codes by
  WhatsApp need `WHATSAPP_OTP_TEMPLATE`** (the approved template's name; the code is its one variable; `WHATSAPP_OTP_BUTTON=false` for a template without a copy-code button);
  `WHATSAPP_TEMPLATE_LANGUAGE` is the language it was approved in. Template variables are cleaned the way Meta demands (no line breaks or tabs, no runs of four spaces, never empty).
  One platform-wide WhatsApp number: per-business WhatsApp accounts are not built.
* **Twilio SMS** (`SMS_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_FROM` *or* `TWILIO_MESSAGING_SERVICE_SID`; a messaging service wins when both are given).
  Other gateways plug into the same seam.
* One request per attempt, `DELIVERY_TIMEOUT_SECONDS` (10), no retry inside the provider: the outbox back-off retries. Errors report the gateway's status/code/text or only the exception's
  type - never the token, the recipient or the message. The tokens are `SecretStr` settings, so printing the settings does not reveal them.
* **Start-up report**: outside development and tests the API and the worker log an error for each gap (`delivery.configuration_problems`): no email provider, no provider for
  `OTP_CHANNEL` (customers cannot sign in by phone), WhatsApp without an OTP template, `MAGIC_LINK_URL` unset. These are errors in the log, not refusals to start (email magic links work without WhatsApp).

* **Providers** (`app/services/delivery.py`): one `DeliveryProvider` per channel (`email`, `sms`, `whatsapp`), registered with
  `set_provider`. Nothing is registered by default, so an unconfigured channel **fails visibly** (`NoProviderError` in the log, retried)
  instead of pretending to send. `configure_providers` (run at API and worker start-up) registers an **SMTP email provider** when `SMTP_HOST`
  is set (`SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS`), and in `ENVIRONMENT=development` a provider that just logs
  every message, so codes can be read off the console. Header injection is refused. The SMTP provider has only run against a fake server.
* **Booking messages** (`app/services/notifications.py`): `python -m app.worker` plugs `NotificationPublisher` into the outbox, so the
  sweep turns `booking.created` (only when the booking starts *pending*; one that starts confirmed is announced by `booking.confirmed`),
  `confirmed`, `rejected` (including a hold that ran out), `cancelled`, `rescheduled` (old and new time), `waiting_for_parts` and `completed`
  into messages. Which channels: the business's active templates for that event, plus the built-in **email** default unless the business
  overrode or switched it off. SMS and WhatsApp are therefore opt-in per event (a template must exist).
* **Exactly-once-ish**: the outbox is at-least-once and a crash between a provider accepting a message and the log being written sends it
  again - but a re-delivered event never re-sends a channel that already went out (unique `(event, channel)` log row), a failing channel is
  retried **alone** with the outbox back-off, and after `NOTIFY_MAX_ATTEMPTS` (6) it stays `failed` in the log and the event is released.
* **Refused messages** (2026-10-14, migration `0030`). A booking's messages go out in order, so a channel that kept failing used to hold back the next message about the same
  booking for the whole retry run (about 15 minutes, six gateway calls): a number that is not on WhatsApp delayed the "your booking is confirmed" email. When the provider says it
  will **never** take *this* message, the log row becomes `refused` (a status of its own; `GET /admin/notifications?status=refused`), it is not retried, it does not count
  against the month's WhatsApp / SMS allowance, and the event moves on at once. Which answers are "never" is deliberately narrow, because a wrong guess loses a message:
  SMTP refusing the recipient with 5xx codes only (a 4xx such as a full mailbox is retried); WhatsApp Cloud API codes 131026 (undeliverable), 131047 (outside the 24-hour window),
  131049 (Meta's per-user limit) and 131021 (recipient is the sender); Twilio codes 21211, 21214, 21217, 21610 (unsubscribed), 21612 and 21614 - all only on a 4xx. Everything
  else (wrong password, rate limit, 5xx, a network error, a template or access-token problem, a number outside a test list) is the business's or the provider's problem, stays
  `failed` and is retried, and still keeps the booking's later messages waiting its turn. The same applies to the alerts to the business and to the stylist. A sign-in code is not
  affected: the person is waiting, so any failure is a 503 and the row is `failed`. The code lists come from the providers' documentation and a fake HTTP transport; no live
  message has been sent.
* **Templates**: `GET /admin/notification-templates`, `PUT|DELETE /admin/notification-templates/{event}/{channel}`,
  `GET /admin/notification-placeholders` (manager+). Placeholders are `$name` / `${name}` from a fixed list (`business_name`, `customer_name`,
  `services`, `date`, `time`, `end_time`, `party_size`, `hours_before`, `previous_date`, `previous_time`, `review_link`, `review_prompt`, `meeting_link`, `meeting_line`); unknown ones and stray `$` are refused when saving,
  nothing is evaluated, and what a customer typed (their name) has control characters stripped, so it cannot add email headers or lines.
  Emails need a subject, SMS has none, WhatsApp may name a Meta-approved `external_template` (its variables are passed in the order
  customer name, services, date, time).
* **Log**: `GET /admin/notifications?booking_id=&status=&event=` (manager+): event, channel, masked recipient (`a***@example.com`,
  `+60****0001`), outcome, provider, error, attempts. **No message text is stored**, and erasing a customer deletes their rows.
* **Sign-in messages** are sent directly (the secret must not sit in an outbox event): the OTP code over `OTP_CHANNEL` (`whatsapp` default, or `sms`),
  the magic link by email (`MAGIC_LINK_URL` with `{token}`, e.g. `https://shop.example.com/login?t={token}`; unset = the email carries the bare token,
  because the front end's sign-in page is not defined yet). If an OTP cannot be sent the code is discarded, the cool-down released and the request answers
  503 `delivery_failed`; a failed magic-link send still answers 202 (the answer must not reveal whether the address exists), leaves no usable token, and is
  in the log. Failures are logged in their own transaction so they survive the request's rollback.
  A code costs a WhatsApp / SMS message from the plan's allowance (see *Plans and limits*), so it is **counted before it is sent**: a log row with status `sending` (and a `sent_at`) is written and committed first, the gateway is
  called, and the row is then settled to `sent` or - the place is given back - `failed`. A worker killed while a code was with the gateway leaves a `sending` row, which keeps its place (the message may have gone out).
* **Sweep order**: the sweep now commits its housekeeping first and works off the outbox in a second transaction. A publisher that opens
  its own sessions (this one does) deadlocks against the row locks the housekeeping holds on the bookings it flags - found by a test that
  hung, and the reason for the split. A failure while publishing therefore no longer rolls back that tick's housekeeping.

## Rate limiting

Blueprint §16 asks for rate limiting on the public endpoints, normally at a gateway; this is the application's own layer, as defence in depth
(`app/core/ratelimit.py`). Redis-backed, two tiers, each a **sliding-window counter** (this window's count plus the last window's, weighted by its overlap):

| Bucket (what falls into it) | per client address | per business |
|---|---|---|
| `auth_send` - `POST /public/auth/otp`, `/public/auth/magic-link` (costs money, can pester people) | 10 / min | 120 / min |
| `auth_verify` - the two `/verify` endpoints | 30 / min | 600 / min |
| `public_write` - every other non-GET `/public/*` (booking, cancel, reschedule, reviews, saved addresses...) | 30 / min | 600 / min |
| `public_read` - GET/HEAD `/public/*` | 300 / min | 6000 / min |
| `admin` - `/admin/*`, `/auth/api-keys` | 600 / min | 1200 / min |
| `token_exchange` - `POST /auth/token` | 300 / min | 120 / min |
| `platform` - `POST /auth/tenants` | 10 / min | - |
| `sync` - `/sync/*` | 300 / min | 600 / min |

* **Per address** runs in a middleware **before any database work**, so a flood of requests with invented API keys costs a Redis round trip, not a query (the
  address limit is applied before the key is even looked at). **Per business** runs once the API key (or admin token) has identified the business: fair use, and a ceiling on a
  runaway script or a leaked publishable key. `/healthz`, the docs, CORS preflights (`OPTIONS`) and unknown paths are never limited.
* **Answers**: `429 {"error": "rate_limited", "retry_after": n}` with `Retry-After` (an upper bound: seconds until the current window ends), `X-RateLimit-Limit`, `-Remaining` and `-Reset`;
  accepted requests on a limited route carry the per-address headers. Refused requests **do not use up quota**, so hammering while refused does not delay recovery. The limiter sits *inside*
  the CORS layer, so a browser can read the 429.
* **Who is the client**: the socket peer, unless `RATE_LIMIT_TRUSTED_PROXIES` = N says N reverse proxies sit in front of the API - then the `X-Forwarded-For` entry N hops from the **right** (the
  left of that header is client-controlled and is ignored; fewer hops than proxies = not trusted). Behind a proxy you **must** set this, or every visitor shares the proxy's address. IPv6 clients are grouped by
  /64; IPv4-mapped IPv6 addresses count as their IPv4 address.
* **Tuning**: `RATE_LIMITS` (JSON) overrides per bucket and tier, e.g. `{"auth_send": {"ip": [5, 60]}, "public_read": {"tenant": [0, 60]}}` (requests, window seconds; 0 requests = that limit off).
  `RATE_LIMIT_ENABLED=false` switches the whole layer off (the test suite does, since every test shares one address).
* **Redis trouble never takes the API down**: errors let requests through (fail open) and Redis is not retried for 10 s. The older per-phone / per-email cool-downs and attempt caps are unchanged and still apply.
* **Not built**: limits per user or token, daily or monthly quotas (such as a cap on SMS sent per business per day), per-business overrides set through the API, a stricter limit for addresses that keep
  being refused, and anything against a distributed flood (that is the gateway's job). Counting is approximate under concurrency (a burst can overshoot by a few requests).

### Outgoing webhooks (for headless sites and the WordPress plugin to react to bookings)

An owner registers URLs and the events they want; the server POSTs a signed JSON message to each when one happens.

* **Admin API** (`/v1/admin`): `GET|POST /webhooks`, `PATCH|DELETE /webhooks/{id}`, `POST /webhooks/{id}/rotate-secret`,
  `POST /webhooks/{id}/test` (one signed `webhook.test` request, answered with the receiver's status; not counted), `GET /webhooks/{id}/deliveries?status=`,
  `POST /webhooks/{id}/deliveries/{delivery}/redeliver`, `GET /webhook-events`. Owners change things, managers can look; at most 10 per business.
  `events` is a list of event types (`booking.created|confirmed|rejected|cancelled|rescheduled|in_progress|waiting_for_parts|completed|no_show|reminder`,
  `shift.updated`, `review.created`, `schedule_conflict`) or `["*"]`. The signing secret (`whsec_...`) is in the creation / rotation response **only**; a ciphertext is stored.
* **What the receiver gets**: `POST` with `Content-Type: application/json` and a body
  `{"id": <event id>, "type": "booking.confirmed", "seq": <increasing number>, "created_at": ..., "tenant_id": ..., "data": {"booking_id", "status", "previous_status", "start_at", "end_at"}}`
  - ids, statuses and times only, never a name, phone, email or note; fetch details with the admin API if needed. Headers: `X-Webhook-Id` (the event; the same for every
  endpoint and every retry - **deduplicate on it**), `X-Webhook-Delivery`, `X-Webhook-Event`, `X-Webhook-Timestamp` (unix seconds, fresh on each attempt) and
  `X-Webhook-Signature: v1=<hex>` where hex = HMAC-SHA256(secret, `"{timestamp}.{raw body}"`) - the scheme of the WordPress sync, so the same check works:
  recompute over the **raw** bytes, compare in constant time, reject timestamps older than a few minutes. Answer with any 2xx quickly; anything else (or a timeout) is a failure.
* **Delivery guarantees**: at least once and **unordered** (a retry of an earlier event can arrive after a later one - use `seq` / `created_at`). The outbox publisher only records what
  each endpoint is owed; the sweep sends it (up to `WEBHOOK_BATCH` = 50 per business per pass, `WEBHOOK_CONCURRENCY` = 10 at a time, `WEBHOOK_TIMEOUT_SECONDS` = 5), retrying each failed
  delivery on its own with the outbox back-off (30 s, 1 min, 2 min ... up to an hour) up to `WEBHOOK_MAX_ATTEMPTS` = 8, then it stays `failed` (an owner can redeliver it).
  An endpoint that fails `WEBHOOK_DISABLE_AFTER_FAILURES` = 15 times in a row is **switched off** (its pending deliveries fail; `PATCH ... {"active": true}` turns it on again),
  and **the business is emailed** at its profile's `contact_email` (the system switching it off queues a `webhook.endpoint_disabled` outbox event in the same transaction; the notification
  publisher sends the email with the usual retries and log row). The email names the host - never the path or query, which can hold secrets - the number of failures and the last error
  (`HTTP 500`), and says how to switch it back on. It is skipped, and logged as skipped, when the business has no contact email, when the webhook has been switched on again or deleted before the
  email went out; switching a webhook off by hand sends nothing. This event is not subscribable and has no editable template.
  A dead URL cannot delay anything else: customer emails for the same booking go out regardless (the two consumers are independent), and the receiver's answer body is never read or stored.
  Finished deliveries are kept `WEBHOOK_LOG_RETENTION_DAYS` = 30.
* **SSRF protection** - the URL is chosen by a tenant, so the server must not be talked into reaching its own network. Enforced when the URL is registered, edited or switched back on **and again on every
  send**: https only; no credentials, fragments or control characters; the host is resolved by us and **every** address must be public (loopback, private, link-local - cloud metadata `169.254.169.254` included -,
  shared 100.64/10, multicast, reserved, IPv4-mapped / 6to4 / Teredo / NAT64 IPv6 forms of those, and the whole reserved `::/8` block (which includes the obsolete `::127.0.0.1` form) and site-local `fec0::/10` are refused);
  the name lookup has a time limit (`WEBHOOK_TIMEOUT_SECONDS`, so a name server that never answers cannot hold the sweep) and a host that does not resolve, resolves too slowly or resolves to a private address gets **one and the same answer**, so registering a webhook cannot be used to probe which internal names exist; the connection goes to the validated address (so a DNS answer that changes between
  check and connect gains nothing), with the real name as `Host` and as the TLS name the certificate must match (checked with a real TLS handshake in the tests); redirects are not followed.
  `WEBHOOK_ALLOW_INSECURE=true` lifts the https / public-address rules for development and must never be set in production.
* **Not built**: a per-endpoint custom header or auth scheme, payload customisation, a warning *before* an endpoint is switched off (only the switch-off itself is announced), alerts to anyone but the
  profile's contact email (the backend stores no owner emails: administrators are WordPress users), IP allow-listing of our own
  sender addresses for receivers, and a signature-verification helper library for the WordPress side (the format above is all a receiver needs).

### Booking reminders

`REMINDER_HOURS` (JSON list, default `[24, 2]`; `[]` switches them off) is the server's default for the lead times before a **confirmed** booking starts. **Each business can choose its own**:
`GET|PUT|DELETE /admin/reminder-settings` (manager+; `PUT {"hours": [48, 6]}`, up to five lead times of 1-720 hours, strict integers, stored longest first; `[]` = this business wants none; `DELETE` = back to the server default; `tenants.reminder_hours`, `NULL` = default).
Changing the times never repeats a reminder a booking already got for a lead time it keeps. Each sweep tick
(`app/services/reminders.py`) finds the bookings whose reminder moment has arrived and writes a `booking.reminder` outbox event plus a
`booking_reminders` row `(booking, kind)` in one transaction; the row's primary key (inserted `ON CONFLICT DO NOTHING`) is what makes a reminder
go out **once**, however many sweeps, retries or crashes there are. Delivery then follows the ordinary notification path - templates for
`booking.reminder` (default: email; SMS / WhatsApp opt-in; `$hours_before` placeholder), log, retries.

* **Not reminded**: bookings that are not `confirmed` (pending, cancelled, ...); a reminder whose moment came *before the booking existed* (booked 20 h
  ahead: no "24 hours before" right after the confirmation); a reminder more than `REMINDER_LATENESS_MINUTES` (60) overdue - a worker that was down, or
  a request approved late, skips it rather than send a stale "tomorrow" (skipped, not deferred); an appointment that has started.
* **Moved bookings**: rescheduling forgets the booking's reminders so the new time gets its own. One already queued for the old time is recognised as
  stale when it is about to be sent (booking moved / cancelled / started) and logged `skipped` with the reason, never sent.
* A restaurant, workshop or practice booking is reminded exactly like a spa one. `TenantSweep.reminders_queued` and the worker summary report the count.
* Not built: per-customer reminder preferences or opt-out, reminders for pending requests, a "your
  appointment is today" digest, and a catch-up for reminders missed by more than the allowance.

## Customer identity: phone numbers, magic link, saved addresses and vehicles

* **Phone numbers have one spelling.** Customers are keyed by `(business, phone)`, so every entry point (booking, OTP request,
  OTP verify, magic-link `phone`) normalises first (`app/core/phone.py`): separators dropped, `+` or `00` = international, one
  leading `0` = national (dropped, then the business's `phone_country_code` - the calling code of its country, fixed at sign-up: see
  *Countries, time zones and daylight saving* - is put in front), otherwise a number starting with the country code is taken as
  international and any other as a national number without a trunk prefix. Stored as `+<digits>`, 7-15 digits; anything else is a
  422. `012-345 0001`, `+60 12 3450001` and `0060123450001` are therefore one customer. This is a spelling fix, **not** a check of
  national numbering plans. Migration `0014` rewrites existing numbers (a number whose normal form already belongs to another
  customer is left untouched; two people are never merged by a migration).
* **Email magic link.** `POST /public/auth/magic-link {email, phone?}` mails a single-use link (through the email provider: see *Notifications*; in
  development the logging provider prints it), `POST /public/auth/magic-link/verify {token}` returns the same customer token as the OTP
  login. The token is 256 random bits, kept hashed in Redis for `MAGIC_LINK_TTL_SECONDS` (900), bound to the business, consumed
  atomically (`GETDEL`), and only the newest link per address works. Verify is a POST so mail scanners that open links do not use
  it up. The answer never says whether the address is registered; requests are limited per address like OTP codes
  (`OTP_RESEND_COOLDOWN_SECONDS`, also for unknown addresses). Emails are matched case-insensitively. An address shared by several
  customers (a family) gets no link unless the request also names the `phone` booked with - ambiguity is never guessed. Erased
  customers cannot sign in this way, even with a link issued before the erasure.
  **What the link opens depends on whether the address is confirmed** (2026-10-13; see the next bullet). Nobody checks an email at
  booking time, and anyone can book with anyone's phone number, so an address typed into a booking says nothing about who owns the
  phone. An address **nobody has confirmed** opens a *limited session*: its token carries the claim `em`, and it can list
  (`GET /public/me/bookings`), cancel, move and review only the bookings made **with that address** (`bookings.contact_email`) - never the
  customer's saved addresses and vehicles, profile, data requests or the customer's other bookings (403 on those, 404 on another
  booking). An address the customer **confirmed** signs in as the whole customer. If several customers confirmed the same address,
  that wins over a booking-only match, and two of them still need the `phone`.
* **An email is the customer's own only after the phone's owner confirms it** (migration `0029`). A public booking never replaces an email
  (or gender) already on the customer's record - before this, booking with another person's phone number and your own email took over their
  record: the emailed link then signed you in as them and their messages (cancelled, reminders) went to you. The email given with a booking
  is kept on that booking (`bookings.contact_email`, lower-cased); the booking's messages go there, else to the customer's *confirmed* email,
  else nowhere (an unconfirmed one may be anyone's; the log says `skipped`, no email address on file). To make an address their own, a customer
  signed in **with the phone code** (or with an address already confirmed) calls `POST /public/auth/email {email}` - a link goes to that
  address (`EMAIL_CONFIRM_URL` with `{token}`; unset = the bare token; 202; 409 if it is already confirmed; limited like sign-in codes;
  503 if it could not be sent; a limited session gets 403) - and following it, `POST /public/auth/email/confirm {token}`, saves the address on
  the customer with `email_verified_at` (replacing whatever was there, which is how someone else's typed-in address is removed). The token is
  hashed in Redis, single use, newest link only, bound to the business. Existing bookings were given their customer's current address as
  `contact_email` so no messages stop, but **no existing address counts as confirmed**: those customers see their bookings through an emailed
  link and sign in with the phone code for everything else, until they confirm. Erasure clears every `contact_email` and the confirmation; the
  access export lists them. **Still open:** the customer's *name* follows the latest booking (a typo fix, but anyone can set it); and where no
  phone provider is configured a customer has only the limited session.
* **`/public/me/addresses`** (`GET`, `POST`, `PATCH /{id}`, `DELETE /{id}`) and **`/public/me/vehicles`** (same; workshops only,
  422 otherwise). Addresses carry the map-pin coordinates, as when booking. A vehicle must be a make/model the workshop services
  (and inside its year range): `incompatible_vehicle`, and the plate is stored normalised and unique per customer (409). The list
  marks a car `serviceable: false` when the workshop has since dropped its model or narrowed the years. Both pick up in a
  booking via `address_id` / `vehicle_id`. Deleting - **and changing** (2026-10-15: a customer could book near, then move the saved address outside the service radius, or swap the
  car on an open job order) - is refused (409) while an *open* booking (pending, confirmed, in-progress,
  waiting-for-parts) uses the row (saying what is already saved is allowed, and a bad edit is still 422 first; save a new address or vehicle instead); otherwise the link is cleared on finished bookings - the rule PDPA erasure follows - and the row
  is deleted. At most 20 of each per customer. Someone else's row is a plain 404.

## Status moves and the clock (2026-10-14)

`PATCH /admin/bookings/{id}/status` no longer lets a booking be marked as started, finished or missed before its time. **`no-show`** needs the start time to have passed.
**`in-progress`** (the customer arrived) and **`completed`** open `STATUS_EARLY_MINUTES` (default 60, 0-1440) before the start, because people arrive early; a booking that is already
`in-progress` can be completed at any time. Before this a booking next week could be completed - which freed its slot and sent "Thank you for visiting us" with the review link - or marked a
no-show. The refusal is `409 invalid_transition` and says when the booking starts, in the business's own time. Cancelling, rejecting and confirming are not timed (they are how a future booking
is decided or ends). There is no override: a booking that really was served early is marked `in-progress` on arrival, and one whose time was wrong is rescheduled. Tests: the rule is switched
off for the rest of the suite by the `no_status_timing` fixture in `tests/conftest.py` (those tests mark tomorrow's bookings without waiting) and switched back on in `tests/test_status_timing.py`.

## Sending a booking twice (Idempotency-Key, 2026-10-14)

`POST /public/bookings` takes an optional **`Idempotency-Key`** header (8-100 characters of letters, digits and `_ . : -`; a fresh UUID per *submission*, kept for that submission's retries). The
first request with a key makes the booking and stamps it with the key and a hash of the whole request (`bookings.idempotency_key`, `idempotency_hash`, migration `0031`; one booking per business and
key, enforced by a partial unique index). The **same request sent again** - a double click, a retry after a time-out - makes nothing and returns that booking with the same body, status `201` and the
header `Idempotent-Replayed: true` (readable by browsers: it is in the CORS expose list). The **same key with a different request** (another time, person, name, note...) is `409`
`idempotency_key_reused`. Two copies arriving at the same moment queue on a lock for the key, so the second finds the first's booking. A request that **failed** (slot taken, outside the window, over
the plan's limit) made nothing, so its key is free to be tried again. A replay is not a booking: it does not use a place in the plan's monthly bookings and sends no second message. A bad key is `422`.
Without the header nothing changes: every call makes its own booking, so a widget should always send one. The replay returns the booking **as it is now** (a booking cancelled in between comes back
cancelled, not rebooked). Deliberately **not** built: guessing duplicates without a key (the same person at the same time) - one phone number can legitimately book two people for the same time
(a couple's massage), so a guess would refuse real bookings. Keys are kept as long as the booking (there is no expiry); the key is the only secret between the two requests, so make it random.

## Customer rescheduling

`POST /public/bookings/{id}/reschedule {start_at, resource_id?, any_available?, accept_minimum_spend?, accepted_minimum_spend?}` moves a customer's own
booking, with the same services, in one transaction. `GET /public/me/bookings` says `can_reschedule`.

* **Who and when**: the owner only (404 otherwise); `pending` or `confirmed` bookings; a confirmed one until `start - min_notice_cancel`
  of the policy *snapshotted on that booking* (422 `too_late_to_reschedule` with the deadline; the deadline itself still allowed). A move is
  a cancel plus a rebook, so it uses the cancellation notice.
* **Where to**: the new time is judged by the booking's own policy snapshot (its minimum notice and booking window - a stricter
  policy introduced later does not apply) and then by the vertical's allocator exactly like a new booking (shifts, opening hours, breaks,
  travel padding, service radius, table caps, ...). The booking's current slot is freed first, so overlapping the old time works.
  Refused moves (409 `slot_unavailable` / `slot_conflict`, 422 ...) change **nothing**: the transaction is rolled back whole.
* **What stays**: services with the duration/buffer they were *booked* with (a later edit of the service does not change a booking;
  items now carry `seq`, the order they were chosen in, so stacked workshop segments keep their order), status, the customer, vehicle,
  address, party, and for a pending booking its original hold deadline (moving does not buy time). **Staff stay the same** unless
  `resource_id` names another or `any_available: true` asks for the least-loaded one (otherwise a customer who chose Mei would be
  silently handed to someone else; if Mei is busy the answer is 409). A restaurant gets a fresh table choice, and a VIP minimum spend that
  differs from the agreed one must be accepted again: `accepted_minimum_spend` (the amount shown; more is refused with the new
  amount, as for a new booking) or the older yes/no `accept_minimum_spend` (any amount).
* **Bookkeeping**: old allocations are `released`, new ones written, review flags cleared and re-checked by the sweep, a
  `booking.rescheduled` outbox event (old and new times, no personal data) is written; no status-history row (the status did not change).
  The booking row is locked, so a move racing a cancellation, an approval or another move runs strictly before or after it.
* **Not done**: a virtual appointment keeps its existing join link (there is no real calendar provider to update); a limit on how
  often one booking may be moved; bookings made before migration `0014` have no item order (`seq` = 0), so a *stacked multi-service
  workshop job from before it* may be re-stacked in a different order on its first move. The business moves bookings with
  *Admin rescheduling* below.

## Admin rescheduling

`POST /admin/bookings/{id}/reschedule {start_at, resource_id?, any_available?, accept_minimum_spend?, accepted_minimum_spend?}` (the body of the customer
endpoint) lets a **manager or owner** move any `pending` or `confirmed` booking of the business: a stylist is ill, the customer phoned.
Staff-role users get 403, even for their own appointments. It is the customer's move (`booking._move_booking`: same services, same
status, one transaction, a refused move changes nothing) with the customer's restrictions lifted:

* **No ownership and no cancellation deadline** - the business may move a booking the customer could no longer move themselves.
* **No minimum notice and no booking window** for the new time (like `resume`): `ignore_notice` on the availability request, now honoured
  by every allocator through one helper, `availability.booking_bounds` (before, only the workshop and field-service allocators looked at it).
  The new time must still lie in the future (422 otherwise; a start equal to the clock is refused).
* **Everything else still binds**: shifts, breaks, opening hours, days off, table caps, the service radius, travel padding and
  double-booking protection decide whether the business *can* serve the new time (409 `slot_unavailable` / `slot_conflict` otherwise).
  Staff stay the same unless `resource_id` names another or `any_available` asks for the least-loaded one; a restaurant gets a fresh table
  choice and a VIP minimum spend that differs from the one agreed needs `accepted_minimum_spend` (the amount shown to the customer) or `accept_minimum_spend` (the customer's consent is still needed).
* **Bookkeeping**: the `booking.rescheduled` outbox event has `actor_type: "staff"`, so the customer receives the usual "your booking was
  moved" message and webhooks fire; flags are reset for the sweep, reminders are forgotten (the new time earns its own), and the admin audit
  log records the call (`POST /v1/admin/bookings/{booking_id}/reschedule`, role, actor, booking id - never the body). No status-history row.
* A job `waiting-for-parts` is refused with a pointer to `/resume`; `in-progress`, `completed`, `cancelled`, `rejected` and `no-show` are
  409. A pending booking keeps its original hold deadline; one whose hold has run out is refused (the sweep records the rejection).
* **Not done**: the customer is not asked to agree to the new time (they are told afterwards); no way to move several bookings at once
  (for example everyone booked with an ill stylist); no reason / note on the move.

## Schedule collisions: telling the stylist

Blueprint §9.3: an add-on added on the spot that runs into the stylist's next appointment alerts the stylist **with that appointment's details**. The same happens for a professional's overrun (§11.3).
Nothing is ever moved or cancelled: the reservation is left as it is and both appointments are flagged `schedule_conflict` (unchanged). What is new is that somebody is *told*:

* **Contact details**: a manager saves `contact_email` and/or `contact_phone` on a *staff* resource (`POST` / `PATCH /admin/resources`; the phone is written the usual way with the business's country code; `null` removes one;
  tables and bays have none - 422). They are managers' and owners' information: other admin roles see `null` in `GET /admin/resources`, and the public staff list never carries them. Migration `0021`.
* **The event**: `booking.schedule_conflict` in the outbox, in the same transaction as the flags, with the running appointment's id, `conflicting_booking_ids`, `cause` (`upsell` / `overrun`) and the times
  (`start_at`, `end_at`, `wanted_end_at`: how far it needed to go) - ids and times only. Webhooks can subscribe to it (or `*`), so the WordPress plugin can push or show it in its own way.
* **The message** (the worker's outbox pass, like every other message): email to `contact_email` and SMS to `contact_phone` (through the configured providers), to the staff member of the appointment that
  needs more time. It names the business, the running appointment and how long it needs to go on, and lists each appointment in the way: the times, the services and the customer's **first name only**.
  It ends "Nothing was moved. Please speak to your manager". The wording is built in (no template). Only the channels that have a contact are used; a staff member with none gets one `skipped` row in the
  notification log (`GET /admin/notifications?event=booking.schedule_conflict`) saying "no contact details saved for the staff member", so a manager can see why nobody was told. Nothing is sent once the flag has been
  cleared (`resolve-conflict`) before the message goes out. Delivery is at least once with the usual per-channel retry: an email that went is not sent again when only the SMS failed.
* **Not built**: WhatsApp for staff alerts (needs an approved template), escalating to the manager when the stylist has no contact or does not react, alerting the *next* appointment's stylist when it is a different person,
  and customer-facing notice of the delay (the business decides that).

## Review invitations

Blueprint §13.4: marking a visit `completed` sends "review request". The `booking.completed` message (default email, or the business's own template on any channel) now carries a link to a review page:

* **Where the link goes**: the business's own page, `review_url` in its profile (`PUT /admin/business-profile`; an http(s) address that must contain `{token}`), else the platform default `REVIEW_URL`
  (same rule). With neither the message keeps its old sentence ("You can rate your visit when you sign in"). The page itself is the website's job (WordPress plugin / widget: not built); it calls the two endpoints below.
* **The token**: a signed JWT with `typ: review`, the booking and the business, valid `REVIEW_LINK_DAYS` (30). It has no `role`, so it cannot be used to sign in anywhere, and anything that is not a review token is refused as one
  (forged, expired, another kind of token, missing claims). It is created when the message is built and stored nowhere. Whoever holds the link can review that one booking, once.
* **Placeholders**: `$review_link` (the URL, or empty) and `$review_prompt` (a ready sentence with the link; the old sentence when there is no page; empty for other events) work in the `booking.completed` templates and are empty everywhere else.
  A WhatsApp template for `booking.completed` is filled with the usual four variables, plus the link as a **fifth** one when there is a link: approve it with five variables if you want the link in WhatsApp.
  No link goes out when the visit has been reviewed already or the customer was erased.
* **`POST /public/review-invitation {token}`** (site key) -> `{business_name, service_names, start_at, status}` with `status` `open` / `already_reviewed` / `not_completed`: enough for the page to say what is being reviewed, nothing about the customer.
  **`POST /public/review-invitation/review {token, rating 1-5, comment?}`** -> 201, the same review a signed-in customer would make (first name only in public, attributed to the staff member, `review.created` event). 409 when it is not a completed visit or was
  reviewed already; 401 for a bad, expired or other business's token; 404 when the booking is gone or the customer erased. Both are POSTs so the token stays out of URLs and access logs; both count as `public_write` for rate limiting.
* **Not built**: sending later than completion or nudging a second time, a per-business switch (edit the template to leave the link out), the review page itself.

## Customer gender

Blueprint §8-9 "Customer Extensions: customer_gender" for spas and salons: `customers.gender`, optional, `female` / `male` / `other` (a database check), migration `0020`.

* **Collected**: `customer.gender` on `POST /public/bookings`, or `PATCH /public/me/profile {"gender": ...}` (`null` removes it; any other field is refused: name and email are not editable there); `GET /public/me/profile` shows the customer's own record.
  Any other kind of business answers 422 to a gender (clearing it is always allowed). A booking that leaves it out keeps what is known; one that sends a value replaces it.
* **Seen by**: every admin role in `GET /admin/bookings/{id}` (`customer_gender`), not in the list. Never in outbox events, webhooks, messages or the customer-facing booking views.
* **PDPA**: part of the access export, cleared by erasure.
* **Not built**: using it (nothing filters or matches staff by it), the staff-side `staff_gender` filter, admin editing of a customer's gender.

## Review flags (settings, shift and service changes)

Blueprint §7.3 / §8.3 / §13.5: when a change means an *upcoming* booking no longer fits, the booking is
**flagged for review, never cancelled, moved or released**. Flags are reason codes in `bookings.flags`
(`flags` on the admin booking output; `?flagged=true|false` and `?flag=<reason>` filter the list; the customer
never sees them):

| Reason | Raised when | Re-checked by |
|---|---|---|
| `outside_shift` | a staff allocation is no longer inside a shift window (breaks excluded) | `PUT /resources/{id}/shifts` (that resource's bookings) |
| `day_off` | a company holiday, or leave of an allocated resource, covers its day | `POST /days-off` |
| `outside_operating_hours` | an allocation is no longer inside opening hours (a restaurant with none can seat nobody; a seating is judged on `slot_duration`, so the table-reset buffer running past closing is not a conflict) | `PUT /operating-hours`, `PUT /time-slot-settings` |
| `outside_slot_times` | fixed-mode restaurant: the start is outside `first_slot_time`..`last_slot_time` | `PUT /time-slot-settings` |
| `staff_not_qualified` | an allocated staff member or bay can no longer do what their reservation is for: **every** base service for one person serving the whole booking (a spa treatment of several services; a salon cart is judged by its base service alone), **that segment's** service for each mechanic and bay of a workshop job order | `PATCH /services/{id}` with `resource_ids` (bookings with that service) |
| `table_cap_exceeded` | more tables of a class are reserved at one moment than the open-table rule allows (everyone seated then is flagged) | `PUT /table-open-rules` |
| `outside_service_area` | the on-site address is now beyond the radius (PostGIS; skipped, flags untouched, if PostGIS is unavailable) | `PUT /business-location` |
| `resource_inactive` | an assigned staff member, table or bay was set `inactive` | `PATCH /resources/{id}` with `status` |
| `table_too_small` | the seats at the assigned table(s) (added up for a combined party) are fewer than the party | `PATCH /resources/{id}` with `capacity` |
| `table_class_changed` | an assigned table moved between normal and VIP, so it no longer matches the booking's `table_type` | `PATCH /resources/{id}` with `attributes` |

* Each change re-checks only its own reasons for the bookings it can affect, in the same transaction, and answers
  with an `X-Bookings-Flagged: n` header (bookings that gained a flag) and `X-Flags-Lifted: n` (bookings that fit again
  and lost one). A reason is **lifted automatically** when the booking fits again (undoing the change), so nothing needs
  cleaning up by hand - reactivating a resource or `DELETE /admin/days-off/{id}` un-flags what they had flagged.
* `POST /admin/bookings/{id}/clear-flags` (manager) dismisses the flags after a human decided to keep the booking.
  A dismissed flag stays dismissed until a later change touches the same thing again.
* Only `pending` (unexpired hold) and `confirmed` bookings are examined - an appointment that has started (`in-progress`), even one stretched past closing time by an upsell or an overrun, is never flagged - and only reservations that still lie ahead
  (finished work and released segments are history). Leaving those two statuses drops the flags.
* **Policies never flag.** A booking carries a frozen copy of its policy, so a policy edit cannot change the rules of
  an existing booking. Editing a service's duration or price does not flag either: bookings keep what they were made with.
* The flagged bookings are locked (`FOR UPDATE`, in id order) while flags are written, so two administrators changing
  settings at the same moment cannot overwrite each other's flags. A booking created at the very instant a setting
  changes can still slip through unflagged (it was validated against the old value); the periodic sweep closes that gap (see *Background sweep*).
* `schedule_conflict` (overrun / upsell collisions) is a separate, older flag with its own endpoint.

## Availability cache (Redis)

`GET /public/availability` answers are cached in Redis for `AVAILABILITY_CACHE_TTL_SECONDS` (default 30; `0` = off),
per tenant and request (date, services, resource, party size, table type, separate seating). The response says
`X-Cache: HIT | MISS | BYPASS`.

* **Invalidation is automatic and complete by construction, and per date for bookings.** Each tenant has a generation token in Redis and
  each date has its own; cached answers live under both (`avail:{tenant}:{tenant token}:{date token}:{request hash}`), so replacing the tenant's token
  drops all of its answers at once and replacing a date's token drops that date alone. Nothing calls "invalidate"
  by hand: SQLAlchemy session hooks notice any write to a table availability depends on (`TRACKED_TABLES` in
  `app/core/availability_cache.py`: bookings, allocations, resources, shifts, days off, opening hours, slot settings,
  table rules, policies, services and their staff/part links, products, the tenant row) - ORM changes and bulk
  `update()`/`delete()` alike - and the tokens are replaced **after the transaction commits**, before the response is
  sent. A rolled-back write (e.g. a lost booking race) invalidates nothing.
* **What clears what.** A change to a **booking or its reservations** (a new booking, a cancellation, a move - old *and* new dates -, a hold that ran out)
  replaces only the tokens of the dates around it: availability for a date D reads reservations overlapping `[D 00:00, D+2 days 00:00)`, so a reservation
  `[lo, hi)` can change dates from the one before `lo`'s to `hi`'s; the cleared range is wider than that on both sides (3 days before and 2 after, in UTC dates,
  which differ from the business's own by at most a day - so no time zone is needed, and tests cover UTC-11, UTC-5 overnight and UTC+14 businesses). Bulk
  `update()`/`delete()` statements give no dates, so the hook reads the periods of the rows a statement is about to change first; a bulk update that touches only columns availability
  does not read (links, notes, flags) clears nothing. Anything it cannot date - a reservation longer than 14 days, an unbounded one, a statement with no condition, a transaction touching
  more than 200 dates, one that also changed a settings table - clears the whole tenant. **Everything else** availability depends on (resources, shifts, days off, opening hours, slot settings,
  table rules, policies, services, stock, the tenant row) replaces the tenant's token: those changes can affect any date. Date tokens expire after 15 minutes (far longer than an
  entry lives); a token that is missing is replaced by a fresh random one, so an old answer is never revived. Raw `text()` SQL is invisible to the hooks, as it always was.
* **A test keeps the list honest:** it records the SQL of one availability request per vertical and fails if any
  table it reads is not in `TRACKED_TABLES`, so a future change that makes availability depend on a new table cannot
  silently leave stale answers behind.
* **Safe by ordering.** A reader takes the token *before* computing and stores under it, so an answer computed before
  a write is never read after it; and the token only changes once the write is visible to readers.
* **Redis failing is never visible to customers.** Errors are swallowed (a miss, and bookings still work); after a
  failure the cache is skipped for at least the TTL (writes during the outage could not invalidate older entries) and
  Redis is not retried on every request. Timeouts are `REDIS_TIMEOUT_SECONDS` (0.25).
* **What the TTL still bounds:** changes that come from time alone - a pending hold expiring (its slot reappears when
  the entry does, at most one TTL late) and the minimum-notice cut-off moving (cached slots that have since slipped
  inside it are filtered out on every hit, so a customer is not offered a slot that would be refused). A slot that
  newly enters the booking window also waits for the entry to expire.
* **Never cached:** requests carrying `latitude`/`longitude` (a customer's map pin is personal and unique), and
  errors. Booking itself never reads the cache: the allocator and the exclusion constraint decide, so a stale
  answer can at worst cost a 409, never a double booking.
* The date ranges are deliberately generous (a booking clears about six dates, not one) and the settings tables still clear the whole tenant; a change to a single specific date
  (a one-day holiday, a date-specific table rule) is not dated either. The month view reads one tenant token and one token per date in a single round trip.

## Countries, time zones and daylight saving

The service is built for **Malaysia (`MY`), Singapore (`SG`) and Australia (`AU`)**, most of all Malaysia (`app/core/countries.py`: one table, one entry per country; adding a country is adding an entry and a test).
A business picks its country **once, when it is set up** (`POST /auth/tenants {"country": "AU", ...}`; left out it is Malaysia, or the country of a given `phone_country_code`, as before countries existed), and the country settles the rest:

| | Malaysia | Singapore | Australia |
|---|---|---|---|
| time zones to choose from | `Asia/Kuala_Lumpur` | `Asia/Singapore` | `Australia/` Sydney (default; also ACT), Melbourne, Brisbane, Adelaide, Perth, Darwin, Hobart, Broken_Hill, Eucla, Lord_Howe |
| clock changes | none | none | Sydney, Melbourne, Hobart, Adelaide, Broken_Hill (1 hour) and Lord_Howe (30 minutes); not Brisbane, Perth, Darwin, Eucla |
| phone code (completes a number typed without one) | 60 | 65 | 61 |
| postcode | 5 digits | 6 digits | 4 digits |
| states in an address | the 13 states and 3 federal territories (`SGR`, `KUL`, ...) | none | `NSW VIC QLD SA WA TAS NT ACT` |
| currency | MYR | SGD | AUD |
| lookup bias for typed addresses | `my` | `sg` | `au` |

* **Sign-up checks.** The time zone (default: the country's first) must be one of the country's; a `phone_country_code` that is not the country's, a country that is none of the three and a zone of another country are 422 with a message that names the choices.
  `GET /v1/countries` (no key; it is what a sign-up or settings page reads) lists the countries with their time zones, states, postcode shape, currency and privacy law.
* **The address in parts.** `address_line1`, `address_line2`, `city`, `state` (code or name, any case: `selangor` is `SGR`) and `postcode` can be given at sign-up and in `PUT /admin/business-profile`: all or nothing (street, city and postcode, and the state where the country has states),
  postcode and state checked for the country (Singapore has no states and its city defaults to Singapore). They become the profile, and the one-line `address` the page shows is written **from** the parts (what is sent as `address` alongside them is ignored). Sent without parts, `address` is kept as typed and the parts are cleared.
  Postcodes are checked by shape only, not against the post office's list. Without an address at sign-up there is no profile yet (the page shows the business's name only), as before.
* **`GET /admin/business-settings`** (manager+) shows the country, currency, phone code and time zone with the zones on offer; **`PUT /admin/business-settings {"timezone": ...}`** (owner only, audit-logged) moves the business to another of its country's zones (a Queensland
  business set up as Sydney). Opening hours and shifts are clock times, so they move with the zone: the availability cache for the business is cleared and upcoming bookings no longer inside their shift or the opening hours are **flagged** (`X-Bookings-Flagged`), never cancelled.
  The country itself cannot be changed (it would change the phone code, currency and postcode rules under existing customers).
* **Customers' postcodes** (saved addresses, an on-site booking's address) are kept in the country's shape (spaces removed) and a wrong shape is 422 before any paid lookup is made; typed addresses are looked up with the business's country as the bias (`GEOCODING_REGION` no longer exists).
* **Daylight saving.** A day is not always 24 hours: in Sydney the night of the first Sunday in October is 23 hours (02:00 becomes 03:00) and of the first Sunday in April 25 (03:00 becomes 02:00). Opening hours and shifts are clock times, so a 22:00-06:00 shift is 7 real hours
  on the first night and 9 on the second. All the interval maths (windows, breaks, busy times, the slot grid, every allocator) is done in UTC and shown in the business's zone only at the end (`Interval` converts whatever it is given: two datetimes in the same zone are compared by their clock
  readings, which is wrong across a change). The grid stays on the quarter hour of the wall clock; no start is offered in the hour the clocks skip; **both passes of a repeated hour are offered and can be booked** (02:30 on summer time and 02:30 again on standard time are two different moments, and the
  one booked first does not block the other unless they overlap). A clock time written for a skipped hour (a shift starting at 02:30 on that night) starts at the moment after the jump; one written for a repeated hour means its first pass.
* Migration `0022`: `tenants.country` (businesses set up before it are given the country their phone code names; one with any other code stops the migration, naming it, rather than being labelled wrongly) and the address parts on `business_profiles`.

## Plans and limits (free and paid tier)

The free tier is **the same hosted backend with per-business limits**; the paid tier has none (`app/services/plans.py`, tables `plans` and `tenants.plan`, migration `0023`). A business is on one plan; the limits are numbers in the `plans` table
(`NULL` = no limit, `0` = none at all). The seeded **`free`** numbers are a first proposal, not a pricing decision - change them with an ordinary `UPDATE plans SET ...` (the application's database role can read the table but not write it):

| limit | what it counts | `free` |
|---|---|---|
| `max_resources` | active staff, tables and bays together (deactivating one gives its place back; switching it back on needs a place again) | 3 |
| `max_services` | active services | 10 |
| `max_bookings_per_month` | bookings made in the business's own calendar month (its time zone), not counting cancelled or rejected ones; bookings made by staff and follow-ups count too | 50 |
| `max_whatsapp_per_month`, `max_sms_per_month` | messages **sent** that month: customer messages, sign-in codes and the stylist alert (email is never limited) | 20 each |

* **Over a limit** the answer is **402** `plan_limit_reached` with `limit`, `allowed`, `used` and `plan`, and nothing is created. For messages: a customer message is logged as `skipped` ("plan limit: ...", never retried, the booking is unaffected); a sign-in code is refused with the same 402 and no code is left
  behind, so the customer signs in with the emailed link instead (this is why the free plan allows some WhatsApp/SMS rather than none: with none, a customer with no email could not sign in at all).
* **Counting** is done under a per-business advisory lock held until the transaction ends, so two requests cannot both take the last place (a business that is limited serialises its own booking requests; an unlimited one pays nothing). **Messages** count under a lock of their own (`messages:<business>`, so a slow gateway never holds up a booking), taken only where the plan has a limit: the worker keeps it until its log row is committed, and a sign-in code takes its place in a short transaction
  *before* the gateway is called (status `sending`, which counts), so two senders - two codes, or a code and the worker - cannot both take the last message. A message that fails to send gives its place back. A sign-in request waits for the worker's current event
  to finish (a gateway call, at most `DELIVERY_TIMEOUT_SECONDS`) when it arrives during one.
* **Who is on which plan.** A business set up by the operator (`POST /auth/tenants`) is **`paid`** unless the body says `"plan": "free"` (a business that signs itself up is always on `free`: see *Self-service sign-up*); migration `0023` put every existing business on `paid`. The operator moves a business with
  `PUT /v1/platform/tenants/{id}/plan {"plan": "free"}` and lists the plans with `GET /v1/platform/plans` (both need `X-Platform-Token`, rate-limited like onboarding), effective at once. **Nothing is ever deleted**: a business moved to a smaller plan keeps what it has and cannot add more until it is back under.
* **`GET /admin/plan`** (manager+): the plan, the limits that apply to this business (`plan.limits`), `customised` (the limits whose numbers were set for this business alone), what is in use now, `over_limit` - the limits the business is over after a move to a smaller plan - and `warnings` (below).
* **Limits for one business alone** (migration `0026`, `tenants.plan_overrides`): the operator can give one business numbers that differ from its plan - a bigger allowance for a customer who asked, a smaller one for a trial - without inventing a plan for it.
  `PUT /v1/platform/tenants/{id}/overrides {"resources": 10, "sms_per_month": null}` (`X-Platform-Token`): a limit named here replaces the plan's (`null` = no limit, `0` = none at all), a limit left out is the plan's, and the call **replaces the whole set** (`{}` removes them all). `GET /v1/platform/tenants/{id}/plan` shows the plan's own
  limits, the overrides, the limits that apply, the usage, `over_limit` and `warnings`. It takes effect at once, nothing is removed if a limit drops below what the business has (it just cannot add more), and a refusal says "with limits set for your business" so nobody looks for the number in the plan.
* **Warnings before a limit**: when something is added that brings a business to `PLAN_WARNING_PERCENT` (80) of a limit - and again when it reaches it - an email goes to the business's contact email (the one in its profile), once: *"Lotus Spa has used 8 of its 10 services on the Free plan..."*. It is decided where the thing is added (a service, a resource, a booking, a message), in the same
  transaction, and remembered in `plan_warnings` (one row per limit, month - the business's own - or `total`, level and limit value), so a rolled-back booking leaves no warning, a business that stays near its limit is not told at every booking, a new month starts afresh, and a changed limit is warned about afresh. `0` switches warnings off; a limit of none at all or no limit never warns. Without a contact email the
  warning is noted in the notification log as skipped; a failed send is retried by the outbox. The same rule shows in `GET /admin/plan` as `warnings`. Rounding: 80% of 3 is 2.4, which rounds up to 3, so a limit of 3 gives only the "reached" email.
* **Not built:** billing, trials, upgrade or payment flows (the operator changes the plan by hand), plan-specific features other than these limits (webhooks, reminders, health notes, reports are the same on every plan), usage history over months, a daily message limit, the business changing its own limits.

## Free plan: one account, one business, one website

Every business lives in this backend, and the free ones are held to the numbers in the `plans` table (the design was considered again and kept: storing free users' data in the WordPress
database would mean rewriting the booking engine in PHP, the limits could be bypassed by editing the plugin, and every upgrade would need a data migration). What is new is the
rule *one account = one business = one website*; more than one website or domain is for paid businesses (migration `0028`, `app/core/origins.py`, `app/services/sites.py`).

| Plan column | What it holds | `free` | `paid` |
|---|---|---|---|
| `max_sites` | websites (`sites` rows) | 1 | none |
| `max_domains` | distinct domains over a business's sites; `www.shop.example` and `shop.example` are one. **A plan that sets this derives the site's allowed origins from its address** | 1 | none |
| `max_live_keys_per_kind` | live (not revoked) publishable keys, and live secret keys: one in use and a spare, so a key can be rotated | 2 | none |
| `domain_change_days` | days that must pass between two changes of the website address (a rule, not a count) | 30 | none |

All four are in `GET /admin/plan` (limits, usage, `over_limit`, and `domain_change`: how often, and when the next change is possible), in the platform's plan views, and can be set for one business
alone with `PUT /platform/tenants/{id}/overrides` (`sites`, `domains`, `live_keys_per_kind`, `domain_change_days`). They are *structural*: unlike the counts of a month's bookings they are not
warned about by email (a free business is at 1 of 1 sites from the day it signs up). Nothing is ever deleted: a business moved to a smaller plan keeps what it has (`over_limit` lists it)
and cannot add more. Every existing business is `paid`, so none is limited by this.

* **The address.** On a plan that limits domains the website address is **required** at sign-up (`domain`; and at the operator's `POST /auth/tenants` for `"plan": "free"`) and a list of origins is
  refused. Whatever was typed (`https://WWW.Shop.Example/book`, `shop.example`, `bücher.example`) becomes one canonical host (`shop.example`; no port, IP address or `localhost`) and the allowed
  origins are derived: `https://shop.example` and `https://www.shop.example`. The publishable key then works from those two origins only (and from `http://localhost` / `http://127.0.0.1` on any
  port, which anyone can reach anyway, so a developer can test). On a plan with no domain limit (paid, or set up by the operator) the owner keeps a free-form list of origins, each checked and
  written canonically (`https://shop.example`, scheme required, no path, at most 50), and an **empty list lets any origin use the key**. `Site.domain` is only recorded there (a development host
  such as `localhost:8080` is accepted).
* **Changing it.** `GET /admin/site` (manager) shows the address, the origins, when it last changed and when the next change is possible; `PUT /admin/site {domain, allowed_origins?}` (owner,
  audit-logged) changes it, and so does the WordPress plugin's `site.updated` sync event (`domain`, `allowed_origins`): **the same rule for both**. A *change* is a different set of domains;
  the same address spelled another way (`www.`, case, a trailing slash, a path) is nothing. The address given at sign-up is not a change. After a change the next is refused with 402
  `plan_limit_reached` (`limit: "domain_change_days"`, `allowed: 30`, `next_allowed_at`) until 30 days (rolling) have passed; the old address stays. A second domain is refused with 402
  `limit: "max_domains"`. Two changes at once run one after the other (the second is refused). To help an owner who needs a second fix early, the operator sets
  `{"domain_change_days": 0}` for that business through the overrides, and clears it afterwards.
* **Keys.** A key always belongs to a site: `POST /auth/api-keys` without `site_id` means the business's site (older keys with no site keep working and show `site_id: null`; none is made now). At
  the limit a new key is 402 `max_live_keys_per_kind` (revoke one first). **`POST /auth/api-keys/recover` always works**: where the limit would be passed it revokes the oldest key of that kind (never the
  ones it just made), counted in the existing `revoked`, so an owner who lost the keys while at the limit is not locked out - but a recovery can end the key the plugin is using until the owner
  pastes the new secret key into WordPress. The last working secret key still cannot be revoked. `plans.ensure_can_add_site` exists for the day an endpoint adds a second site; nothing calls it yet.
* **What the plugin says it runs on.** `POST /auth/token` takes an optional `site_url`. It is a **signal for the operator and nothing more**: any value is accepted (even a wrong type) and a request is
  never refused because of it; the last one seen is stored on the site when it changes, the platform's `GET /platform/tenants/{id}/plan` shows it next to the domain (`site.site_url_matches_domain`: true,
  false, or null when there is nothing to compare; true for `localhost`), and a mismatch is logged once as a warning. Nothing is blocked on a mismatch.
* **What this cannot do** (said plainly, because the terms should not promise more): the `Origin` header only exists in browsers, so a **server** that calls the API itself (a second WordPress
  install using the same keys) is not held by the allowed origins, and the secret key is not tied to a domain (a copied secret key works anywhere; secret-key requests also skip the bot check).
  The limits that cost money (resources, services, bookings, WhatsApp, SMS) are shared by the whole business however many websites it has, so a second site gains no capacity: this rule keeps the
  product tiers apart, it is not a wall. A person can still start several free businesses with different mailboxes (one business per owner email, compared as typed: plus-tags and Gmail dots are
  not folded together). A domain is checked for form, not for ownership (no DNS or meta-tag check), which is also why one domain is not reserved to one business.
* **Not built**: feature switches on plans (see *Plans and limits*: not part of this; the plan columns leave room for a `features` list and a `features` key in `plan_overrides`), an endpoint that adds a
  second site, re-issuing the site's webhook secret (it is shown once at set-up; `recover` returns keys only), ownership verification of the domain, folding mailbox variants together.

## Self-service sign-up

A business can set itself up on the **free plan** without the operator (`app/services/signup.py`, migration `0024`). **Off unless `SELF_SERVICE_SIGNUP=true`** (403 `signup_disabled` otherwise); it also needs an email provider (`SMTP_HOST`), `SIGNUP_VERIFY_URL`
(the website's confirmation page, `{token}` marks the token) and `TERMS_URL` (start-up warns about each that is missing). Nothing exists until the owner's email address is confirmed:

1. **`GET /v1/signup/config`** (no key) - `{enabled, terms_version, terms_url}`: what the sign-up page shows and which `terms_version` it must send back.
2. **`POST /v1/signup`** (no key) `{name, business_type, country, timezone?, owner_email, accepted_terms_version, address_line1, address_line2?, city, state, postcode, site_name?, domain}` - **`domain` (the business's website, `shop.example`) is required and `allowed_origins` is refused**: see *Free plan: one account, one business, one website* - the same checks as the operator's
   onboarding (country, zone, address) with the address **required** and the terms version required to be the current one (`TERMS_VERSION`). Nothing is created: the request is kept in Redis for `SIGNUP_TOKEN_TTL_SECONDS` (a day), under a hashed one-time token, and the link is mailed.
   The answer is **always `202 {"status": "sent"}`** for a valid request, even when the address already has a business (then no mail is sent), so the form cannot be used to find out who is a customer. One pending request per address (a newer one cancels the older link);
   `SIGNUP_RESEND_COOLDOWN_SECONDS` (60) between requests for one address (429, the same for every address); a mail that cannot be sent is 503 and leaves nothing behind. **`plan` and any other field not listed is refused**: a business cannot choose its plan.
3. **`POST /v1/signup/verify {token}`** (a POST on purpose: mail scanners that merely open links would use a GET token up) - takes the token once and creates the business, its first site and keys **on the free plan**, storing `owner_email` (lower-case, unique: **one business per address**) and
   `terms_version` / `terms_accepted_at`. The keys are in the answer **this once**. 401 for an unknown, used or expired token, 422 if the terms changed meanwhile (sign up again), 409 if the address got a business in the meantime.

Limits per client address: `signup` 5 an hour, `signup_verify` 20 per 10 minutes (override with `RATE_LIMITS`). The operator's `POST /auth/tenants` shares the same set-up code (`app/services/onboarding.py`).
**Booking rules from the start** (2026-10-16): both paths also create one booking policy called `Default` and make it the business's default, with the same values `POST /admin/policies` uses when a field is left out (confirmed at once, 2 h notice to book, 24 h to cancel, 60 days ahead, 30-minute hold for a pending booking). Without it a new business answered 422 "No booking policy configured" to availability and bookings until its owner made one. The owner sees it in `GET /admin/policies` and replaces it with `make_default`; businesses made before this change are not touched (no migration).
**Bot check:** Cloudflare Turnstile in front of `POST /signup` (see *Bot check*). **Throw-away addresses are refused**: an owner address on a temporary-mail service (mailinator, guerrillamail, yopmail, 10minutemail and some 100 others, with their subdomains) is a `422 disposable_email` before anything is stored or sent, so such a business could never be reached again for its owner sign-in.
The built-in list (`app/core/disposable_emails.py`) is a starting point, not a guarantee - new services appear every week; `BLOCKED_EMAIL_DOMAINS` adds domains and `ALLOWED_EMAIL_DOMAINS` takes some off (JSON lists in the environment). Customers' addresses are never checked, and the refusal depends only on the domain, so it tells nothing about who is registered.
**Lost keys, owner sign-in:** see *Owner sign-in and lost keys*. **Not built:** changing the owner email by the owner (the operator can: `PUT /platform/tenants/{id}/owner-email`), a welcome email after confirming, payment (nothing asks for a card), the terms text itself (the page shows `TERMS_URL`; only the version is recorded).

## Owner sign-in and lost keys

The owner of a business can sign in **with their email address**, without WordPress and without a password (`app/services/owner_login.py`, `app/api/v1/owner.py`; no migration). For a business that signed itself up, `owner_email` was proved by a confirmation link before the business existed. A business the operator set up has none until the operator gives one:
`PUT /v1/platform/tenants/{id}/owner-email {"email": "owner@example.com"}` (`null` removes it; 409 if another business has it), or `"owner_email"` in `POST /auth/tenants`.

1. **`POST /v1/owner/login {email}`** (no key) mails a sign-in link to that address if it is a business's owner address. The answer is **always `202 {"status": "sent"}`**, so the form cannot be used to find out who is registered (a mail that cannot be sent is only logged). The link goes to `OWNER_LOGIN_URL` (`{token}` marks the token; without it the email carries the bare token). One live link per address (a newer one cancels the older),
   valid `OWNER_LOGIN_TTL_SECONDS` (15 minutes), only a hash kept in Redis. `OWNER_LOGIN_COOLDOWN_SECONDS` (60) between requests for one address and `SIGN_IN_REQUESTS_PER_DAY` per address (429, the same for every address, known or not); limits per client address: `owner_login` 10 an hour, `owner_login_verify` 30 per 10 minutes. A suspended business gets nothing.
2. **`POST /v1/owner/login/verify {token}`** (a POST so that a mail scanner that merely opens links cannot use it up) takes the token once and returns an **owner admin token**: `{access_token, token_type, expires_in, tenant_id}`, the same token, powers and lifetime (15 minutes) the WordPress server gets from the secret key. The user is recorded as `owner:<email>` (visible in `GET /admin/users`, named in the audit log); an owner the business has
   disabled cannot sign in and loses a token already held.
3. **Lost the keys?** Sign in as above, then **`POST /v1/auth/api-keys/recover {"revoke_existing": false}`** (owner): a new publishable and secret key for the business's site, shown this once. Put the secret key in the WordPress plugin and the publishable key in the web page. `revoke_existing: true` switches every *other* key off once the new pair exists - what to do if a key may have leaked (the old ones stop at once). `GET /auth/api-keys` lists the keys (prefixes only), `DELETE /auth/api-keys/{id}` revokes one.

**Not built:** a password, two-factor sign-in, a session that outlives 15 minutes (the owner signs in again), a bot check on the sign-in request (the per-address pause, daily cap and per-client limits stand in for it), notifying the owner of a sign-in. Anyone who controls the owner's mailbox can sign in as the owner: that is the definition of the owner here.

## Overnight hours

Opening hours and shifts may run past midnight: give an end time **before** the start (`open 18:00, close 02:00`; a
shift `22:00`-`06:00`). Equal times are rejected. Fixed-mode reservation times work the same way (`last_slot_time` before
`first_slot_time`, e.g. 18:00 to 01:00), and a shift's break may fall before, after or across midnight.

* **Business Day Rule (blueprint §6).** Time after midnight belongs to the business day on which the window *started*. A
  Friday 18:00-02:00 window is Friday's, including its early hours of Saturday. So `GET /availability?date=Friday` lists the
  slots up to 01:xx on Saturday (their `start_at` carries the real date), Saturday's date starts again at 18:00, a day off on
  Friday blocks the whole Friday night, and a day off on Saturday leaves Friday's after-midnight hours open. Table-open rules
  and review flags use the same day.
* **Booking an after-midnight start.** The API is given only the start instant (Saturday 01:00). The allocator first tries
  that calendar date; if it does not offer the start and yesterday has a window that runs past midnight (hours or a shift,
  one small query), it tries yesterday's business day. Businesses without overnight windows behave exactly as before
  (one allocation attempt). A start neither day offers is refused with the usual 409 and the requested date's slots.
* **Everything that assumed a day ends at midnight was widened:** slot generation, the busy-time lookups (an after-midnight
  booking blocks the same night's slots and "Any Available" counts it in that stylist's load), workshop/practice slot loops,
  tenants with no opening hours at all (shifts alone decide, so a night shift is not cut at midnight), the month view (a
  business day still counts as bookable in its after-midnight tail), and the flag rules (`day_off`, `outside_shift`,
  `outside_operating_hours`, `outside_slot_times`, `table_cap_exceeded` are judged on the business day).
* Migration `0009` relaxes the shift constraints (`end_time <> start_time`; the break is measured forward from the shift's
  start, so overnight breaks pass) and drops `slot_order` on the restaurant slot settings. Downgrading is only possible
  while no overnight row exists.

## Month view

`GET /public/availability/month?month=YYYY-MM&service_ids=&...` (same optional parameters as the day view:
`resource_id`, `party_size`, `table_type`, `allow_separate_seating`, `latitude`/`longitude`) returns, for every date of the
month in the business's timezone, `{date, available, slot_count}` - enough to grey out a calendar - plus the automotive
`lead_time_notice`. The blueprint only defines the day endpoint, so this shape is a design choice; the slots themselves
still come from the day view.

* **Same answers as the day view, by construction.** Each date is computed by the same code as `GET /availability`
  (`app/services/availability_view.py`) and stored under the same cache key, so the two views cannot disagree, the month
  warms the day view (and the reverse), and invalidation needs nothing extra. Cached days are re-filtered against the
  clock exactly as in the day view. Cache lookups for the whole month use one token read and one `MGET`.
* **Cost.** On a cold cache a month runs the availability pipeline once per date (up to 31 runs, sequentially on one
  DB session); dates that cannot hold a bookable slot - already past, or after the booking window closes - are skipped
  without running it (so a month in the past or far ahead is free). Warm, it is a single Redis round trip. Requests
  carrying a map pin are not cached, so an on-site month runs every date (including the PostGIS radius check per date).
* `X-Cache` is `HIT` when nothing had to be computed, `MISS` when some dates were, `BYPASS` when the cache is unusable.

## Background sweep (`python -m app.worker`)

A worker process (`--once` for a single pass, e.g. from cron; the compose file runs it as the `worker` service) sweeps
every active tenant each `SWEEP_INTERVAL_SECONDS` (60):

1. **Pending holds expire** without waiting for someone to book: an unanswered pending booking becomes `rejected` and its
   slot is freed (blueprint §13.5). Reservations and transitions also do this opportunistically; the sweep covers quiet tenants
   and refreshes their cached availability, which would otherwise show the slot as taken until the entry expires.
2. **Review flags are checked once for every new booking.** A settings change re-checks the bookings that exist at that
   moment; a booking being made at the same instant was validated against the *old* setting and can commit after that
   re-check, so nothing would ever flag it. `SWEEP_GRACE_SECONDS` (60, longer than any booking transaction) after creation,
   every rule is run against it once and `bookings.flags_checked_at` is set. Resuming a workshop job clears the marker (new allocations).
   Bookings that are over, cancelled or rejected are simply marked without running the rules.
   *Once, not repeatedly, on purpose:* a manager can dismiss a flag; re-checking dismissed bookings every minute would raise it forever.
   The change-time re-checks keep existing bookings current; the sweep only catches the ones they could not see.
   On the first run after upgrading, existing upcoming bookings get this check too (their marker starts empty).

Safety: one Postgres advisory lock means several workers can run and only one sweeps at a time (the others skip the tick);
each tenant is swept in its own transaction, so a failure is logged, that tenant is rolled back whole and retried next tick,
and the others are unaffected; a large backlog is worked off in batches over several ticks. The sweep changes nothing about
availability (only flags and markers), and the availability cache only treats bookings' `status`, `start_at`, `end_at` and
`hold_expires_at` as things that matter, so a sweep tick does not clear anyone's cached answers unless it actually expired a hold.

This is a plain asyncio worker, not Celery/RabbitMQ (blueprint §13.5): the sweep is an ordinary function
(`app.services.sweep.sweep_all`) that a Celery beat task could call, but that wrapper is not built.

## Schema tables of blueprint §12.3

The blueprint lists tables beyond the scheduling core. Where it gives fields they are followed; where it only names a
table its purpose is taken from the sections that mention it. Every table is tenant-scoped with row-level security.

| Table | What it does |
|---|---|
| `booking_status_history` | Append-only: every status a booking has had, who moved it (customer id, WordPress user id, or the system) and when. `GET /admin/bookings/{id}/history`. |
| `outbox_events` | Transactional outbox: `booking.created`, `booking.confirmed`/`cancelled`/`rejected`/`in_progress`/`completed`/`no_show`/`waiting_for_parts`, `shift.updated`, `review.created`, written in the *same transaction* as the change (a rolled-back change leaves none). Ids, statuses and times only, never personal data. |
| `audit_log` | PDPA: every successful state-changing admin request (route, actor, role, record concerned, never the body) plus reads of health notes, data exports/erasures and retention purges. Append-only (a trigger refuses updates and deletes). `GET /admin/audit-log` (owner). |
| `data_requests` | PDPA: a customer's access or erasure request (`POST /public/me/data-requests`) and how the owner handled it (`/admin/data-requests`). |
| `business_profiles` | `GET /public/business` (blueprint §14) and `GET/PUT /admin/business-profile`: name, description, contacts, logo, gallery, social links, address, map link, the business's `review_url` (admin only: see *Review invitations*), and per-business `details` (`cuisine_type`, `workshop_category`, `profession_type`). URLs must be http(s). The map location and service radius stay on the tenant (`PUT /admin/business-location`). |
| `reviews` | A customer rates a *completed* appointment once (`POST /public/bookings/{id}/review`, or through the emailed link: *Review invitations*); attributed to the staff member who served it; public list and average (`/public/reviews`, `/public/reviews/summary`) show the first name only; a manager can hide one (`PATCH /admin/reviews/{id}`). |
| `users`, `user_roles` | Administrative users as WordPress asserts them at token exchange (`POST /auth/token` records the user and role). An owner can disable a user (`PATCH /admin/users/{id}`): no new token, and their existing tokens stop working on the next request. Never the last active owner, never yourself. |

**`global_settings` is deliberately not a table.** The blueprint's GlobalSettings are the default policy and the timezone;
here those live on the `tenants` row (`default_policy_id`, `timezone`), which is read before a tenant-scoped session exists
(to authenticate and to interpret dates). A second copy would need two sources of truth kept in sync for no gain.

**Outbox delivery** is at-least-once: the consumers are the customer-notification and webhook publishers the worker installs (a `CompositePublisher`; no RabbitMQ yet); with no worker running events wait in the table; `app.services.outbox.set_event_publisher` is the seam a broker plugs into, after which
the sweep delivers them in order per booking with exponential back-off, and deletes delivered ones after
`OUTBOX_RETENTION_DAYS` (7). Consumers must tolerate duplicates.

**PDPA**: an *access* request returns everything held about the customer (health notes decrypted, reviews included);
*erasure* anonymises the customer (name, phone, email), deletes their saved addresses and vehicles, clears the notes and
special requests, the contact email and the idempotency key and request hash on their bookings and the text of their reviews, queues their bookings' Google Calendar events to be rewritten without them (events written while the owner shared customer details held name, phone and request; a paused calendar is rewritten in full when it is connected again; events of a *disconnected* calendar stay in Google), and keeps the business's own booking records with no way
back to the person. Erasure is refused (409) while they have an open booking. `HEALTH_NOTES_RETENTION_DAYS` (0 = keep)
clears health notes that many days after a booking ended, done by the sweep and written to the audit log.

## Auth model

| Caller | Credential | Used for |
|---|---|---|
| Browser / headless widget | `X-Site-Key: pk_…` (+ per-site `Origin` allowlist) | `/v1/public/*` |
| WordPress server | `X-API-Key: sk_…` | `POST /v1/auth/token` → short-lived JWT (`owner`/`manager`/`staff`) |
| Admin SPA | `Authorization: Bearer <jwt>` | `/v1/admin/*` |
| WordPress webhooks | `X-Site-Key` + `X-Timestamp` + `X-Signature` (HMAC-SHA256 of `"{ts}.{body}"`) | `/v1/sync/webhook` |
| Customer | site key + `Authorization: Bearer <customer token>` from the OTP or magic-link login (a link for an unconfirmed email gives a *limited* session: own-address bookings only) | `/v1/public/me/*`, `/v1/public/bookings/{id}/cancel`, `/reschedule`, `/review` |
| Owner (admin token) | `Authorization: Bearer <jwt>` | `GET /v1/auth/api-keys`, `POST /v1/auth/api-keys`, `DELETE /v1/auth/api-keys/{id}` (see *Security review*) |
| Google (a browser redirect) | the single-use `state` it was given | `GET /v1/calendar/oauth/callback` (see *Google Calendar sync*) |
| Platform operator | `X-Platform-Token` | `POST /v1/auth/tenants` (returns keys once) |
| Platform operator | `X-Platform-Token` | `GET /v1/platform/plans`, `PUT /v1/platform/tenants/{id}/plan` |
| Nobody (a sign-up page has no business yet) | none | `GET /v1/countries`, `GET /v1/captcha`, `GET /v1/signup/config`, `POST /v1/signup`, `POST /v1/signup/verify` (see *Self-service sign-up*, *Bot check*) |
| The owner, by email | the single-use link mailed to the owner address | `POST /v1/owner/login`, `POST /v1/owner/login/verify` -> an owner admin token (see *Owner sign-in and lost keys*) |

## Roles (admin route review, 2026-10-12)

The blueprint's roles, and what the code enforces (`tests/test_role_matrix.py` pins every route's minimum role, calls each route with a token of each role, and fails if a route is added without a row):

| Role | May |
|---|---|
| **owner** | everything, including: API keys and key recovery, the website address (`PUT /admin/site`), webhooks (create, change, delete, rotate, test, redeliver), Google Calendar connections (connect, change, resync, disconnect), disabling users, the business's time zone, the audit log, privacy requests (access export, erasure) |
| **manager** | settings, services, resources and shifts, policies, products and parts lists, opening hours and slot rules, business profile and location, templates and reminder times, reviews, the notification log, the plan and usage; *read* webhooks, calendar connections and users; every booking (list, detail with health notes, reschedule, clear flags, resolve a conflict); and all of staff's |
| **staff** | the booking list and detail (no health notes), the status of an appointment, add-ons and products on the ticket, parts on the job order, overrun report, follow-up, resume and meeting link; read the staff and shift lists and days off. Contact details of other staff are hidden from them (a *customer's* name, phone and booking email are not: see below) |

* **Who booked (2026-10-15).** Every booking an admin route answers with (the list, the detail, and every route that changes one: status, reschedule, clear flags, add-ons, follow-up, resume, overrun, resolve a conflict, meeting link) carries `customer_name`, `customer_phone` and `contact_email`, for **every admin role**: staff call a customer who is late or missing (decided 2026-10-15). `contact_email` is the email given with *that* booking (`bookings.contact_email`, lower-cased), not the customer's: anyone can type any address into a booking. An erased customer shows "Erased customer" with no phone and no email. A staff member tied to a resource still sees only their own appointments, so never a colleague's customers. The list loads its customers in one query, however many rows (`tests/test_admin_customer_contacts.py`).
* **Own appointments.** A staff member whose token is tied to one resource (a stylist, a mechanic: the `resource_id` WordPress asserts at token exchange) may use the booking routes only on appointments allocated to them, and the list shows only theirs (403 "Staff can only see/update their own appointments"). Managers, owners and staff tied to nobody (a receptionist) are not held to it. One helper (`_own_job_only` in `app/api/v1/admin.py`) does this; before this review five routes (booking detail, `resume`, `follow-up`, `actual-duration`, `meeting-link`) forgot it and let a stylist read or change a colleague's appointment.
* Every state-changing route that needs a role is audit-logged (checked for all 50). Role tokens last 15 minutes and a role change by WordPress only shows at the next token.
* **Decisions left as they are, for the product owner**: staff may set any status on their own appointments, including approving or rejecting a pending request and cancelling (the blueprint says "status updates"); a staff member tied to a resource can still list resources, read anyone's shifts and see company days off (the blueprint says "own schedule"); a staff token with no resource is unrestricted by design; managers can read webhook URLs (which may hold a secret in their path); a stylist whose appointment was moved to someone else keeps access to it (the old allocation stays on record as released).

## Endpoints (v1)

Everything is under `/v1`; the live, complete schema is at `/docs` in development (switched off in production unless `DOCS_ENABLED=true`: *Security review*). Credentials: *Auth model*.

| Area | Endpoints |
|---|---|
| Open (no key) | `GET /countries`, `GET /captcha`, `GET /signup/config`, `POST /signup`, `POST /signup/verify`, `POST /owner/login`, `POST /owner/login/verify`, `GET /calendar/oauth/callback` (Google's redirect: the single-use `state` is the credential); outside `/v1`: `GET /healthz`, `GET /readyz` |
| Public (site key `X-Site-Key`) | `GET /public/services`, `/public/resources`, `/public/products`, `/public/supported-vehicles`, `/public/business`, `/public/reviews`, `/public/reviews/summary`; `GET /public/availability`, `/public/availability/month` (*Month view*); `POST /public/bookings` (bot check: *Bot check*; `Idempotency-Key` header: *Sending a booking twice*); `POST /public/geocode` |
| Customer sign-in | `POST /public/auth/otp`, `/public/auth/otp/verify`, `/public/auth/magic-link`, `/public/auth/magic-link/verify`, `/public/auth/email`, `/public/auth/email/confirm` (*Customer login and cancellation*, *Customer identity*) |
| Customer (site key + customer token) | `GET /public/me/bookings`; `POST /public/bookings/{id}/cancel`, `/reschedule`, `/review`; `GET\|POST /public/me/addresses`, `PATCH\|DELETE .../{id}`; `GET\|POST /public/me/vehicles`, `PATCH\|DELETE .../{id}`; `GET\|PATCH /public/me/profile`; `GET\|POST /public/me/data-requests`; `POST /public/review-invitation`, `/public/review-invitation/review` (the signed link instead of a login: *Review invitations*) |
| Admin: bookings | `GET /admin/bookings` (filters `date`, `status`, `resource_id`, `flagged`, `flag`, ...), `GET /admin/bookings/{id}`, `PATCH .../status`, `POST .../reschedule`, `.../clear-flags`, `.../items`, `.../follow-up`, `.../resume`, `.../resolve-conflict`; `PATCH .../actual-duration`, `.../meeting-link`; `GET\|PUT .../parts`; `GET .../history`. Every booking in these answers carries `customer_name`, `customer_phone`, `contact_email` (*Roles*, "Who booked") |
| Admin: what is sold and who does it | `GET\|POST /admin/services`, `PATCH .../{id}`, `PUT .../{id}/parts`; `GET\|POST /admin/resources`, `PATCH .../{id}`, `GET\|PUT .../{id}/shifts`; `GET\|POST /admin/products`, `PUT .../{id}`; `GET\|POST /admin/supported-vehicles`, `DELETE .../{id}` |
| Admin: rules and settings | `GET /admin/site` (manager), `PUT /admin/site` (owner: *Free plan: one account, one business, one website*); `GET\|POST /admin/policies`; `PUT /admin/operating-hours`; `GET\|POST /admin/days-off`, `DELETE .../{id}`; `GET\|PUT /admin/time-slot-settings`, `/admin/table-open-rules`, `/admin/business-profile`, `/admin/business-location`, `/admin/business-settings` (*Countries, time zones and daylight saving*); `GET /admin/plan` (*Plans and limits*) |
| Admin: messages | `GET\|PUT\|DELETE /admin/reminder-settings`; `GET /admin/notification-templates`, `/admin/notification-placeholders`, `PUT\|DELETE .../{event}/{channel}`; `GET /admin/notifications` (the log) |
| Admin: integrations | `/admin/webhooks` (create, list, `PATCH\|DELETE .../{id}`, `.../deliveries`, `.../deliveries/{id}/redeliver`, `.../rotate-secret`, `.../test`), `GET /admin/webhook-events` (*Outgoing webhooks*); `/admin/calendar/connect`, `/admin/calendar/connections` and `.../{id}`, `.../{id}/resync` (*Google Calendar sync*) |
| Admin: people, reviews, privacy | `GET /admin/users`, `PATCH .../{id}`; `GET /admin/reviews`, `PATCH .../{id}`; owners: `GET /admin/audit-log`, `GET /admin/data-requests`, `.../{id}/export`, `.../complete`, `.../reject` (*Schema tables of blueprint §12.3*) |
| Keys and tokens | `POST /auth/token` (secret key -> admin token); owner: `GET\|POST /auth/api-keys`, `POST /auth/api-keys/recover`, `DELETE /auth/api-keys/{id}` (*Security review*, *Owner sign-in and lost keys*) |
| Platform operator (`X-Platform-Token`) | `POST /auth/tenants`; `GET /platform/plans`; `GET /platform/tenants/{id}/plan`, `PUT /platform/tenants/{id}/plan`, `PUT /platform/tenants/{id}/overrides`, `PUT /platform/tenants/{id}/owner-email` |
| WordPress connector | `POST /sync/webhook` (signed with the site's webhook secret) |

## Google Calendar sync

Confirmed bookings are copied to a Google calendar: the business's own (every booking) and/or a staff member's (their bookings). **One way**: the booking system is the truth; an
event edited in Google is overwritten the next time its booking changes, and nothing is read back. Code: `app/services/calendar.py`, `app/api/v1/calendar.py`; tables `calendar_connections` and `calendar_events` (migration 0025, Row-Level Security).

**What is on the calendar.** An event exists while the booking is `confirmed`, `in-progress`, `waiting-for-parts` or `completed`; it is removed when the booking is `cancelled`, `rejected` or `no-show` (and a `pending` one has none yet).
It is moved when the booking is rescheduled, and moves to another staff member's calendar when the booking changes hands. A business calendar receives every booking; a staff calendar the ones that member is allocated to.
**Privacy:** an event carries the services, the staff, the status and a short booking reference. The customer's name and phone, the party size, their request and the address are added only if the owner turned on `share_customer_details`
for that calendar (Google is then a processor of that data: list it in the privacy notice). Health notes are never sent. Tokens are stored encrypted (AES-GCM, tied to the business).

**How it stays right.** A booking change on the outbox only *nudges* (`CalendarPublisher` marks that booking's `calendar_events` rows `pending`); the sweep then looks at the booking *as it is then* and makes Google match it, one request at a time
(`CALENDAR_BATCH` = 50 per business per sweep). So retries, reordering and repeated nudges end in the same place, an unchanged booking costs no request (a hash of what was last sent), and the event id is derived from the row, so a retry after a lost answer finds the event it made instead of making a second.
Failures: a server error, a rate limit (429, 403 `rateLimitExceeded`) or a network error is retried with the outbox back-off, up to `CALENDAR_MAX_ATTEMPTS` (8); anything else Google refuses for good (400, other 403, 404) marks that event `failed`. A calendar whose events fail `CALENDAR_PAUSE_AFTER_FAILURES` (10) times in a row, or
whose saved sign-in Google no longer accepts (revoked in the Google account), is **paused** (`needs_reconnect`; its events wait, not lost) and **the business is emailed** at its profile contact email.

**Connecting** (Google's OAuth 2.0 web flow; scope `calendar.events` = events only; offline access):
1. `POST /v1/admin/calendar/connect {"resource_id"?, "calendar_id"?: "primary", "share_customer_details"?: false}` (owner) returns `authorization_url`; send the owner's browser there. `resource_id` (a staff member) = that member's own calendar; left out = the business's. Connecting the same one again replaces its saved sign-in (this is also how a paused calendar is revived).
2. The owner allows access at Google; Google sends the browser to `GET /v1/calendar/oauth/callback` (no key: the single-use `state`, kept in Redis for ten minutes, is the credential). The server exchanges the code, checks the calendar can be read, stores the connection, queues every booking that belongs in it (those not over for more than a day), and redirects to `CALENDAR_DONE_URL`
   with `?calendar=connected` or `?calendar=error&reason=...` (`invalid_state`, `denied`, `google_error`, `google_refused`, `google_unavailable`, `no_refresh_token`, `resource_gone`); without that setting it answers with JSON.
3. `GET /v1/admin/calendar/connections` (manager) lists them with how their sync is going (never a token); `PATCH .../{id}` (owner) turns customer details on or off and rewrites every event; `POST .../{id}/resync` (owner) writes everything again and gives failed events another chance; `DELETE .../{id}` (owner) asks Google to revoke the sign-in and forgets the connection - events already in the calendar stay there.

**Set-up on your side** (nothing here can be done by the code): in Google Cloud create an OAuth client of type *web application*, enable the *Google Calendar API*, add `GOOGLE_OAUTH_REDIRECT_URI` (this server's `/v1/calendar/oauth/callback`) as an authorised redirect URI, and set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI` (all three or none).
`calendar.events` is a *sensitive* scope: while the app is in "testing" only listed test users can connect (and their sign-in lapses after 7 days); letting any business connect needs Google's **app verification** (a consent-screen review with a privacy policy). That is a launch task for you.

**Not built / not verified.** Tested against a **fake Google only**: no real OAuth client, consent screen, token or calendar has been used, so Google's exact behaviour for restoring a deleted event, quotas and the consent screen are unverified. Not built: reading changes back from Google, attendees or invitations, a calendar per service, choosing which booking statuses appear, a plan limit on calendars,
an event for a booking whose details change without a status or time change (adding an add-on to a running appointment does not nudge the calendar until the booking changes again; **Resync** fixes it), and removing the events when a calendar is disconnected.

## Logging and error reporting

Every log line, from our code and from the libraries, goes through one handler on standard output (`app/core/logs.py`). Settings: `LOG_LEVEL` (`INFO`), `LOG_FORMAT` (`json`, `text` or `auto` = text in development and tests, JSON elsewhere), `SENTRY_DSN`.

* **JSON lines**: `time` (UTC), `level`, `logger`, `message`, `request_id`, `tenant_id` and, for the access line, `method`, `path`, `route`, `status`, `duration_ms`, `client`, `tenant`; a traceback is in `exception`. One object per line, so any log collector reads it.
* **Request id**: every response has `X-Request-ID` (browsers may read it: it is exposed through CORS, with `Retry-After` and the `X-RateLimit-*` headers). A caller's own id is kept when it is 8-64 characters of `A-Za-z0-9._-` (so a proxy's id flows through), otherwise one is made.
  Every line written while serving the request carries it, and `tenant_id` once the business is known. The worker gives each sweep its own id (`sweep-…`).
* **Access log** (`app.access`): one line per request, at `INFO`; 401, 403, 429 and 5xx are `WARNING`; `/healthz` and `/readyz` are `DEBUG`. **Never the query string, headers or body** (they carry tokens and personal data). `route` is the path with each id replaced by its name
  (`/v1/admin/resources/{resource_id}`), `null` for a path that is no route. Uvicorn's own access line is switched off in favour of this one.
* **Unexpected errors**: a traceback at `ERROR` (with the request id) and a `500 {"error": "internal_error", "detail": …, "request_id": …}` that says nothing about the exception (it could contain SQL or a secret). The service keeps answering.
* **Secrets are scrubbed from every line** (`logs.redact`, run over the message and the traceback): bearer tokens, JWTs, our `pk_`/`sk_` keys, Google API keys, `key=` / `token=` / `password=` / `secret=` / `signature=` values, `Authorization`-style header values and the password in a database URL.
  `httpx` is held at `WARNING` because it logs every request URL (with Google's key in it). This is a safety net: the code does not log secrets in the first place.
* **Sentry**: only when `SENTRY_DSN` is set. Every `ERROR` log line (all unexpected errors, and each "giving up" in the code) becomes an event, tagged with the request id and business. No request data, headers, cookies, local variables or personal data are sent, and the text that is sent is scrubbed as above.
  If the DSN is set but `sentry-sdk` is missing it says so in the log and carries on.
* **Not built**: tracing and performance monitoring, metrics (request counts, latencies, queue depth), OpenTelemetry, log shipping, alert rules. **Not verified**: delivery to a real Sentry project (the tests use a collecting transport).

## Bot check (Cloudflare Turnstile)

In front of **`POST /v1/signup`** and **`POST /v1/public/bookings`** (guest booking). Off unless `CAPTCHA_PROVIDER=turnstile` with `TURNSTILE_SITE_KEY` (public, goes into the page) and `TURNSTILE_SECRET_KEY` (stays here); the server refuses to start with the provider set and a key missing.
Free: Cloudflare lists Turnstile's free plan with unlimited challenges (20 widgets per account, 10 hostnames per widget); check its plans page, third parties quote other limits.

How a page uses it:
1. `GET /v1/captcha` (no key needed; the same object is in `GET /v1/signup/config`) returns `{"enabled", "provider", "site_key", "protects": ["signup", "booking"]}`.
2. Build Turnstile's widget with that `site_key` and **`action` set to `signup` or `booking`**.
3. Send the token the widget produces in the **`X-Captcha-Token`** header of the request. A token lives 5 minutes and works once, so a new widget run (`turnstile.reset`) is needed for each request.

What the server does (`app/services/captcha.py`), before anything else:
* no header -> `403 captcha_required`; Turnstile refuses it (wrong, expired, already used) or it was made for the other form -> `403 captcha_failed` with a `reason`; over 2048 characters -> refused without asking Cloudflare. Nothing is created, sent or counted (no email, no cool-down, no daily-cap use).
* Cloudflare unreachable, an unreadable answer, or our secret refused (a misconfiguration: logged as an error) -> `503 captcha_unavailable`: nobody gets in unchecked. `CAPTCHA_FAIL_OPEN=true` lets requests through instead (and logs an error) so customers can still book during an outage; so can bots.
* **Exempt:** a request made with a *secret* API key (the WordPress server booking for a visitor: it has no widget to show and holds administrator power anyway). A site key request is never exempt.
* The sign-up confirmation (`POST /signup/verify`) needs no token: the emailed link is the proof there. Reading (availability, services...) needs none. The client's address is sent to Cloudflare when it is a plain IP address.
* The token is never logged (`X-Captcha-Token` is scrubbed from log lines and from Sentry events) and never stored.

For development, Cloudflare publishes dummy keys (site key `1x00000000000000000000AA` always passes, secret `1x0000000000000000000000000000000AA` always passes, `2x…` always fails; the dummy token is `XXXX.DUMMY.TOKEN.XXXX`) so the whole path can be tried without an account.
The verification address can be changed with `TURNSTILE_VERIFY_URL` (the tests point it at a fake). **Tested against a fake Cloudflare only; no real account, widget or token has been used.** Per-business on/off, and the check on sign-in code / link requests, are not built.

## Security review (2026-10-08)

A read through the authentication, tenant resolution, rate limiting, sign-up, sign-in, webhooks, token and configuration code, with every finding written up as a test (`tests/test_security_review.py`).

| Finding | Fix |
|---|---|
| **API keys could not be revoked or listed** (`revoked_at` was read but nothing set it): a leaked secret key - which can sign in as owner - stayed valid for ever | `GET /auth/api-keys` (never shows a key), `DELETE /auth/api-keys/{id}` (immediate, idempotent, audited). The last working secret key cannot be revoked (nobody could sign in to make another); create the new key first |
| `POST /auth/api-keys` accepted **another business's `site_id`** (`sites` and `api_keys` have no Row-Level Security, so the database did not stop it) | the site must belong to the owner's business (404 otherwise); every query on those tables names the business |
| `limit` / `offset` negative on `GET /admin/bookings` and `GET /public/me/bookings` made the database refuse the query: a **500** | clamped like the other lists |
| A **signed token with no `exp`** (or no `iat`, `sub`, `tid`, `role`) was accepted, and never expired | all five claims are required (we always issue them; this is defence in depth) |
| `ENVIRONMENT` **defaulted to `development`**: a deployment that forgot it ran with the start-up checks off and sign-in codes in the log | defaults to `production`; `docker-compose.yml` states `ENVIRONMENT: production` for `migrate`, `api` and `worker` whatever `.env` says |
| A stranger who knows a phone number or address could **fill it with sign-in codes / links / sign-up emails** (1 a minute, for ever) and run up the business's WhatsApp / SMS bill | at most `SIGN_IN_REQUESTS_PER_DAY` (10) codes or links per phone / email per business, and `SIGNUP_REQUESTS_PER_ADDRESS_PER_DAY` (5) sign-up emails per address, per day; a message that could not be sent does not count |
| `/docs`, `/redoc`, `/openapi.json` listed every route (the operator's too) in production | served only in development and tests, or when `DOCS_ENABLED=true` |
| No browser-safety headers | `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` on every API answer (not the docs page; an endpoint's own value is kept); `Strict-Transport-Security` when the request came over TLS (behind a proxy: when it says so in `X-Forwarded-Proto` **and** `RATE_LIMIT_TRUSTED_PROXIES` > 0) |

**A guard for the future**: the tests walk every route in the OpenAPI schema and check that a caller with no credentials is refused everywhere except the four sign-up / country routes, that a publishable key alone opens nothing administrative, that a customer's token opens nothing administrative,
and that neither an administrator's token nor a secret key opens an operator route (and the operator's token opens nothing of a business). A route added without its dependency fails these.

**Looked at and found sound** (already covered by tests): SQL (no string-built SQL anywhere), the SSRF checks on webhooks (the address is resolved again when the delivery is sent), signature checks (constant-time comparison, a replay window), the one-time use of sign-in codes, magic links and sign-up tokens (GETDEL), the cap of 5 wrong codes, the
OTP and magic-link answers that do not reveal whether a person is registered, AES-GCM encryption bound to the business, Row-Level Security with three database roles and the start-up refusal when it is off, no secrets in settings representations (`SecretStr`), `PLATFORM_ADMIN_TOKEN` compared in constant time.

**Known risks that remain** (decisions, or work not done):
* **Guest booking is open to anyone holding the publishable key** (it is in the web page). Rate limits (per address and per business) and the plan's monthly cap bound it, but a botnet could fill a free business's monthly allowance with bookings that are never honoured. Switch on the bot check (*Bot check*) to close this: without `CAPTCHA_PROVIDER` it stays open. It does not cover the sign-in code and link requests (the daily caps do), or customers' cancel / reschedule (they need a signed-in token).
* The sign-in answers differ in **time** between a known and an unknown address (a send takes longer than a refusal): a patient attacker might tell them apart. Not addressed.
* **Redis** now has a password in `docker-compose.yml` (`REDIS_PASSWORD`, built into `REDIS_URL`) and the start-up refuses a `REDIS_URL` without one anywhere but this machine - but that compose file has never been run, and Redis traffic is not encrypted (use `rediss://` to a managed Redis, or keep it on a private network).
* A secret key is full administrator power for its business, by design (the WordPress server signs in with it): treat it like a password. A lost key cannot be read back, but the owner can sign in by email and get a new pair (*Owner sign-in and lost keys*).
* An administrator's token lives 15 minutes and cannot be revoked inside that time (a disabled *user* is refused at once).
* No account lock-out or alerting on repeated failures beyond the rate limits; no audit entry for failed sign-ins; the dependency and static scans were run on 2026-10-13 (*Dependency and static scans*).
* **Code reviews since then**: the role each admin route needs (2026-10-12, see *Roles*), and two read-throughs of the services (2026-10-13: booking, allocation, notifications, reminders; 2026-10-14: webhooks, calendar, PDPA, customer profile, reviews, availability view, geocoding). All nine findings are fixed (*Customer identity*, *Refused messages* under *Notifications*, *Sending a booking twice*, *Status moves and the clock*, saved addresses and vehicles, erasure, webhook SSRF); left on purpose: a sign-in code waits behind a slow gateway, the sweep makes its network calls one business at a time, and an add-on can run past closing time. `/security-review` over a real diff, and a penetration test, are still to do.

## Dependency and static scans (2026-10-13)

Run with `bash deploy/security_scan.sh` (needs network; builds a throw-away environment, so the project's own is untouched; non-zero exit if either tool finds something).

* **`pip-audit` 2.10.1: no known vulnerabilities**, on the 53 packages installed in the environment the tests ran against *and* on a fresh resolution of `requirements.txt` (47 packages: what a new image build installs today).
* **`bandit` 1.9.4** over `app/` (11,730 lines) and `alembic/` (1,507): 0 high, 0 medium, **11 low**: ten `assert` statements (B101) and one false alarm (B105, `REVIEW_TOKEN_TYPE = "review"`, the name of a kind of token).
  Each assert was read: they narrow a type for `mypy` or state what cannot happen (a foreign key, a schema rule, an import-time check that two tables list the same names); none stands in for a check, and under `python -O` they would fail loudly
  (an `AttributeError`, a refusal with no numbers) rather than let anything through - except one, the business-location handler (`PUT /admin/business-location`), where a missing coordinate would have been written to the database. **That one is now an explicit
  check** (`tests/test_hardening.py`). B101 is therefore skipped on purpose in `pyproject.toml` (`[tool.bandit]`, with the reason), the one B105 carries `# nosec B105`, and a test pins that the Dockerfile and the compose files never run Python with `-O` / `PYTHONOPTIMIZE`
  (that is what makes skipping B101 safe). `tests/` is left out of the scan.
* **Found and not changed (they touch the Docker build, which has never been run here):**
  1. `requirements.txt` is unpinned (`>=`). The image installs whatever is newest on the day, and a fresh resolution already differs from the environment the 2,000+ tests ran against (`fastapi` 0.143.0 against 0.141.1, `sqlalchemy` 2.1.4 against 2.1.1,
     `pydantic` 2.14.1 against 2.13.5, `pydantic-core` 2.50.1 against 2.46.5, `asyncpg` 0.32.0 against 0.31.0, ...). Recommended: a lock file generated from the tested environment (with hashes) that the Dockerfile installs with `--require-hashes`, refreshed on purpose and
     re-audited each time.
  2. The production image installs the test tools too (`pytest`, `pytest-asyncio`, `fakeredis` are in `requirements.txt`). Recommended: split the runtime packages from the test ones.
* **Not covered**: a scan is a snapshot (a vulnerability published tomorrow is not in it: run the script on a schedule or in CI, and there is no CI yet); neither tool looks for secrets in the files or at the container image (no Docker here to build one); neither is a penetration test.

## Production setup

The tenant isolation, the encryption of health notes and the rate limits only protect anyone if the deployment is set up so that they cannot be bypassed. This is what that takes.

**1. Three database roles** (`deploy/postgres/01_roles.sql`, run once as a superuser per database; safe to run again):

| Role | Used by | Can |
|---|---|---|
| `booking_owner` | the `migrate` step only (`MIGRATION_DATABASE_URL`) | owns the schema, runs Alembic; not a superuser |
| `booking_app` | the API and the worker (`DATABASE_URL`) | read and write rows; **no DDL, no `BYPASSRLS`, owns nothing**, cannot touch `alembic_version` or PostGIS's tables |
| `booking_backup` | `deploy/backup.sh` | read everything (`pg_read_all_data`, `BYPASSRLS`), change nothing |

Why not one role: the policies are `FORCE`d, so they bind the owner too - but the owner can still `ALTER TABLE ... DISABLE ROW LEVEL SECURITY`, and a superuser or `BYPASSRLS` role ignores them
altogether. With the application as `booking_app`, a bug or injection in it cannot lift tenant isolation. `pg_dump` refuses to run as a role the policies apply to (it would dump only part of the
data), which is why backups have their own role. PostGIS is not a "trusted" extension, so the script creates it (and `btree_gist`) as the superuser and the owner never needs superuser rights. Migrations pick up
`MIGRATION_DATABASE_URL` (else `DATABASE_URL`); after migrating, `alembic/env.py` takes `alembic_version` away from `booking_app`.

**2. Start-up checks** (`app/core/hardening.py`; skipped for `ENVIRONMENT=development` and `test`). The API and the worker refuse to start (`StartupRefused`, naming every problem) when:
the database role is a superuser or has `BYPASSRLS`, or the database says Row-Level Security is not in force for any tenant table for that role (`row_security_active`; the identity tables `tenants`,
`sites`, `api_keys` are exempt by design); `JWT_SECRET` or `PLATFORM_ADMIN_TOKEN` looks like a placeholder (`change-me`, `example`, `password`, ...); `DATABASE_URL` still has the example credentials
`booking:booking`; `CORS_ORIGINS` contains `*`; `WEBHOOK_ALLOW_INSECURE=true`; `RATE_LIMIT_ENABLED=false`. It only *warns* when the application role owns the tables (it could switch RLS off).
`ENVIRONMENT` defaults to `production`, so a deployment that never sets it is checked. `REDIS_URL` pointing anywhere but this machine must carry a password (`redis://:PASSWORD@host:6379/0`). `PDPA_ENCRYPTION_KEY` must be base64 of exactly 32 bytes in every environment (checked when the settings load, not on the first health note). `ALLOW_UNSAFE_STARTUP=true` turns the refusal into
critical log lines - an emergency switch, not a setting to keep. `GET /healthz` is liveness only; `GET /readyz` is 503 when the database is unreachable and `degraded` (still 200) when only Redis is.

**3. Compose.** `docker-compose.yml` is the production-shaped stack: `postgres` (roles created on first start by `deploy/postgres/01_roles.sh`), `redis`, a one-shot `migrate` (as the owner), then `api` and
`worker` (as `booking_app`) which wait for it. Neither database nor Redis is published on the host, and compose stops if a password is unset. Put a TLS-terminating reverse proxy in front of the API and set
`RATE_LIMIT_TRUSTED_PROXIES` to the number of proxies; set `CORS_ORIGINS` to the real sites; use `?ssl=require` on the database URLs when the database is not on the same private network. Passwords go into URLs, so use URL-safe characters.
`docker-compose.dev.yml` is the old single-role stack, for development only.

**4. Secrets.** `JWT_SECRET`, `PLATFORM_ADMIN_TOKEN`, `PDPA_ENCRYPTION_KEY`, the database passwords and the WhatsApp/Twilio/SMTP credentials belong in a secrets manager or the platform's secret store, not in the image or the repository.
**Back up `PDPA_ENCRYPTION_KEY` separately from the database**: health notes are stored encrypted with it, so a restored backup without the key cannot read them (and a leaked key without the data is useless).

**5. Backups.** `deploy/backup.sh` (as `booking_backup`: `BACKUP_DIR`, `KEEP_DAYS`, the usual `PG*` variables) writes a compressed `pg_dump` archive, checks that it can be read back before keeping it, never lets a partial dump replace a good one, and
prunes archives older than `KEEP_DAYS`. Schedule it (daily at least) and copy the archives off the machine. `deploy/restore.sh <dump> <new database>` restores into a *new* database as a superuser, refuses to overwrite one, drops a half-restored database, and
fails if any tenant table came back without enforced Row-Level Security. Practise a restore before you need one. Redis holds only short-lived codes and caches and is not backed up.

**Verified, and not.** Checked by `tests/test_hardening.py` and by a scripted run on a scratch PostgreSQL 16 (Windows build, PostGIS 3.6.2, trust authentication): the roles script (twice, idempotent), migrating as the owner, eight things the application role cannot do
(DDL, truncate, bookkeeping, writing PostGIS tables...), a full booking flow through the real API as `booking_app` (tenants, booking, availability, admin reschedule, encrypted health notes, audit log, the sweep), isolation between two businesses (without a tenant the app role
sees no bookings), the start-up checks with the real roles (accepted / warned / refused), and backup -> restore (grants and enforced RLS intact; a backup without RLS is rejected). **Not verified:** the compose file and the Docker images themselves (never run: no Docker here), the PostGIS 3.4 image,
password (scram) authentication and TLS to the database, a real Redis, the reverse proxy, a real Sentry project. **Not built:** tracing / OpenTelemetry, metrics, connection pooling in front of PostgreSQL, scheduled backups (the script is yours to schedule), secret rotation, per-business WhatsApp/SMS accounts. (JSON logging and Sentry: see *Logging and error reporting*.)

## Known limits

* Webhooks: see *Outgoing webhooks* for what is not built. The SSRF checks use the interpreter's `ipaddress` classification plus explicit rules for IPv6 forms that embed IPv4 addresses (redundant on
  Python 3.14, relied on by the 3.12 container image, which could not be run here).
* Rate limiting: see *Rate limiting* for what is not built; behind a reverse proxy `RATE_LIMIT_TRUSTED_PROXIES` has to be set or the limits apply to the proxy's address.
* Notifications: the WhatsApp Cloud API and Twilio SMS providers are tested against a fake HTTP transport only (Meta's template approval, the real payload rules and a live number are unverified); one WhatsApp number and one SMS account for the whole platform
  (no per-business accounts); only Twilio among SMS gateways; only a narrow list of provider answers counts as a permanent refusal (see *Refused messages* under *Notifications*), everything else is retried until the attempts run out; WhatsApp templates are sent in one language (`WHATSAPP_TEMPLATE_LANGUAGE`);
  reminders have no per-customer opt-out; no delayed or repeated review invitation (one message, when the visit is marked completed); no customer opt-out or per-customer channel preference (everyone with an email gets the email default);
  no localisation (one wording per template); messages are sent from the worker's outbox pass, so with no worker running nothing is sent;
  the SMTP provider is unverified against a real server, and so is every live delivery.

* Plans: see *Plans and limits* for what is not built (no billing, no feature switches: only the limits listed there); the free numbers are proposals; a business over a limit after a downgrade is only told, nothing is enforced retroactively.
* Countries: only Malaysia, Singapore and Australia, one country per business, fixed at sign-up (no change afterwards). Postcodes and states are checked by shape and list, not against the post office's data; phone numbers are still only respelled, not checked against a country's numbering plan.
  Self-service sign-up exists but is off by default (see *Self-service sign-up*); the platform operator can still create a business (`POST /auth/tenants`), where an address is optional. Daylight saving is tested in Sydney, Melbourne, Hobart, Adelaide (half-hour offsets) and Lord Howe (a 30-minute change), spring and autumn, for every kind of business
  (`tests/test_dst.py`, `test_dst_nights.py`, `test_dst_zones.py`), and in Brisbane (none); Broken Hill (same clock as Adelaide) and the zones without a change (Perth, Darwin, Eucla) are not tested separately. The slot grid steps in real time, so it keeps
  to the wall clock only when the step divides the clock change: the default 15- and 30-minute steps always do, but an **hourly** grid (`SLOT_STEP_MINUTES=60`, or a restaurant interval of an hour) reads xx:30 instead of xx:00 on Lord Howe between the two changes
  (pinned by a test, not fixed). A booking made before the owner changes the time zone keeps its moment (it is stored in UTC) but its place in the day moves with the zone. Nothing here has run in a real browser or against real Google.
* Audit log: only *successful* changes are recorded (refused attempts are not); requests are recorded at the HTTP level, so
  the log says which route changed which record, not what the new values were. Disabling a user does not revoke a customer
  session (customers are not users). There is no endpoint to purge or export the audit log.
* Users: WordPress remains the authority for roles - the recorded roles are what it last asserted, not a permission check;
  the token exchange still trusts the secret-key caller. Each admin request costs one extra tiny query (the disabled check).
* Reviews: one review per booking, no edits or replies; the invitation is one message sent when the visit is marked completed (no delay, no reminder, no way for a customer to opt out of it other than the business editing the template).

* Field service: no video rooms are made (the business brings its own meeting service: a saved room per consultant or a link per booking, see *Virtual appointments*); typed addresses are looked up only through Google (see *Typed addresses*; untested against the real API); no route optimisation between consecutive on-site visits; spare parts on a job are a flat list (see *Parts on the job order*), and
  the "actual duration shorter than planned" case (which does not release time early) is not modelled.
* Salon: add-ons are not tied to particular base services (any add-on goes with any base); products are
  added to the ticket at the salon, not while booking online; two tickets that both claimed the last unit are
  clamped to zero at completion rather than blocked; the stylist's alert goes by email and SMS only to the contact details a manager saved on their staff
  profile (and as a webhook event for a plugin to push): there is no push notification of our own, no escalation to the manager, and WhatsApp is not used for it (it needs an approved template).
* Automotive: parts on a job order are a flat list with stock checked, not reserved (see *Parts on the job order*); the customer is told of `waiting-for-parts` by the default email; a bay is not held between segments for a
  car that stays put (each segment picks a bay independently, preferring the previous one).
* Restaurant: table choice is opt-in per tenant and only ever a single table (the system combines tables by itself when the party needs it, but the customer cannot choose a combination). Google Calendar sync: see *Google Calendar sync*.
* Overnight hours: overlapping hours/shifts for the same weekday are not validated; if today's own window and yesterday's
  overnight window both offer the same instant, it is resolved to today (calendar date first); the admin booking list's
  `?date=` filters by the calendar date of the start, so an after-midnight booking is listed under the next date.
* Review flags: qualification is judged the way the booking was made (see the table above) since `booking_allocations.service_id` (migration `0019`, set for every workshop
  segment, including resumed and moved jobs). Rows from before it keep the old lenient rule when they are a *multi-service workshop job order* (nothing says which segment a
  row belongs to: qualified for at least one of its services); migration `0019` filled the column in for workshop bookings with exactly one service. A segment's service is
  recorded when it is booked, so a job order edited later (services added to a ticket) is not re-segmented. Resources are deactivated, not deleted (bookings reference them), and `PUT /table-open-rules` is not
  re-validated when tables are deactivated (its check counts active tables only when a rule is *saved*).
  Extensions are not a gap: an upsell or an overrun only ever stretches an `in-progress` appointment, which review flags never cover (they are dropped when the appointment
  starts), and nothing leads from `in-progress` / `waiting-for-parts` back to `pending` / `confirmed` except `resume`, which hands the booking back to the sweep. Tests pin that
  chain down (`tests/test_flag_extensions.py`), and a successful extension also resets the sweep marker, so the guarantee survives if extensions ever reach live bookings.
* The availability cache is invalidated per date for bookings (see *Availability cache*) but per tenant for settings; a cold month view runs the pipeline once per date
  (no bulk month computation); no Celery or RabbitMQ (the sweep, the outbox and the reminders run in a plain asyncio worker: see *Background sweep*). The cache has been tested against `fakeredis`, not a real Redis server.
* Customer accounts: phone (OTP) or email (magic link) login, no shared accounts across tenants. OTP codes and magic links go out through the
  delivery providers (see *Notifications*): in production they need an email provider (SMTP) and, for OTP, a WhatsApp or SMS provider (both built, tested against a fake gateway only). Phones are normalised for spelling, not validated per country; the country
  code is set at onboarding only (no endpoint to change it later); emails are not verified when a booking is made; there is no
  guest manage-token (a guest signs in by phone or email); requests are limited per client address and per business (see *Rate limiting*) on top of the per-phone / per-email cool-downs and daily caps.
* Rescheduling: see *Customer rescheduling* and *Admin rescheduling* for what is not done (virtual join links, a move limit, bulk moves, asking the customer first).
* PostGIS (compose image `postgis/postgis:16-3.4`) is used for on-site radius checks; that image and the Dockerfile
  have not been run here (no Docker on the development machine), and PostGIS was tested on 3.6.2.
