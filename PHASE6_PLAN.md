# Phase 6 scope: field-service modules (in office, online, on site; inspections and follow-ups)

Status: **waiting for confirmation** (Birdview step 3). Map: `.birdview/architecture.json` revision 5. Planned event: `.birdview/activity.jsonl` session `phase-6`, sequence 1.
Branch: `standalone-wp-plugin` (phases 1-5 committed; last commit `a92d3aa`).

**Done means:** a consultant or home-service business can offer services **in its office**, **online** or **at the customer's address**.
- **On-site visits:** only inside the business's **service radius**, with **travel time** blocked before and after on the professional's schedule.
- **Online appointments:** they get the professional's **meeting link** when confirmed.
- **Inspections:** an inspection visit can lead to a **follow-up job** linked to it.
- **Overruns:** a job that runs long either extends the professional's reservation or flags a clash; it never pushes into the next customer's time.
- **This is the last booking type.** Phase 7 is the screens.

**Ported from:** `backend/app/services/allocators/field_service.py`, `field_service.py` (follow-ups, overruns, meeting link by hand), `meetings.py`, `geo.py`, `locations.py`, and the cases in `backend/tests/test_field_service.py` (31), `test_meeting_links.py` (14) and `test_price_after_inspection.py` (6).

## 1. Modules in scope

| Birdview module | Change |
| :--- | :--- |
| `engine` | `FieldAllocator` (one professional, travel padding, radius check, named professional); `Geo::distance_km()` (pure); `Assignment` gains the travel padding |
| `booking-service` | The address on an on-site booking; meeting links; follow-ups; actual duration and overruns. The phase 3 stretch-or-flag code becomes shared (`Extend`); parts lists extended to field jobs |
| `rest-api` | Service location and category fields, the business location, the professional's meeting room, and the meeting-link, follow-up and actual-duration routes |
| `data-layer` | **No schema change:** `services.location_type`, `travel_buffer_minutes` and `service_category`, `resources.meeting_link`, `customer_addresses`, `bookings.address_id`, `meeting_link`, `parent_booking_id` and `actual_duration_minutes` were all created in phase 1. The business location lives in the module's settings JSON |
| Untouched | `lifecycle`, `admin-pages`, `front-widget`, and the staff, salon, restaurant and workshop behaviour (except that salon upsell now uses the shared `Extend`) |

## 2. Rules, as the engine has them

1. **One service per booking**, which happens in office, online or on site.
2. **The service radius:**
   - An on-site request must carry the customer's coordinates. It is refused with 422 `outside_service_area` (giving the distance and the radius) when it's farther than the module's radius from the business, and **nothing is written**.
   - The check applies to **availability and booking** alike.
   - A module with no location or radius set refuses on-site requests with a clear message. In-office and online services don't need the location at all.
3. **Travel time:**
   - An on-site visit reserves `travel_buffer_minutes` **before and after** the visit on the professional's schedule. The whole padded block must fit the shift, breaks and opening hours, and be free.
   - The **booking** shows the visit itself (10:00-11:00); the **allocation** holds the padded time (09:30-11:30).
   - In-office and online services carry no travel time.
4. **Who:** the least-loaded qualified professional that day, or a named one. A named id that isn't a professional gets 404. No opening hours means open all day (shifts decide), as for workshops.
5. **The address:** an on-site booking needs exactly one address (line, city, postcode, coordinates). Any other service refuses one, with 422.
6. **Meeting links (online services):**
   - When an online appointment becomes **confirmed** with no link yet, it gets its professional's saved meeting room. This happens on booking if it's confirmed at once, or when a pending one is approved.
   - A link set by hand on the booking always wins. If the booking is already confirmed, it's emailed to the customer once.
   - A room must be an `http(s)` web link. It shows in admin and to the customer of that booking, never on the public module page. In-person visits get no link.
7. **Inspections and follow-ups:**
   - A follow-up can be booked only from an **inspection** (a service with category `inspection`) that is in progress or completed. The follow-up must be a **standard** service (not another inspection), starting in the future.
   - It reuses the inspection's customer and address. An on-site follow-up is **checked against the radius again**, and it needs the inspection to have had an address.
   - It is confirmed at once, made by staff, ignores the notice rules, and is linked through `parent_booking_id`.
8. **Overruns (`actual_duration_minutes`):**
   - Only for a job **in progress**, and at least 1 minute.
   - Shorter or on time: only the figure is stored.
   - Longer, with the professional free afterwards: the reservation and end time stretch.
   - Longer, running into the next appointment: nothing moves, both are flagged `schedule_conflict`, and the clashing booking ids are returned. The flag is cleared as in phase 3.
9. **Price after inspection:** this already exists on services since phase 2 (a 0 price, or a starting price marked "after inspection"). This phase adds the engine's cases and shows it in the emails.
10. **Parts lists:** the engine lets field jobs keep a parts list like workshops ("professionals keep spare parts too"), so phase 5's parts list and stock-on-completion are extended to `field`.
11. **Service settings:** location type, category and travel time are accepted only on field modules. Other modules keep the defaults (in office, no category, no travel).

## 3. Concurrency

- New bookings, follow-ups and overruns all take the **per-module lock**, as booking and upsell do. That covers both padded visits whose start times look apart but whose travel time overlaps, and an overrun racing a new booking for the time right after.
- The stretch-or-flag logic from salon upsell becomes one shared `Extend::try_extend()`, used by both upsell and overrun.

## 4. Files

| File | What |
| :--- | :--- |
| `src/Engine/FieldAllocator.php` | `travel_pad()`, `find_slots()` / `allocate()`; radius check before planning |
| `src/Engine/Geo.php` | `distance_km()` on the WGS-84 ellipsoid (Vincenty), with a sphere fallback for the rare points where Vincenty doesn't converge. Pure PHP, so no PostGIS is needed |
| `src/Engine/Assignment.php`, `AvailabilityRequest.php`, `Allocators.php`, `EngineError.php` | Travel padding; the customer's coordinates and the business location on the request; `field` in the factory; new code `outside_service_area` (422) |
| `src/Booking/Addresses.php` | The on-site address: checked, then saved for the customer |
| `src/Booking/Extend.php` | The shared stretch-or-flag (moved out of `Upsell`) |
| `src/Booking/FieldService.php` | `follow_up()`, `record_actual_duration()`, `set_meeting_link()`, and filling the meeting room on confirmation |
| `src/Booking/BookingService.php`, `Parts.php`, `Mailer.php` | `BOOKABLE_TYPES` gains `field`; the address step; booking times exclude travel; the meeting link on confirmation; views show location, address, meeting link (to the booking's customer), parent and actual duration; parts and stock for field jobs; the emails carry the address or the link |
| `src/Rest/AdminController.php`, `PublicController.php` | Routes below |

### New and changed routes

| Method | Route | Notes |
| :--- | :--- | :--- |
| PATCH | `/admin/modules/{id}` | `settings.latitude`, `settings.longitude`, `settings.service_radius_km` (-90..90, -180..180, more than 0 up to 500 km) |
| POST, PATCH | `/admin/modules/{id}/services`, `/admin/services/{id}` | `location_type`, `service_category`, `travel_buffer_minutes` (field modules only) |
| POST, PATCH | `/admin/modules/{id}/staff`, `/admin/staff/{id}` | `meeting_link` (http/https) |
| PATCH | `/admin/bookings/{id}/meeting-link` | `{ url }`: a link set by hand, emailed when the booking is confirmed |
| POST | `/admin/bookings/{id}/follow-up` | `{ service_id, start, staff_id? }` |
| PATCH | `/admin/bookings/{id}/actual-duration` | `{ minutes }`, returning `{ booking, conflicting_booking_ids }` |
| GET | `/modules/{id}/availability` | On-site services need `latitude` and `longitude` |
| POST | `/modules/{id}/bookings` | `address { line1, city, postcode, latitude, longitude }` |

## 5. One decision for you: typed addresses without a map pin

The engine turns a typed address into coordinates through **Google's Geocoding API** when the customer drops no map pin. The plugin promises **no outside service** for free hosting: an API key, billing, and requests leaving the owner's site.

**Recommendation:** in this phase an on-site booking needs **coordinates**. The phase 7 widget gets them from a map pin, or from the browser's own location button, which is free and needs no key. A typed address without coordinates gets a clear 422. Optional geocoding, with the owner's own key, can come later as a setting.

The other option is to call OpenStreetMap's free Nominatim service from the server. It needs no key, but its usage policy allows about one request a second and asks for low volume, so a busy site could be blocked.

As for saved addresses: like vehicles in phase 5, there's no customer sign-in, so the public can't pick an address by id. Each on-site booking carries its address. A follow-up reuses the inspection's address on the server side, so it isn't affected.

## 6. Left out of phase 6

- A video service that creates rooms itself (the engine ships none either: "the business brings its own meeting service").
- Route planning between consecutive visits. The engine also models travel as "back to base each time".
- Calendar sync, review flags, reminders, customer sign-in and saved addresses, rescheduling by the customer.
- The screens (phase 7).

## 7. Checks

1. **Engine suite** (`tests/run.php`):
   - Pure cases:
     - travel time only for on-site visits
     - `Geo::distance_km`: Kuala Lumpur to Subang about 20 km, matching the engine test's 19.5-20.6 km sanity range; zero distance; across the antimeridian; the radius boundary a hair inside and outside
   - Allocator cases on the in-memory source:
     - the radius check and its error data; no location set
     - travel blocks before and after, and shapes what can be booked next
     - online and in-office have no travel
     - one service only
     - least-loaded and qualified professionals; a named professional
     - no opening hours means open all day
   - All 107 existing cases must still pass.
2. **Integration script:** a field section ported from the three test files:
   - on-site availability needs coordinates and offers slots inside the radius; outside is refused everywhere with nothing written
   - the business location is validated; on-site needs an address and other services refuse one
   - the booking shows the visit while the allocation holds the travel
   - meeting rooms on confirmed online appointments, on approval of pending ones, set by hand and emailed once, none for in-person visits, only staff web links, not on the public page
   - follow-ups linked to the inspection with travel, and the follow-up rules (radius re-checked, needs an address, standard only, future only, inspection only)
   - overruns: a free one stretches, a shorter one only records, one into the next appointment is flagged and not forced, the overrun rules
   - price after inspection; field-only service settings
   - parts on field jobs

   All 199 existing checks must still pass.
3. **Concurrency with parallel PHP processes** (new `book-field` and `overrun` actions in `tests/concurrency.php`):
   - **6 on-site requests at once for one professional at one time:** exactly 1 booked.
   - **10:00 and 11:00 at once, whose travel blocks overlap:** exactly 1 booked.
   - **Requests at once spread across 3 professionals:** never more bookings than professionals.
   - **An overrun racing a new booking for the time right after:** exactly one wins, in both orders.
   - **Across the whole database:** no overlapping live allocations.
4. **Lint and debug log:** `php -l` on every file, and an empty `debug.log`.

**What these won't prove:** the same limits as before, namely behaviour on a real free host, and PHP 7.4 itself. The distance is checked against the engine test's range and known values, not against PostGIS itself, which isn't available here.
