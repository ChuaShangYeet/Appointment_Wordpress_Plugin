# JustSimple booking prototype (UX testing)

A clickable prototype of the booking product, running entirely in the browser on **sample data**. Nothing is sent anywhere; **Reset data**
in the top strip starts over. Built 2026-10-15 against the backend's API as it stands (migrations 0001-0031).

Run it locally: `python devserver.py`, then open http://localhost:8765 (it serves `index.html` the way the published page is served).

## What testers can try

The dark strip at the top is the prototype's own control panel, not part of the product. It picks the **area**, the **business** (five
sample businesses, one of each kind) and a **scenario**.

| Area | What it shows |
|---|---|
| Book | The booking widget a visitor uses. Steps differ per business: spa (service, therapist, time), salon (main service, extras, stylist), restaurant (party size, dining room or private room with a minimum spend, separate seating), car workshop (car, services one after another, parts lead time), home services (address checked against a 25 km service area, travel time). |
| My bookings | Sign in with a code sent to the phone, or a link sent by email. Upcoming bookings with move and cancel (blocked inside the cancellation window), past visits with a star rating, saved details, privacy requests. An email link for an address nobody has confirmed opens only the bookings made with it. |
| Business admin | Day list and schedule board, booking drawer with status changes (timed: "completed" opens an hour before the start, "no-show" at the start), requests to approve, bookings flagged for review, moving bookings, salon extras that clash with the next client, workshop "waiting for parts" and resume, set-up (services, staff, hours) with the free plan's limits, plan meters, website address (changes once every 30 days on the free plan), new keys shown once. "Signed in as" switches owner / manager / staff; staff see only their own appointments and no health notes. |
| Sign up a business | Self-service sign-up on the free plan: kind of business, country (Malaysia, Singapore, Australia), address checks, website address, owner email (temporary-mail addresses refused), terms, human check, email confirmation, keys shown once, then the set-up checklist. |

Sample sign-ins: phone **012-345 6789** and email **aisha.rahman@gmail.com** have bookings at every business. Any 6-digit code works except
000000 (a wrong code).

Scenarios: everything works; someone takes the slot first (the next booking attempt gets "pick another time"); every booking needs
approval; messages fail to send; the monthly booking limit is reached.

## Files

`index.html` (styles and shell), `data.js` (the sample businesses), `api.js` (a mock of the backend: same field names, same error codes,
same rules for free times, cancelling, moving, plan limits), `core.js` (state, rendering, the top strip), `booking.js`, `account.js`,
`admin.js`, `signup.js` (one per area).

## Not real

All data is invented and resets on reload. Times are worked out the way the backend does (opening hours, shifts, breaks, existing bookings,
notice and booking window), but only in the browser. Messages, emails, the human check, address look-ups, Google Calendar and payments are
pretended. The restaurant takes requests (pending) because its sample policy says so; the other businesses confirm at once.
