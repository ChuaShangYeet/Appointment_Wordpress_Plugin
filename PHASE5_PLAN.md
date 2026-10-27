# Phase 5 scope: workshop modules (mechanic + bay, stacked services, vehicles, parts)

Status: **waiting for confirmation** (Birdview step 3). Map: `.birdview/architecture.json` revision 4. Planned event: `.birdview/activity.jsonl` session `phase-5`, sequence 1.
Branch: `standalone-wp-plugin` (phases 1-4 committed; last commit `7a67da3`).

**Done means:** a car workshop can set up mechanics (staff with shifts), service bays, the vehicles it services and the parts each service needs. A customer books one or more services for their car:
- Every service needs a **qualified mechanic and a free bay**, and the services run **back to back**.
- An unsupported car is refused.
- If a part has to be ordered, the customer must **acknowledge the lead time** first.

The workshop keeps each job's parts list. A job can be paused as **waiting for parts**, which frees the mechanic and bay from that moment, and then **resumed** at a new time. Completing the job takes the parts off the shelf. Still no screens, as in phases 2-4.

**Ported from:** `backend/app/services/allocators/workshop.py`, `automotive.py`, `parts.py`, `booking.truncate_allocations` and `resume_booking`, the related routes in `api/v1/admin.py`, and the cases in `backend/tests/test_automotive.py` (39) and `test_job_parts.py` (21).

## 1. Modules in scope

| Birdview module | Change |
| :--- | :--- |
| `engine` | `WorkshopAllocator`: segments, mechanic **and** bay per segment, continuity, a named mechanic. Pure `plan_segments()` and `choose_resource()` |
| `booking-service` | Vehicles; the parts lead-time acknowledgement; the job parts list; waiting for parts (cut allocations at now); resume; stock taken on completion |
| `rest-api` | Bays, supported vehicles, parts per service, parts per job, resume, the lead-time notice in availability |
| `data-layer` | `WpScheduleSource` gives resource names (for tie-breaks). **No schema change:** `supported_vehicles`, `vehicles`, `service_products`, `bookings.vehicle_id` and `lead_time_acknowledged_utc` were created in phase 1 |
| Untouched | `lifecycle`, `admin-pages`, `front-widget`, and the staff, salon and restaurant behaviour |

## 2. Rules, as the engine has them

1. **Two resources per segment.** Each service on a job is a segment, *duration + buffer* long; segment *n* starts when segment *n-1* ends. Every segment needs a qualified mechanic **and** a qualified bay, both free for the whole segment. Each segment writes **two allocation rows** (mechanic, bay), each tagged with its service.
2. **Who and where:**
   - Mechanics follow their shifts, breaks, days off and the opening hours. Bays follow the opening hours and days off only; they have no shifts or breaks.
   - Qualification is checked **per service and per resource type**. A service linked only to a bay doesn't disqualify any mechanic; this already works since phase 2.
   - A workshop with **no opening hours is open all day**. This matches the engine and differs from restaurants.
3. **Choosing:** keep the previous segment's mechanic and bay when they're still free (continuity). Otherwise take the least-loaded mechanic, or the lowest-named bay, then decide by id.
4. **A named mechanic** must be qualified for, and free during, **every** segment; the bay is still chosen automatically. A named id that isn't a mechanic gets 404.
5. **Vehicles:**
   - A booking needs a vehicle: plate, supported model and year. The model must be one the workshop supports; a year-limited model needs a year inside its range. Otherwise it gets 422 `incompatible_vehicle` and **nothing is written**.
   - Plates are normalised (`abc 123` = `ABC-123`), so the same customer and plate is the same vehicle record.
   - A vehicle on a non-workshop booking gets 422.
6. **Parts lead time:**
   - When a chosen service needs a part that is only **available on order**, the day's availability lists it (`lead_time_notice`). Booking without `acknowledge_lead_time` gets 422 `lead_time_not_acknowledged`, listing the parts, and nothing is written.
   - The booking records when the customer acknowledged it. Parts in stock or out of stock trigger nothing.
   - A product marked available on order must have a lead time.
7. **The job's parts list:**
   - `PUT` replaces the whole list. It's only possible while the job is confirmed, in progress or waiting for parts.
   - A part is priced at today's price when it's first added, and keeps that price when its quantity changes later.
   - Each part appears once, at most 50 lines. A bad list changes nothing.
   - A line wanting more than the shelf holds is marked `needs_ordering`, which is the cue for "waiting for parts". It is still allowed on the list.
8. **Waiting for parts:**
   - Only from **in progress**, and only for workshops.
   - From that moment the mechanic and bay are free: rows not started yet are released, a running segment is **cut at now**, and finished work stays on record.
   - A job waiting for parts can't be confirmed, only resumed. It can't be completed either; it can be cancelled.
9. **Resume:**
   - Only a job waiting for parts can be resumed, at a start in the future. It schedules all of the job's services, or only the remaining ones named.
   - It skips the notice and booking-window rules (an admin action), but not the mechanic, bay or clash rules. A clash gets 409 with the free slots.
   - Afterwards the status is confirmed and the end moves; the original start is kept.
10. **Completing** a workshop job takes its parts off the shelf, never below 0, using phase 3's stock code.
11. **Own appointments only** (phase 3) also covers the parts list and resume for a mechanic linked to a login.

## 3. Concurrency

Every decision that writes time takes the **per-module lock**: a new job, a resume, and pausing for parts, which frees time. So jobs racing for the scarcer of mechanics and bays can't overfill it. Two people editing one job's parts list, or a completion racing a parts edit, are ordered by the **booking-row lock** (`SELECT … FOR UPDATE`), as the engine does. So a part is never lost, and an edit that arrives after the job is completed is refused.

## 4. Files

| File | What |
| :--- | :--- |
| `src/Engine/WorkshopAllocator.php` | `plan_segments()`, `choose_resource()`, `find_slots()` / `allocate()` |
| `src/Engine/WorkshopSource.php` (interface) | `resource_names()`. Separate, so the other modules' test fixtures don't change |
| `src/Engine/SlotOption.php`, `Allocators.php`, `EngineError.php` | A job slot lists `staff_ids` and `bay_ids`; `workshop` in the factory; new codes `incompatible_vehicle` and `lead_time_not_acknowledged` (422) |
| `src/Booking/Vehicles.php` | Plate normalisation, the supported-model and year check, the per-customer vehicle record |
| `src/Booking/Parts.php` | Lead-time notice, the job parts list, `needs_ordering` |
| `src/Booking/BookingService.php` | `BOOKABLE_TYPES` gains `workshop`; vehicle and lead-time steps in `create`; waiting for parts in `transition`; `resume()`; views show the vehicle, the parts and, privately, the segments |
| `src/Booking/Inventory.php`, `Mailer.php` | Stock taken on workshop completion; the vehicle in the emails |
| `src/Rest/AdminController.php`, `PublicController.php`, `Fields.php` | Routes below |

### New and changed routes

| Method | Route | Notes |
| :--- | :--- | :--- |
| GET, POST | `/admin/modules/{id}/bays` | `name`, `status`, `sort_order` (workshop modules only) |
| PATCH, DELETE | `/admin/bays/{id}` | A bay with bookings is deactivated, not deleted |
| PUT | `/admin/bays/{id}/services` | Which services a bay supports (same rule as staff) |
| GET, POST | `/admin/modules/{id}/supported-vehicles` | `make`, `model`, `year_from`, `year_to` |
| PATCH, DELETE | `/admin/supported-vehicles/{id}` | |
| GET, PUT | `/admin/services/{id}/parts` | The products a service needs (drives the lead-time notice) |
| GET, PUT | `/admin/bookings/{id}/parts` | The job's parts list (rule 7), own appointments only |
| POST | `/admin/bookings/{id}/resume` | `{ start, staff_id?, service_ids? }` |
| GET | `/modules/{id}/supported-vehicles` | Public: make, model, years |
| GET | `/modules/{id}/availability` | Workshop slots carry `staff_ids` and `bay_ids`; the response adds `lead_time_notice` |
| POST | `/modules/{id}/bookings` | `vehicle { plate_no, supported_vehicle_id, year }`, `acknowledge_lead_time` |

## 5. One decision for you: picking a saved vehicle

The engine lets a **signed-in** customer pick a saved vehicle by its id. The plugin has no customer sign-in yet; it's planned with the screens. Accepting a vehicle id from an anonymous request would let anyone who knew the id book work on someone else's car.

**Recommendation:** in this phase the public booking takes the vehicle **as typed** (plate, model, year) every time. The same phone number and plate still land on **one** vehicle record, so nothing is duplicated. Picking a saved vehicle comes with customer sign-in later. The other option is to accept a vehicle id when the phone number matches its owner, as the engine does. That is weaker, because phone numbers aren't secret.

## 6. Left out of phase 5

- **Review flags** when the owner later changes shifts or hours (as in phases 2-4).
- **The engine's audit log** of parts edits; the booking history records status changes only.
- **Customer sign-in, saved vehicles, rescheduling by the customer, reminders, the screens.**
- **Field service** is phase 6.

## 7. Checks

1. **Engine suite** (`tests/run.php`):
   - The pure cases from `test_automotive.py`: segments contiguous with buffers; `fits` needs the whole segment; continuity, then load, then name.
   - Allocator cases on the in-memory source:
     - a slot needs both a mechanic and a bay; whichever is scarcer is the limit
     - qualification separately for mechanics and bays; a bay-only link doesn't disqualify mechanics
     - mechanics' breaks apply but bays have none; buffers are part of each segment
     - the same mechanic and bay are kept across segments when possible
     - segments block resources one at a time; a segment running into a break removes the start
     - a named mechanic must be qualified and free throughout
     - no opening hours means open all day
   - All 90 existing cases must still pass.
2. **Integration script:** a workshop section ported from `test_automotive.py` and `test_job_parts.py`:
   - vehicles: required, unsupported refused with nothing written, year ranges, plate normalisation, refused on other modules, public list of supported models
   - two allocation rows per segment; stacked segments
   - waiting for parts releases future time and cuts a running segment at now; the waiting-for-parts rules
   - resume: all services or only some, its validation, a clash gets 409, beyond the window allowed
   - the lead-time notice and acknowledgement, and that only on-order parts trigger it
   - the parts list: today's price, replace semantics, a bad list changes nothing, needs ordering, which statuses allow edits, own jobs only, 50 lines
   - completion takes parts off the shelf; a job never completed takes nothing; never negative, and the on-order label stays
   - the full parts-waiting flow from booking to completion

   All 141 existing checks must still pass.
3. **Concurrency with parallel PHP processes** (new `book-job`, `set-parts`, `complete` actions in `tests/concurrency.php`):
   - **Jobs at once never exceed the scarcer resource**, run three ways: 3 mechanics with 1 bay, 1 mechanic with 3 bays, 2 and 2. The winners must be exactly 1, 1 and 2.
   - **Stacked two-service jobs at once** fill the free time but don't overfill it.
   - **Two parts edits at once** on one job apply one after the other; the final list is exactly one of them, never a mix.
   - **A completion racing a parts edit:** stock is taken for exactly the list that was on the job at completion, and a late edit is refused.
   - **Across the whole database:** no overlapping live allocations.
4. **Lint and debug log:** `php -l` on every file, and an empty `debug.log`.

**What these won't prove:** the same limits as before, namely behaviour on a real free host, and PHP 7.4 itself.
