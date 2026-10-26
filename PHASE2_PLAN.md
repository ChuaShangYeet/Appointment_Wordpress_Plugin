# Phase 2 scope: core engine, staff allocator, booking service, REST routes

Status: **waiting for confirmation** (Birdview step 3). Map: `.birdview/architecture.json` revision 1. Planned event: `.birdview/activity.jsonl` sequence 1.

**Done means:** a staff-appointment module (spa, clinic, consultant) can be set up through the admin REST routes. A visitor can then see free times and book. Two visitors racing for the same slot get exactly one booking. A pending booking that nobody approves gives its slot back. The customer can view and cancel through their link. Owners and staff can confirm, reject, complete and mark no-show. **No screens yet:** the admin UI and the widget are phase 7. Everything here is tested through REST and the engine test suite.

## 1. Modules in scope

| Birdview module | Change |
| :--- | :--- |
| `engine` | **New**, pure PHP, no WordPress calls |
| `booking-service` | **New** |
| `rest-api` | **New** |
| `data-layer` | Adds the `$wpdb` reader the engine uses. **No schema change** |
| `lifecycle` | Registers REST routes and the `jsb_sweep` handler in `Plugin::boot` |
| Untouched | `admin-pages`, `front-widget`, `reference-engine` |

## 2. Data flow for one booking

```text
Visitor ──POST /modules/7/bookings──▶ PublicController
                                        │ validate + honeypot + per-IP rate limit
                                        ▼
                                   BookingService::create
                                        │ 1. HoldSweeper: expired pending → rejected, allocations → released
                                        │ 2. load module, services, policy (strictest of the services' policies)
                                        │ 3. policy check: min notice, booking window
                                        │ 4. GET_LOCK('jsb:module:7', 10s) ──────────────┐ one booking decision per module at a time
                                        │ 5. START TRANSACTION                           │
                                        │ 6. StaffAllocator::allocate ◀── Availability ◀─┤── WpScheduleSource (reads committed rows)
                                        │      (the slot is re-computed inside the lock, never trusted from the browser)
                                        │ 7. defensive overlap SELECT on the chosen resource
                                        │ 8. upsert customer, insert booking + items + allocations + history
                                        │ 9. COMMIT, RELEASE_LOCK ───────────────────────┘
                                        ▼
                                   201 { code, manage_token, status, start, end, staff }  + plain-text email via wp_mail
```

**One decision changes the plan's wording:** the lock is per **module**, not per module and day. Free-tier sites get little traffic, so one lock per module costs nothing noticeable. It also removes the engine's hardest edge case: an overnight booking at 01:00 belongs to the day before, which a per-day lock would have to take twice. Holding the lock makes the engine's retry loop (`reserve_with_retry`, 5 attempts) unnecessary. The decision inside the lock always sees committed state, so the first answer is final.

## 3. Engine (`src/Engine`), ported from `backend/app/services/availability.py` and `allocators/staff.py`

| File | Ported from | Notes |
| :--- | :--- | :--- |
| `Interval.php`, `IntervalMath.php` | `Interval`, `subtract_intervals`, `intersect_intervals`, `generate_slots`, `fits` | Half-open `[start, end)` as UTC Unix seconds (ints). The maths is in real elapsed time, like the original |
| `Clock.php` | `clock_on`, `window_on`, `shift_windows`, `weekday_index`, `covers_date`, `is_overnight` | `DateTimeImmutable` + `DateTimeZone` for wall-clock to UTC. Skipped hour → moment after the jump; repeated hour → first pass (checked by test) |
| `Policy.php` | `merge_policies`, `validate_booking_window`, `booking_bounds`, `policy_snapshot` | Strictest merge: longest notice, shortest window, pending if any is pending |
| `AvailabilityRequest.php`, `SlotOption.php` | dataclasses | |
| `ScheduleSource.php` (interface) | the SQL inside `compute_availability` | `daysOff`, `operatingHours`, `qualifiedResourceIds`, `shifts`, `busy` |
| `Availability.php` | `compute_availability` | The 5-step pipeline; slot step 15 min (engine default), overridable per module |
| `Allocator.php`, `StaffAllocator.php` | `BaseAllocator`, `StaffAllocator`, `pick_least_loaded` | Named staff, or "any available" → fewest booked minutes that day, ties by lowest id (as `pick_least_loaded` does) |
| `EngineError.php` | `app.core.exceptions` | Codes: `slot_unavailable`, `outside_window`, `invalid_transition`, `too_late_to_cancel`, `validation` |
| `StatusFlow.php` | `TRANSITIONS`, `ensure_status_time`, `cancel_deadline` | Same table. `in-progress` is allowed only for module types that use it (salon, workshop, field) |

## 4. Booking service (`src/Booking`) and data reader (`src/Data`)

- `Data/WpScheduleSource.php`: implements `ScheduleSource` with `$wpdb->prepare`. Busy time ignores held allocations whose hold has expired, as the engine does.
- `Booking/BookingService.php`: `create`, `transition` (row locked with `SELECT … FOR UPDATE`), `cancelAsCustomer`.
- `Booking/HoldSweeper.php`: runs on the `jsb_sweep` cron, **and** at the start of every availability read and booking. Free hosts' WP-Cron is lazy, so the sweep can't depend on it alone.
- `Booking/Customers.php`: upsert by phone (digits and leading `+` only). An email already on a customer record is **never overwritten** by a public booking; the email given is kept on the booking instead. This is the same protection the engine has against someone attaching their email to another person's record.
- `Booking/ManageLink.php`: 12-character public code + 32-byte random token. Only the token's SHA-256 is stored. The token appears once, in the response and the email.
- `Booking/Mailer.php`: plain-text emails through `wp_mail` for created, confirmed, rejected and cancelled. The wording can be changed with filters. Designed HTML emails come later.
- **Idempotency:** the widget sends an `idempotency_key` with each submit. Sending the same key and body again returns the first booking instead of making a second. The same key with a different body gets 409.

## 5. REST routes (`src/Rest`), namespace `justsimple/v1`

**Public** (no login; per-IP rate limit through transients; honeypot field; no nonce, because caching plugins serve stale nonces to logged-out visitors):

| Method | Route | Purpose |
| :--- | :--- | :--- |
| GET | `/modules/{id}` | Title, type, active services, staff (display name only), policy summary |
| GET | `/modules/{id}/availability?date=&services[]=&staff=` | Slots for one day |
| GET | `/modules/{id}/availability/month?month=&services[]=&staff=` | Which dates have any slot, for the calendar |
| POST | `/modules/{id}/bookings` | Create. 201 / 409 `slot_unavailable` with fresh slots / 422 `outside_window` / 429 |
| GET | `/bookings/{code}?token=` | View own booking |
| POST | `/bookings/{code}/cancel` | Cancel with the token, under the snapshotted cancel notice |

**Admin** (cookie + `X-WP-Nonce`; `jsb_manage` for setup, `jsb_work_bookings` for bookings):

| Route | Methods |
| :--- | :--- |
| `/admin/modules`, `/admin/modules/{id}` | GET, POST / GET, PATCH, DELETE (delete refuses while live bookings exist) |
| `/admin/modules/{id}/services`, `/admin/services/{id}` | GET, POST / PATCH, DELETE |
| `/admin/modules/{id}/staff`, `/admin/staff/{id}` | GET, POST / PATCH, DELETE |
| `/admin/staff/{id}/shifts` | GET, PUT (replace the week) |
| `/admin/staff/{id}/services` | PUT (who may do what) |
| `/admin/modules/{id}/policies`, `/admin/policies/{id}` | GET, POST / PATCH |
| `/admin/modules/{id}/hours` | GET, PUT |
| `/admin/modules/{id}/days-off`, `/admin/days-off/{id}` | GET, POST / DELETE |
| `/admin/modules/{id}/bookings?from=&to=&status=` | GET |
| `/admin/bookings/{id}`, `/admin/bookings/{id}/status`, `/admin/bookings/{id}/history` | GET / PATCH / GET |

## 6. Left out of phase 2

- **Not in this phase:**
  - Rescheduling, by customer or admin.
  - The availability cache.
  - Captcha.
  - Customer sign-in ("my bookings" across several bookings).
  - Reminders: the sweep only expires holds for now.
- **Health notes (spa and clinic):** the column exists, but the API won't accept health notes until encryption lands. Storing them unencrypted would break the PDPA rule the engine follows.
- **Other module types:** salon add-ons, restaurant, workshop and field types are refused with `validation` until their phases.

## 7. Checks

1. **Engine suite:** a plain PHP test runner (`tests/run.php`, no PHPUnit, so nothing to download), with scenarios ported from:
   - `test_availability.py`, `test_availability_bounds.py`, `test_allocator_staff.py`
   - `test_overnight.py`, `test_dst.py`, `test_dst_nights.py`
   - `test_customer_cancel.py`, and the policy-merge cases.
2. **Integration script** against `http://localhost/jsb-test`:
   - Sets up a module via `rest_do_request` as admin.
   - Books a slot, books it again (409), confirms, cancels via the manage link, and checks the history rows.
   - Lets a pending hold expire and checks the slot comes back.
3. **Race test:** 10 parallel `curl` POSTs for one slot and one named staff member. Exactly one gets 201 and nine get 409. A second run with "any available" and 3 staff gives exactly 3 × 201.
4. **Checks:** `php -l` on every file, and an empty `debug.log` after all runs.

**What these won't prove:**
- How it behaves on a real free host, where GET_LOCK timeouts and WP-Cron timing differ.
- PHP 7.4 compatibility (local PHP is 8.2), beyond avoiding PHP 8-only syntax.
