# JustSimple demo API

The real booking engine (`backend/`, unchanged) running on the internet with five sample businesses on the free plan, for demos and for
building against (a chatbot, an app, a website). Started by `demo/server.py`.

**It is a public demo.** Every key, including the secret keys, is shown on its start page, and everyone shares the same data. Nothing is
really sent: WhatsApp, SMS and email messages (sign-in codes too) are kept in memory and shown at `/demo/messages`. Never put real
customers or a real business into it.

## Deploy on Render (about 15 minutes, once)

1. Sign up at <https://render.com> (a GitHub sign-in is simplest) and allow Render to see the repository `Appointment_Wordpress_Plugin`.
2. **New > Blueprint**, pick the repository. Render reads `render.yaml`: one web service `justsimple-demo-api` and one database
   `justsimple-demo-db`, both free, in Singapore. Press **Apply**.
3. Wait for the first deploy (5-10 minutes: it installs the packages, builds the database, then adds the sample data, which takes a minute or two
   more on the first start only). The log ends with `[demo] ready on port ...`.
4. Open the service's address (`https://justsimple-demo-api.onrender.com` or similar). The start page lists the businesses and their keys; the API
   documentation is at `/docs`.

Good to know:
* **Free web service sleeps** after 15 minutes without visitors; the next request takes 30-60 seconds. Open the page a minute before a demo.
* **Free database expires after 30 days.** Render emails before; then create a new one (the next start fills it again) or move to a paid plan (from about US$7 a month).
* **Messages are kept in memory**, so they are gone after a restart or a sleep. Bookings and businesses are in the database and stay.
* If the start fails with `PDPA_ENCRYPTION_KEY must decode to 32 bytes`, set it by hand in the service's Environment tab to the output of
  `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` and deploy again.
* If it fails with `Could not create the postgis extension`, the database plan does not offer PostGIS: tell me and we pick another host.
* **Starting again from fresh sample data:** delete the row in the `demo_state` table and every business (or create a new database), then restart.

## For the chatbot developer

Base address: the Render address above (`$BASE` below). Every answer is JSON; errors are `{"error": "...", "detail": "..."}` with an HTTP status.
The full list, with every field, is at `$BASE/docs` (OpenAPI: `$BASE/openapi.json`, from which a client can be generated). The keys are at `$BASE/demo/info`.

### Two sides, two keys

| Who | Key | Header |
|---|---|---|
| The customer side (what a visitor or a chatbot talking to customers does) | publishable key `pk_...` | `X-Site-Key: pk_...` |
| The business side (staff, owner, a back-office bot) | secret key `sk_...`, swapped for a token | `X-API-Key: sk_...` once, then `Authorization: Bearer <token>` |

The secret key is a server-side secret in a real deployment (in a chatbot's backend, never in a browser). In the demo it is public on purpose.

### Customer side: find a time and book

```bash
PK=pk_...   # from $BASE/demo/info
curl "$BASE/v1/public/business" -H "X-Site-Key: $PK"                         # name, address, hours text, rating
curl "$BASE/v1/public/services" -H "X-Site-Key: $PK"                          # services with ids, duration, price
curl "$BASE/v1/public/resources" -H "X-Site-Key: $PK"                         # staff (or tables, bays)
curl "$BASE/v1/public/availability/month?month=2026-11&service_ids=SID" -H "X-Site-Key: $PK"   # which days have room
curl "$BASE/v1/public/availability?date=2026-11-03&service_ids=SID" -H "X-Site-Key: $PK"       # free times that day
curl -X POST "$BASE/v1/public/bookings" -H "X-Site-Key: $PK" -H "Content-Type: application/json" \
  -H "Idempotency-Key: chat-1234" \
  -d '{"service_ids": ["SID"], "start_at": "2026-11-03T10:00:00+08:00", "customer": {"name": "Aina", "phone": "+60123000000", "email": "aina@example.com"}}'
```
* Restaurants: `party_size` (and `table_type`: `normal` or `vip`) instead of `service_ids`. Car workshop: a `vehicle`. Home services: an `address`
  with `latitude` / `longitude` (or `POST /v1/public/geocode` first). The docs page shows each.
* A time taken in the meantime is `409`; a rule broken (too short notice, outside hours) is `422` with the reason in `detail`.
* Send an `Idempotency-Key` header with each new booking: if the chatbot retries after a lost answer, it gets the same booking back instead of a second one.

**The customer's own bookings** (cancel, move, review) need the customer signed in:
```bash
curl -X POST "$BASE/v1/public/auth/otp" -H "X-Site-Key: $PK" -H "Content-Type: application/json" -d '{"phone": "+60123456789"}'
curl "$BASE/demo/messages?to=%2B60123456789"                                  # the demo shows the code that was "sent"
curl -X POST "$BASE/v1/public/auth/otp/verify" -H "X-Site-Key: $PK" -H "Content-Type: application/json" -d '{"phone": "+60123456789", "code": "123456"}'
curl "$BASE/v1/public/me/bookings" -H "X-Site-Key: $PK" -H "Authorization: Bearer <access_token>"
curl -X POST "$BASE/v1/public/bookings/<id>/cancel" -H "X-Site-Key: $PK" -H "Authorization: Bearer <access_token>"
curl -X POST "$BASE/v1/public/bookings/<id>/reschedule" -H "X-Site-Key: $PK" -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" -d '{"start_at": "..."}'
```
The sample customer (phone +60123456789, email aisha.rahman@gmail.com) already has bookings at every business.

### Business side: read and manage

```bash
SK=sk_...
TOKEN=$(curl -s -X POST "$BASE/v1/auth/token" -H "X-API-Key: $SK" -H "Content-Type: application/json" \
  -d '{"user_id": "chatbot", "role": "manager", "display_name": "Chatbot"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl "$BASE/v1/admin/bookings?date=2026-11-03" -H "Authorization: Bearer $TOKEN"                 # the day's bookings, with customer name and phone
curl "$BASE/v1/admin/bookings?status=pending" -H "Authorization: Bearer $TOKEN"                  # requests waiting for an answer
curl -X PATCH "$BASE/v1/admin/bookings/<id>/status" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"status": "confirmed"}'
```
Roles: `owner` (everything), `manager` (bookings, services, staff, settings), `staff` (bookings; with `resource_id`, only their own).
Tokens last a short time: ask for a new one when a call answers `401`.

### Being told instead of asking

`POST /v1/admin/webhooks` (owner) registers an address that receives signed events (`booking.created`, `booking.confirmed`, `booking.cancelled`,
`booking.rescheduled`, ...): a chatbot can tell a customer the moment the business confirms. See *Outgoing webhooks* in `backend/README.md`.

### What the demo cannot show

Real WhatsApp, SMS or email delivery, Google Calendar, Google address lookup (a small table of Klang Valley places stands in for it).
Three sample businesses are on the free plan and two (the restaurant and the workshop, which need more tables and bays) on the
paid plan. The free plan's limits on staff, services and websites apply as in the product; the monthly limits on bookings and messages are
raised for the demo businesses (`DEMO_LIMITS` in `server.py`), because shared testing would use them up within days.
