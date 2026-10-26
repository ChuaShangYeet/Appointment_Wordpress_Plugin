# JustSimple Booking: standalone WordPress plugin plan

Status: **draft, waiting for confirmation** (Birdview step 2 of 4). No code is written until the scope below is confirmed.

## 1. What changes from the copied project

The copied project (`backend/`, `demo/`, `localsite/`) is a hosted multi-tenant engine in Python/FastAPI on PostgreSQL. The WordPress side in that design is a thin connector with **no booking logic** (booking_plan.md section 3). The new constraint is **WordPress only**, because it has to run on free hosting. So the engine is ported to PHP and runs inside the plugin.

| Engine today (evidence) | In the plugin |
| :--- | :--- |
| Multi-tenant, `TenantMixin` on every model (`models/base.py`) | One WordPress site = one business. No tenant column, no RLS |
| PostgreSQL `EXCLUDE USING gist` on `booking_allocations` stops double booking | MySQL/MariaDB has no exclusion constraints. The plugin uses `GET_LOCK('jsb:day:<date>')` + overlap re-check inside an InnoDB transaction. This is the same idea as the engine's own `lock_day()` (`services/allocators/base.py:18`) |
| PostGIS service radius (`allocators/field_service.py`) | Haversine distance in PHP, with the owner's base location entered as latitude/longitude. A list of postcodes or areas is the simpler alternative |
| Redis availability cache | WordPress transients, cleared when bookings or settings change |
| RabbitMQ + Celery (hold expiry, reminders) | WP-Cron, plus a "lazy sweep" each time availability is read. Free hosts often run WP-Cron only when someone visits, so the sweep keeps expired holds from blocking slots |
| WhatsApp / SMS / email | `wp_mail()` only in the free plugin. WhatsApp/SMS become hooks for a later paid upgrade |
| React admin SPA (blueprint) | Plain JS, no build step, served through the WP REST API. This matches `localsite/web`, and nothing has to be built before uploading to a free host |

**Hosting warning:** the free plan on WordPress.com does **not** allow plugins. The plugin needs self-hosted WordPress (free hosts such as InfinityFree, or any shared host). Minimum target: **PHP 7.4+, WordPress 6.2+, MySQL 5.7 / MariaDB 10.3**.

## 2. Architecture map

```text
 Visitor's browser                          WordPress (owner's site)
 ─────────────────                          ────────────────────────────────────────────────
 [justsimple_booking id=3]  ── renders ──▶  Public layer   shortcode + Gutenberg block (block.json, no build)
 <jsb-booking> web component                  │
        │ fetch (nonce)                       ▼
        └──────────────────────────────▶  REST  /wp-json/justsimple/v1/
                                              │   public: modules/{id}, availability, bookings (create), manage/{code}
                                              │   admin:  modules, services, resources, shifts, days-off, bookings  (capability + nonce)
                                              ▼
                                          Engine (pure PHP, no WordPress calls → unit-testable)
                                              Interval math · Policy · Availability
                                              Allocators: Staff · Salon(upsell) · Restaurant · Workshop · FieldService
                                              │
                                          BookingService ── GET_LOCK(day) + transaction ──▶ MySQL tables {prefix}jsb_*
                                              │
                                          Events ──▶ Notifications (wp_mail) · WP-Cron (hold expiry, reminders)

 Owner in wp-admin ──▶ Admin screens (plain JS) ──▶ same REST admin routes
```

**Booking modules:** an owner can create several modules on one site (for example "Haircuts" as a salon module and "Dinner" as a restaurant module). Each module has a **type** (one of the five verticals), its own services, resources, policy and look. Each one embeds with its own shortcode or block. This is the "create the appointment modules" job.

### Plugin layout

```text
justsimple-booking/
  justsimple-booking.php      bootstrap, constants, autoloader
  uninstall.php               drops tables only if the owner ticked "delete data"
  src/Engine/                 pure PHP: Interval, Slots, Policy, Availability, Allocator/*  (ported from backend/app/services)
  src/Data/                   Schema (dbDelta + version migrations), repositories over $wpdb
  src/Booking/                BookingService, StatusFlow, HoldSweeper, Notifications
  src/Rest/                   PublicController, AdminController
  src/Admin/                  menu pages, asset loading
  src/Front/                  Shortcode, Block
  assets/admin/  assets/front/   plain JS modules + CSS (no build)
  tests/                      PHPUnit tests for src/Engine (run without WordPress)
marketing/                    standalone static landing page (separate from the plugin zip)
```

### Tables (`{prefix}jsb_`)

`modules`, `services`, `service_resources`, `products` (salon retail), `resources` (staff / table / bay, with capacity and attributes JSON), `shifts`, `days_off`, `policies`, `customers`, `customer_addresses`, `vehicles`, `bookings`, `booking_allocations` (resource, start, end, state held/active/released), `status_history`. Every status the engine has stays: pending, confirmed, in-progress, waiting-for-parts, completed, cancelled, rejected, no-show.

## 3. Build order (all five verticals, one at a time)

Each phase ends with passing engine tests and a manual check in a local WordPress (WordPress Playground CLI via Node, or XAMPP, since PHP 8.2 is already installed).

1. **Skeleton:** plugin boots, activates, creates tables, has an empty admin menu, and uninstalls cleanly. A Birdview map (`architecture.json`, `constraints.reviewed.json`) is written alongside.
2. **Core engine + Staff (spa / clinic / consultant):** shifts, breaks, days off, booking window and notice, "any available" goes to the least-loaded staff member, pending holds that expire, a double-booking test with parallel requests.
3. **Salon:** add-ons lengthen the appointment, upsell overlap warning, retail stock taken off on completion.
4. **Restaurant:** smallest table that fits, combining tables (subset-sum) when separate seating is allowed, VIP tables never mixed with others.
5. **Workshop:** mechanic **and** bay both required, services stacked one after another, waiting-for-parts frees the bay.
6. **Field service:** radius check, travel buffer before and after, online meetings via a link the owner pastes (Google Meet API needs OAuth: later/paid).
7. **Frontends** (designed in parallel from phase 2, finished here): visitor widget, admin screens, marketing page.

## 4. Design and motion (before any frontend code)

Following anti-slop-design, the frontend starts with a **named visual thesis**, then type/colour/spacing tokens taken from it, then all eight interaction states for each component. Following the motion rules, an **interaction thesis** comes before any hover, loading or step transition is built. There's one hard rule for the widget: it is embedded inside *other people's* themes. So it has to be fully scoped (custom element + prefixed CSS), inherit the theme's font when the owner chooses, give the owner one accent colour, and respect `prefers-reduced-motion`. The existing DESIGN.md is reference only, not the source of truth.

## 5. Out of scope for the free plugin

Multi-tenancy, WhatsApp/SMS, Google Calendar sync, payments, Google Meet auto-links. The data model keeps the engine's field names, so a later "connect to the hosted JustSimple engine" upgrade can map them across.

## 6. Checks I will run, and what they don't prove

- PHPUnit on the engine, with the scenarios ported from `backend/tests` where they apply.
- A parallel-request test against the real MySQL lock for double booking.
- Installing the plugin zip into a clean local WordPress, then a booking end to end in each vertical.
- These don't prove the plugin behaves on every free host (WP-Cron timing, `GET_LOCK` permissions). One real free host should be tested before release.
