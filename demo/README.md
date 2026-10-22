# JustSimple online demo: website and API

The real booking engine (`backend/`, unchanged) running on the internet with five sample businesses on the free plan. Started by `demo/server.py`.

* **The demo website, at `/`**: the same pages as the local test site (`localsite/web`, redesigned 2026-10-22; the register / log-in and `/api` pages in `demo/access.py` and `demo/pages.py` share its look): the booking page for all five kinds of business, My bookings
  (sign in with a code or an emailed link: both appear under **Messages** in the top strip), the business admin (owner / manager / staff), and
  self-service sign-up. Testers just open the link: nothing to install.
* **The API, for building against** (a chatbot, an app, a website): the start page with the keys at `/api`, the documentation at `/docs`.

**Visitors register, then log in** (`demo/access.py`). **Register** asks for a work email, the organisation and, optionally, a mobile number; **Log in** asks only for the email. Each time a link is emailed (one use, 30 minutes) and the website opens only after it is followed; a cookie then keeps the visitor in for 30 days. Log in with an address nobody registered sends nothing (the page answers the same either way); register with an address already registered sends a log-in link. Only the website is behind it; the API (`/v1`, `/docs`, `/api`) stays open.
At **`/demo/visitors`** (enter the `PLATFORM_ADMIN_TOKEN` from Render's Environment tab) you see the visitors and the latest steps, and download two CSV files: **the register and log-in log** (every registration, confirmation, log-in request, log-in, log-out, unknown address and used link, with the time in Malaysia time, email, organisation, mobile and client address) and **the visitors** (one row each, with the number of log-ins).

**The sample data resets every night** (`demo/reset.py`), at 3 am Malaysia time (`DEMO_RESET_HOUR`, an hour from 0 to 23, or `off`): the five
sample businesses and their bookings go back to fresh, and businesses visitors signed up, bookings they made and everything changed in set-up are
removed. The visitors and the register and log-in log are kept. The `/demo/visitors` page also has a **Reset the demo data now** button and shows
when the last reset ran. Render's free plan sleeps when nobody visits, so a reset missed while asleep runs as soon as the demo wakes. While a reset
runs (about half a minute here, maybe a minute or two on Render) the website shows "The demo is being reset". If a reset is cut off half-way (a
restart), the next start finds the sample businesses missing and resets again by itself.

**The sign-in needs an email account to send from.** Set these on Render (service > Environment), then redeploy:

| Setting | Example |
|---|---|
| `DEMO_SMTP_HOST` | `smtp.gmail.com` (Gmail / Google Workspace), `smtp-relay.brevo.com` (Brevo, free 300 a day) |
| `DEMO_SMTP_PORT` | `2525` (Render's free plan blocks 587; Brevo also takes 2525) |
| `DEMO_SMTP_USER` | the account's address or login |
| `DEMO_SMTP_PASSWORD` | for Gmail an **app password** (Google account > Security > 2-Step Verification > App passwords), not the normal password |
| `DEMO_SMTP_FROM` | the address the email comes from, for example `JustSimple Demo <demo@yourdomain.com>` |

Without them the sign-in page says email is not set up and nobody gets in. On your own computer, `DEMO_ACCESS_SHOW_LINK=true` shows the link on
the page instead of emailing it (never set that on Render). Only these sign-in emails are really sent; the booking system's own messages still
go to the demo inbox.

**It is a public demo.** Every key, including the secret keys, is shown on the `/api` page, and everyone shares the same data. Nothing is
really sent: WhatsApp, SMS and email messages (sign-in codes too) are kept in memory and shown at `/demo/messages`. Never put real
customers or a real business into it.

## Deploy on Render (about 15 minutes, once)

1. Sign up at <https://render.com> (a GitHub sign-in is simplest) and allow Render to see the repository `Appointment_Wordpress_Plugin`.
2. Make the database on Neon first (see *The database (Neon)* below) and copy its connection string.
   Then on Render: **New > Blueprint**, pick the repository. Render reads `render.yaml`: one free web service `justsimple-demo-api` in Singapore.
   It asks for `DATABASE_URL` (paste Neon's connection string) and the `DEMO_SMTP_*` values. Press **Apply**.
3. Wait for the first deploy (5-10 minutes: it installs the packages, builds the database, then adds the sample data, which takes a minute or two
   more on the first start only). The log ends with `[demo] ready on port ...`.
4. Open the service's address (`https://justsimple-demo-api.onrender.com` or similar). That is the demo website; the keys are at `/api` and the API
   documentation at `/docs`. Businesses signed up on the website, and keys replaced there, are kept in the database (`demo_state`), so they survive a restart.

Good to know:
* **Free web service sleeps** after 15 minutes without visitors; the next request takes 30-60 seconds. Open the page a minute before a demo.
* **The database is on Neon, not Render.** Render's free database is deleted after 30 days; Neon's free plan does not expire. Neon puts the
  database to sleep after a few idle minutes and wakes it on the next connection (under a second).
* **Messages are kept in memory**, so they are gone after a restart or a sleep. Bookings and businesses are in the database and stay.
* If the start fails with `PDPA_ENCRYPTION_KEY must decode to 32 bytes`, set it by hand in the service's Environment tab to the output of
  `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` and deploy again.
* If it fails with `Could not create the postgis extension`, the database plan does not offer PostGIS: tell me and we pick another host.
* The log line `the backend connects as its own role, justsimple_demo_app` is expected on Neon: the demo makes that role so that Row-Level Security
  applies to the backend (Neon's own user skips it). On a database whose user may not create roles it says `connects as the database owner` instead.

### The database (Neon)

1. Sign up at <https://neon.com> (a GitHub sign-in is simplest; the free plan needs no card).
2. Create a project: any name, **Postgres 17**, region **AWS Asia Pacific 1 (Singapore)** (next to Render's Singapore service).
3. On the project's dashboard press **Connect**, turn **Connection pooling off** (the backend keeps its own connections), and copy the
   connection string (`postgresql://neondb_owner:...@ep-....ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require`).
   The demo accepts it exactly as copied.
4. On Render: the service > **Environment** > `DATABASE_URL` > paste it > **Save, rebuild and deploy**. The first start builds the database and
   adds the sample data (a few minutes). The visitors list and log start empty: download the CSV files from `/demo/visitors` before switching.
* **Starting again from fresh sample data:** delete the row in the `demo_state` table and every business (or create a new database), then restart.

## Run it on your own computer (no Render needed)

You need **Python 3.12 or newer**, **Git** and **Docker Desktop** (for the database: PostgreSQL 16 with PostGIS). Redis is not needed: the demo keeps
its cache in memory. From a terminal:

```bash
git clone https://github.com/ChuaShangYeet/Appointment_Wordpress_Plugin.git
cd Appointment_Wordpress_Plugin

# 1. the database (PostgreSQL 16 + PostGIS) on localhost:5432, user/password booking/booking
docker compose -f backend/docker-compose.dev.yml up -d postgres

# 2. the Python packages (once)
python -m venv .venv
source .venv/Scripts/activate     # Windows Git Bash (PowerShell: .venv\Scripts\Activate.ps1); on macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements.txt
```

3. Settings, then start (bash / Git Bash; PowerShell below):
```bash
export ENVIRONMENT=development PORT=8000
export DATABASE_URL=postgresql://booking:booking@localhost:5432/booking
export JWT_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export PDPA_ENCRYPTION_KEY=$(python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")
export PLATFORM_ADMIN_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
python demo/server.py
```
```powershell
$env:ENVIRONMENT="development"; $env:PORT="8000"
$env:DATABASE_URL="postgresql://booking:booking@localhost:5432/booking"
$env:JWT_SECRET = python -c "import secrets;print(secrets.token_urlsafe(48))"
$env:PDPA_ENCRYPTION_KEY = python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
$env:PLATFORM_ADMIN_TOKEN = python -c "import secrets;print(secrets.token_urlsafe(24))"
python demo/server.py
```

4. Open <http://localhost:8000>: the demo website; the keys at <http://localhost:8000/api>, the API docs at <http://localhost:8000/docs>. The first start adds the sample data
   (a minute or two); later starts reuse it. Stop with Ctrl+C; the data stays in Docker. To start again from fresh sample data:
   `docker compose -f backend/docker-compose.dev.yml down -v` and repeat from step 1.

* `ENVIRONMENT=development` is needed here because the Docker database's only user is a superuser (the start-up safety checks would refuse it otherwise). Never use these settings on a server.
* **Keep `PDPA_ENCRYPTION_KEY` the same between starts** (save it in a script): the sample data's encrypted fields (health notes, webhook secrets) cannot be read with another key. The line above makes a new one each time, which is fine only if you also reset the database.
* Port 5432 already taken by another PostgreSQL? Change `"5432:5432"` to `"5433:5432"` in `backend/docker-compose.dev.yml` and use `localhost:5433` in `DATABASE_URL`.
* The backend's own tests: see *Tests* in `backend/README.md` (they need a separate, throw-away database: the test setup wipes it).

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
