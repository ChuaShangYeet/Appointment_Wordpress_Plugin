# Phase 4 scope: restaurant modules (tables, party size, VIP, open-table caps)

Status: **waiting for confirmation** (Birdview step 3). Map: `.birdview/architecture.json` revision 3. Planned event: `.birdview/activity.jsonl` session `phase-4`, sequence 1.
Branch: `standalone-wp-plugin` (phases 1-3 committed; last commit `4042107`).

**Done means:** an owner can create a **restaurant** module with tables (normal and VIP), opening hours, seating times and an optional cap on how many tables are open for online booking. A customer asks for a party size and a time, and the plugin seats them at the **smallest table that fits**. If the customer allows it, a group too big for one table is seated at the **fewest tables that cover it**. VIP and normal tables are never mixed. A VIP table's **minimum spend** must be accepted, and a customer is never booked at a higher amount than they agreed to. No screens yet, as in phases 2 and 3.

**Ported from:** `backend/app/services/allocators/restaurant.py`, `booking.ensure_minimum_spend_accepted`, the time-slot and table-rule routes in `api/v1/admin.py`, and the 49 cases in `backend/tests/test_restaurant.py`.

## 1. Modules in scope

| Birdview module | Change |
| :--- | :--- |
| `engine` | `RestaurantAllocator` with three pure functions (`solve_seating`, `max_concurrency`, `candidate_starts`), the minimum-spend rule, and party and table fields on the request and slot |
| `booking-service` | Bookings with no services (a party instead), party size, table class, separate seating, minimum spend agreed; views and emails name **tables**, not staff |
| `rest-api` | Tables CRUD, seating-time settings, open-table rules, restaurant parameters on availability and booking |
| `data-layer` | `WpScheduleSource` also reads tables, seating-time settings and open-table rules. **No schema change:** `slot_settings`, `table_open_rules`, `party_size`, `table_type`, `allow_separate_seating` and `minimum_spend_agreed` were all created in phase 1 |
| Untouched | `lifecycle`, `admin-pages`, `front-widget`, and the staff and salon behaviour |

## 2. Rules, as the engine has them

1. **Seating:**
   - Pick the smallest single table with enough seats; equal sizes are decided by table name, then id.
   - Only if the customer allows separate seating, pick the fewest tables (at most 6) whose seats add up to the party. Among those, the fewest spare seats wins, then names.
   - One table that fits is always preferred over combining tables.
2. **VIP and normal** are separate pools and never combined. A VIP table may carry a minimum spend; a normal table may not. Combined VIP tables add up their minimum spends.
3. **Minimum spend:** the booking must carry either `accepted_minimum_spend`, the amount the customer was shown, or the older yes/no `accept_minimum_spend`.
   - Tables are chosen when the request arrives, not when the slot was shown. If another booking took the cheaper table in between, the request is **refused with the new amount (422 `minimum_spend_not_accepted`)**, so the page can ask again. The customer is never booked at a dearer table they didn't agree to.
   - A cheaper table than the amount shown is fine, and records its own amount.
4. **Seating times:** one setting per module.
   - **Flexible:** starts every *interval* from each opening time, while the seating still fits before closing.
   - **Fixed:** starts from *first* to *last* seating, every *interval*; it may run past midnight. Starts that don't fit before closing are dropped.
   - A table is held for *duration + buffer*.
   - The setting may name its own policy instead of the module's default.
5. **Opening hours are required:** a restaurant with no hours is closed. This differs from staff modules, where no hours means shifts decide.
6. **Open-table cap:** a rule caps how many tables of a class can be **in use at the same time** for online bookings, on a weekday or on a specific date. A date rule beats a weekday rule, and 0 closes online booking that day. The cap can't exceed the number of active tables of that class. Each rule has a weekday or a date, never both and never neither, and no duplicates.
7. **Choosing a table** is off by default. When the owner turns it on, a customer may name one table. It must be free, of the class asked for and big enough, and it is never combined with others. When the setting is off, a named table is refused, not silently ignored.
8. **Closures:** a module holiday closes the day. A **day off on a table** takes it out of service; this is the same days-off table the staff modules use, pointed at a table.
9. **Status:** restaurants don't use "in progress", as in the engine. Everything else (pending holds, confirm, cancel, no-show) works as in phase 2.

## 3. Concurrency

- **The open-table cap is the risky part.** Three parties of 2, 3 and 5 can each choose a *different* table, so no per-table check would stop them all going past a cap of 1.
- The engine guards this with a per-day lock. The plugin already takes a **per-module lock** for every booking decision (phase 2), which covers it. The cap and the table choice are always worked out against saved bookings, one request at a time.
- Nothing new is needed here, but it must be **proven** with parallel processes (section 7).

## 4. Files

| File | What |
| :--- | :--- |
| `src/Engine/RestaurantAllocator.php` | `solve_seating()`, `max_concurrency()`, `candidate_starts()` (pure); `find_slots()` / `allocate()` |
| `src/Engine/RestaurantSource.php` (interface) | `tables()`, `slot_settings()`, `open_rules()`. Kept separate so the staff and salon test fixtures stay as they are |
| `src/Engine/MinimumSpend.php` | `ensure_accepted()`, the engine's `ensure_minimum_spend_accepted` |
| `src/Engine/AvailabilityRequest.php`, `SlotOption.php`, `Assignment.php`, `Allocators.php`, `EngineError.php` | Party size, table class, separate seating; slot `table_ids`, `combined` and `minimum_spend`; per-table minimum spend; `restaurant` in the factory; new code `minimum_spend_not_accepted` (422) |
| `src/Data/WpScheduleSource.php`, `Catalog.php` | The restaurant reads; policy resolution through the slot setting's own policy |
| `src/Booking/BookingService.php` | `BOOKABLE_TYPES` gains `restaurant`; party fields; minimum-spend check before writing; views show `party_size`, `table_type`, `tables`, `minimum_spend_agreed` |
| `src/Booking/Mailer.php` | "Table: …" and the party size for restaurant bookings |
| `src/Rest/AdminController.php`, `PublicController.php` | Routes below |

### New and changed routes

| Method | Route | Notes |
| :--- | :--- | :--- |
| GET, POST | `/admin/modules/{id}/tables` | `name`, `capacity` (1-50, required), `status`, `is_vip`, `minimum_spend` (VIP only), `location` |
| PATCH, DELETE | `/admin/tables/{id}` | A table with bookings is deactivated, not deleted (like staff) |
| GET, PUT | `/admin/modules/{id}/seating` | The seating-time setting described in rule 4 |
| GET, PUT | `/admin/modules/{id}/table-rules` | Replaces all open-table rules; validated as in rule 6 |
| GET | `/admin/modules/{id}/bookings?table_type=` | Extra filter on the desk list |
| GET | `/modules/{id}` | Restaurant: biggest party one table seats, biggest party with separate seating, and the tables (name, seats, class, location, minimum spend) **only when choosing a table is on** |
| GET | `/modules/{id}/availability`, `/availability/month` | `party_size` (required), `table_type`, `separate`, `table`. Each slot carries `table_ids`, `combined` and `minimum_spend` |
| POST | `/modules/{id}/bookings` | `party_size`, `table_type`, `allow_separate_seating`, `table_id`, `accepted_minimum_spend` or `accept_minimum_spend`. No `service_ids` |

## 5. Left out of phase 4

- **Deposits and payment for VIP minimum spend:** the amount is agreed and recorded, not charged. Payments stay out of the free plugin.
- **Review flags** when the owner later lowers a cap or changes seating times. The engine's `flagging` sweep marks existing bookings that no longer fit; it's left out, as in phases 2-3.
- **The other phases:** workshop (5), field (6), screens (7), rescheduling, reminders.

## 6. Decisions

None needed. Everything above follows the engine. The one difference is the one already described in section 3: the plugin's per-module lock is broader than the engine's per-day lock. It is harmless at free-site traffic.

## 7. Checks

1. **Engine suite** (`tests/run.php`):
   - The 14 pure cases from `test_restaurant.py`:
     - seating: smallest fit, equal size decided by name, no combining unless allowed, fewest tables, least spare seats, single table preferred, impossible when too few seats, at most 6 tables, same answer whatever the input order
     - a cap counts overlaps but not tables that only touch
     - flexible and fixed seating grids
   - Allocator cases on the in-memory source: VIP and normal pools, combined VIP minimum spend, cap and date-over-weekday rules, 0 open tables, table leave, closed days, chosen-table rules, the business-day retry after midnight, and every minimum-spend case from `test_minimum_spend_rule`.
   - All 61 existing cases must still pass.
2. **Integration script:** a restaurant section ported from the end-to-end cases in `test_restaurant.py`:
   - tables fill smallest first, then larger ones
   - occupancy blocks overlapping starts but not the next free one
   - cancelling frees the tables
   - a pending booking holds its table
   - input validation and missing settings
   - the special request, and the `table_type` filter on the desk list
   - choosing a table: off by default, refused when off, works when on, and turning it off again restores automatic seating

   All 98 existing checks must still pass.
3. **Concurrency with parallel PHP processes**, using `tests/concurrency.php` with a new `book-table` action:
   - **8 parties of 2 at once under a cap of 2:** exactly 2 booked.
   - **Parties of 2, 3 and 5 at once, choosing different tables, under a cap of 1:** exactly 1 booked.
   - **6 parties of 6 at once with one 6-seat table:** exactly 1 booked, 5 told it's taken.
   - **Two VIP parties of 2 at once, both having accepted the 200 room:** one seated at 200; the other refused with the new 300 amount, not booked.
   - **Several requests at once for the same chosen table:** exactly 1 booked.
   - **Across the whole database:** no overlapping live allocations.
4. **Lint and debug log:** `php -l` on every file, and an empty `debug.log`.

**What these won't prove:** the same limits as before, namely behaviour on a real free host, and PHP 7.4 itself.
