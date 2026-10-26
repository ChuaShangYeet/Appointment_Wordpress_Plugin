# Phase 3 scope: salon and barber modules (add-ons, on-the-spot upsell, retail stock)

Status: **waiting for confirmation** (Birdview step 3). Map: `.birdview/architecture.json` revision 2. Planned event: `.birdview/activity.jsonl` sequence 1 of session `phase-3`.
Branch: `standalone-wp-plugin` (phases 1-2 committed as `c7bf558`).

**Done means:** an owner can create a **salon** module with base services, **add-ons** and **retail products**. A customer books one base service plus any add-ons, and the stylist is reserved for all of them back to back. During the appointment the stylist can add add-ons or products to the ticket. An add-on that would run into the stylist's next appointment is still put on the ticket, but **nothing is forced into the other customer's time**: both appointments are flagged and the stylist sees whose appointment it is. Completing the appointment takes the products off the shelf and records the real duration. Still no screens; the tests use REST, as in phase 2.

**Ported from:** `backend/app/services/allocators/salon.py`, `upsell.py`, `inventory.py`, `booking.try_extend`, and the cases in `backend/tests/test_salon.py`.

## 1. Modules in scope

| Birdview module | Change |
| :--- | :--- |
| `engine` | `SalonAllocator`, the cart rule, qualification by the base service, `Allocators::for_type` |
| `booking-service` | `Upsell` (extend or flag), `Inventory` (stock on completion), salon-aware create and transition |
| `rest-api` | Products routes, ticket-items route, `is_addon` on services, salon fields in booking views |
| `data-layer` | `Catalog` lets salon carts include add-ons. **No schema change:** `products`, `booking_products`, `is_addon`, `is_upsell`, `schedule_conflict` and `actual_duration_minutes` were created in phase 1 |
| Untouched | `lifecycle`, `admin-pages`, `front-widget` |

## 2. Rules, as the engine has them

1. **The cart:** exactly one base service plus zero or more add-ons. An add-on on its own, or two base services, gets 422. The block is the sum of every duration and buffer: cut 45 + gel 30 + treatment 20 (+5 buffer) = 10:00-11:40.
2. **Who can do it:** follows the **base service only**. If an add-on is linked to another stylist, that never narrows the choice. "Any available" goes to the least-loaded qualified stylist, as for staff modules.
3. **Policy:** the strictest of the cart's policies (already built in phase 2).
4. **Upsell:** only while the appointment is **in-progress**, and only add-ons (a base service gets 422) and products. Asking for nothing gets 422.
5. **Extending:** the stylist's reservation stretches by the add-ons' minutes **only if** nothing else of theirs is live in that extra time. Ending exactly when the next appointment starts is fine. Otherwise:
   - The reservation is left as it is.
   - The add-on is still on the ticket.
   - Both bookings get `schedule_conflict`.
   - The response lists the clashing appointment (customer name, services, start and end).
6. **Products:**
   - Each product goes on the ticket at **today's price**; that price is frozen on the ticket.
   - Adding the same product again adds to its quantity.
   - The request is refused with 409 `insufficient_stock` if the product is out of stock, or if what's already on the ticket plus the new amount would exceed the shelf count. A refused request changes nothing.
7. **Completing:** stock goes down by each ticket line, never below 0. A product that hits 0 changes from `in_stock` to `out_of_stock`; an `available_on_order` product keeps that status. `actual_duration_minutes` = base + every add-on, upsells included.
8. **Public catalog:** product name, price and stock **status** only, never the count or SKU.

## 3. Concurrency (the part that needs care in MySQL)

- **An upsell competes with new bookings for the same stylist's time**, so it takes the **same `jsb:module:<id>` lock** as booking, then locks the booking row (`SELECT … FOR UPDATE`), checks for clashes and writes, all in one transaction. Three things follow:
  - A new booking can't slip into the gap between the clash check and the extension. The engine handles that case with an exclusion-constraint error and a re-check; here the lock prevents it.
  - Upsells to the same appointment that arrive together apply one after the other: each add-on is added exactly once and the end time moves by the total.
- **Stock on completion** is one atomic `UPDATE` per ticket line. **One MySQL trap:** MySQL applies `SET` assignments left to right, so a `stock_status` assignment placed after `stock_qty` would see the *new* quantity. The status is therefore set first, from the old quantity, then the quantity is clamped with `GREATEST(stock_qty - q, 0)`. Two completions that both claimed the last unit leave 0, not -1.

## 4. Files

| File | What |
| :--- | :--- |
| `src/Engine/SalonAllocator.php` | `split_cart()`; sets `qualifying_services` to the base, then runs the staff pipeline |
| `src/Engine/Allocators.php` | `for_type('staff' \| 'salon')`, with later types to follow. Replaces the hard-coded `StaffAllocator` in `BookingService` |
| `src/Engine/AvailabilityRequest.php`, `Availability.php` | `qualifying_services` (null = all services), used for the qualification query |
| `src/Engine/EngineError.php` | New code `insufficient_stock` (409) |
| `src/Data/Catalog.php` | `services_for_booking` takes add-ons for salon modules, keeps refusing them for staff modules; `products_for_module` |
| `src/Booking/Upsell.php` | `add_to_ticket()`: lock, row lock, validate everything, write lines, extend or flag, return clash details |
| `src/Booking/Inventory.php` | `deduct_stock()`, `finalise_completion()` |
| `src/Booking/BookingService.php` | `BOOKABLE_TYPES` gains `salon`; uses `Allocators::for_type`; calls `Inventory` when a salon booking completes; the private view adds items (`is_upsell`), products, `schedule_conflict`, `actual_duration_minutes` |
| `src/Rest/AdminController.php` | Products CRUD; `is_addon` on services (refused on staff modules); `POST /admin/bookings/{id}/items`; `PATCH /admin/bookings/{id}` to clear `schedule_conflict`; `?schedule_conflict=1` filter on the desk list |
| `src/Rest/PublicController.php` | `is_addon` in module info; `GET /modules/{id}/products` |

### New routes

| Method | Route | Who |
| :--- | :--- | :--- |
| GET, POST | `/admin/modules/{id}/products` | `jsb_manage` |
| PATCH, DELETE | `/admin/products/{id}` (delete refused while the product is on a ticket) | `jsb_manage` |
| POST | `/admin/bookings/{id}/items` `{ service_ids?, products?: [{product_id, quantity}] }` → `{ booking, extended, added_minutes, conflicts[] }` | `jsb_work_bookings`, own appointments (see decision below) |
| PATCH | `/admin/bookings/{id}` `{ schedule_conflict: false }` | `jsb_work_bookings` |
| GET | `/modules/{id}/products` | public, read budget |

## 5. One decision for you: "own appointments only"

In the engine, a **staff** login can add items only to **their own** appointments; owners and managers can do it for anyone. The plugin's equivalent is a WordPress user linked to a staff member (`wp_user_id`) who has `jsb_work_bookings` but not `jsb_manage`.

**Recommendation:** apply that rule to **ticket items and status changes** for linked users. Unlinked desk users, such as an editor at the front desk, keep seeing and working everything, as in phase 2. Applying it to status changes is a small change to phase 2 behaviour, which is why I'm raising it. If you'd rather keep phase 2 untouched, it applies to ticket items only.

## 6. Left out of phase 3

- Restaurant, workshop and field modules (phases 4-6).
- Rescheduling, reminders and the screens, as in phase 2.
- Health notes: still refused, now for salon modules too.
- Parts lead-time disclaimers: those belong to workshop modules (phase 5), even though `available_on_order` is already a product status.

## 7. Checks

1. **Engine suite** (`tests/run.php`) gains cases from `test_salon.py`:
   - one base service per cart
   - an add-on lengthens the block: the last start is 16:45 for 45 + 30 minutes before 18:00
   - qualification follows the base service only
   - "any available" picks the least-loaded stylist with add-ons in the cart
   - a strictest-policy cart

   All 56 existing cases must still pass.
2. **Integration script** gains a salon section, ported from `test_salon.py`:
   - add-ons extend the booking and are recorded
   - one base service per cart
   - qualification by the base service
   - an upsell that reaches the next appointment is flagged and forces nothing
   - an upsell ending exactly at the next start is fine
   - upsell rules (not in progress yet → 409, base service → 422, nothing to add → 422, a staff login only on their own appointments → 403, a staff module → 409)
   - products at today's price with stock checks
   - completing deducts stock, and 0 shows out of stock
   - stock never goes negative, and on-order is kept
   - the public catalog shows status, not counts
   - the add-on flag on staff modules is refused

   The phase 2 checks must still pass.
3. **Concurrency, with real parallel processes** (each PHP CLI process has its own database connection):
   - **3 upsells at once to one appointment** (+30, +20, +15 min): every add-on appears exactly once, and the allocation ends at start + 45 + 65 minutes.
   - **An upsell and a new booking racing for the stylist's next 30 minutes:** exactly one wins. Either the booking lands and the upsell is flagged, or the upsell extends and the booking gets 409. Never both.
   - **Two completions claiming the last unit:** stock ends at 0.
4. **Lint and debug log:** `php -l` on every file, and an empty `debug.log`.

**What these won't prove:** the same limits as phase 2, namely behaviour on a real free host, and PHP 7.4 itself.
