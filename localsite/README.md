# JustSimple local test site

The booking product running on your own computer: the real booking system (the same one the businesses will use) with a small
database of sample data, and the test pages to use it. Bookings you make are really made, checked by the real rules, and kept until
you reset. Nothing is sent to anyone: WhatsApp, SMS and email messages appear in the **Messages** panel instead.

## What you need

* Windows 10 or 11.
* Python 3.12 or newer, from https://www.python.org/downloads/ (in the installer, tick **Add python.exe to PATH**).
* An internet connection the first time only (to download the Python packages; the database is included).

## Start and stop

1. Unzip the folder anywhere (not inside OneDrive if you can avoid it).
2. Open the `localsite` folder and double-click **start.bat**. The first start takes a few minutes; later starts take a few seconds.
3. The site opens in your browser at **http://localhost:8780**. If it does not, open that address yourself.
4. To stop, close the black window (or press Ctrl+C in it). Your data is kept for next time.

To throw everything away and start again from the sample data, double-click **reset.bat**.

## What to try

The white bar at the top is the test site's own control panel, not part of the product. Its first row picks the **area** and opens
**Messages**; the second row picks the **business** (five sample businesses, one of each kind) and, in Business admin, who is
**signed in** (owner, manager or one staff member). On a phone the bar folds into a menu button.

| Area | What it shows |
|---|---|
| Book | The booking page a visitor uses on the business's website. The steps differ per business: spa, salon (main service and extras), restaurant (party size, dining room or private room with a minimum spend), car workshop (car, services one after another), home services (address checked against a 25 km service area). |
| My bookings | Sign in with a code sent to the phone (the code is in **Messages**), or a link sent by email. Upcoming bookings with move and cancel, past visits with a star rating, saved details, privacy requests. |
| Business admin | Day list and schedule board, booking details with status changes, requests to approve, bookings to review, moving bookings, extras on a running salon appointment, workshop "waiting for parts" and resume, set-up (services, staff, opening hours, **location from a Google Maps link** (a share link from the app works), **booking rules**) with the free plan's limits, plan meters, website address, new keys. "Signed in as" switches owner / manager / staff. |
| Sign up a business | A new business signs itself up on the free plan, confirms its email (the button opens the emailed link), gets its keys, and sets itself up. |

Sample sign-ins: phone **012-345 6789** and email **aisha.rahman@gmail.com** have bookings at every business.

Things that behave as in the real product, so they may surprise you:

* Times follow the business's clock (Malaysia), whatever your computer's time zone.
* The free plan really limits: 3 staff/tables/bays, 10 services, 50 bookings and 20 WhatsApp messages a month. Each phone sign-in
  code is a WhatsApp message, so after about 20 sign-ins at one free business, phone sign-in stops for that month; use the email link.
* A booking can be marked completed only from an hour before it starts, and a no-show only once it has started.
* A new business has no booking rules until its owner sets them, and cannot take bookings before that (see the gaps below).
* A Google Maps share link (`maps.app.goo.gl`) is really opened at Google to find the place, so that needs an internet connection.
* Too many requests in a minute from one computer are refused for a moment (the rate limit), as they would be online.

Reminders are switched off on the test site (they would use up the free plan's WhatsApp messages within a day).

## For the developer

`run_local.py` starts the bundled PostgreSQL 16 with PostGIS (`pgsql/`, Windows build, port 54330, data in `data/`), brings the schema up to
date with the backend's own migrations, fills an empty database through the real API (`seed.py`), and serves the API, the sweep worker and
the pages (`web/`) on one port. `devtools.py` swaps only what would reach the outside world: an in-memory Redis (fakeredis), an inbox
instead of WhatsApp/SMS/email, a table of Klang Valley places instead of Google geocoding, and `/dev/*` routes the pages use (the business
list, an admin token per role in place of the WordPress plugin, the inbox). The backend's code is unchanged. The API runs as the limited
`booking_app` role, so row-level security applies.

`web/` started as the prototype's pages with `api-live.js` in place of the in-browser mock; since 2026-10-22 it has its own look (one typeface, near-black actions, the booking page beside the business; see `DESIGN.md` in the project folder), which `prototype/` does not have. All the styles are in `web/index.html`; icons, the mark and the arrival-only animations (`J.arrive`) are in `web/core.js`. Where the real API does not give the pages what they
need, `api-live.js` says so in a `BACKEND GAP` comment and fills it locally. Found while building it:

1. **A self-service sign-up creates no booking rules** (`app/services/onboarding.py`), so the new business's availability and booking
   answer 422 "No booking policy configured" until a policy is made. The sign-up checklist promised default rules.
2. Opening hours can be written (`PUT /admin/operating-hours`) but not read back; the test site keeps a copy in `data/local.json`.
3. Nothing says which rule set is the business's default (`GET /admin/policies` has no flag); the test site remembers the one it made.
4. Which staff (or bays) can do which service is not given out, not even to the admin; the booking page offers every staff member.
5. The admin booking answer has `vehicle_id` / `address_id` only, so the admin cannot see the car or the visit's address, and the
   resources without their own times (a workshop's segments, travel padding).
6. A customer's booking (`GET /public/me/bookings`) has no staff, service ids, table type or review; the test site reads them from the
   admin API so the pages can name the staff member and show the rating.

Make the zip to send: `python make_tester_zip.py` (leaves out `data/`, `.venv`, caches and the backend's tests).
