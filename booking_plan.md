# Headless Booking Platform: Technical Blueprint & Architecture (v3.0)
**Project:** JustSimple Booking Platform  
**Target Delivery:** Multi-Tenant Hosted SaaS Engine (Python/FastAPI) + WordPress Connector Plugin + Headless Embeddable JS Widget / Public REST API  
**Supported Verticals:** Restaurant, Spa / Clinic, Salon / Barber, Automotive Services, Professional Field/Consultation Services  

---

## 1. System Architecture & Topology

The platform runs as a hosted multi-tenant cloud service. Client frontends (WordPress sites, Next.js web applications, mobile apps) communicate directly with the API Gateway without routing booking computation through client servers.

```text
┌────────────────────────────────────────────────────────────────────────┐
│                          CLIENT FRONTENDS                              │
│                                                                        │
│   ┌─────────────────────┐   ┌───────────────────┐   ┌──────────────┐   │
│   │ WordPress Site      │   │ Headless Web App  │   │ Mobile App   │   │
│   │ (Plugin Connector   │   │ (Next.js / Vue /  │   │ (iOS/Android │   │
│   │  & Admin SPA)       │   │  Web Component)   │   │  via SDK)    │   │
│   └──────────┬──────────┘   └─────────┬─────────┘   └──────┬───────┘   │
└──────────────┼────────────────────────┼────────────────────┼───────────┘
               │ (Public Traffic & JWT) │                    │
               ▼                        ▼                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        API GATEWAY (Traefik / Kong)                    │
│   - Rate limiting & DDoS protection                                    │
│   - Tenant resolution via API keys & CORS domain whitelist             │
│   - JWT verification for Admin & Staff actions                         │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
    ┌───────────────────────────────┼──────────────────────────────┐
    ▼                               ▼                              ▼
┌──────────────────────┐  ┌──────────────────────┐  ┌────────────────────┐
│ Identity & Tenant    │  │ Catalog Service      │  │ Scheduling Core    │
│ - Tenant onboard     │  │ - Services & bundles │  │ - Availability calc│
│ - API key management │  │ - Add-ons & retail   │  │ - Allocation engine│
│ - User auth & roles  │  │ - Supported vehicles │  │ - Booking states   │
└──────────────────────┘  └──────────────────────┘  └─────────┬──────────┘
                                                              │
                                       ┌──────────────────────┼──────────┐
                                       ▼                      ▼          ▼
                             ┌──────────────────┐  ┌─────────────┐  ┌────┴─────┐
                             │ Customer & PDPA  │  │ Notification│  │Integrat- │
                             │ - Profiles       │  │ - WhatsApp  │  │  ions    │
                             │ - Car registry   │  │   Cloud API │  │ - Google │
                             │ - Encrypted notes│  │ - Email/SMS │  │   Sync   │
                             └──────────────────┘  └─────────────┘  └──────────┘
                                       │                      │          │
                     ┌─────────────────┴──────────────────────┴──────────┴─────┐
                     │              ASYNC EVENT BUS (RabbitMQ + Celery)        │
                     │  - Outbox Pattern for guaranteed delivery               │
                     │  - Pending hold auto-rejections & reminders             │
                     └─────────────────────────────────────────────────────────┘
```

---

## 2. Technology Stack

* **Backend Framework:** Python 3.12+ with FastAPI & Pydantic v2 (auto-generates OpenAPI / TypeScript SDK).
* **Database Engine:** PostgreSQL 16 with:
  * `btree_gist`: Enforces mathematically unbreachable exclusion constraints to prevent double-booking.
  * `postgis`: Calculates geographical distance and service radius bounds for on-site services.
* **ORM & Database Migrations:** SQLAlchemy 2.0 (AsyncIO) + Alembic.
* **Caching & Distributed Locks:** Redis 7.
* **Message Broker & Background Tasks:** RabbitMQ with Transactional Outbox + Celery & Celery Beat.
* **Tenant Isolation:** PostgreSQL Row-Level Security (RLS) setting `app.tenant_id` per database session.

---

## 3. WordPress Plugin Design (Thin Connector)

The WordPress plugin contains **zero booking calculation logic** to keep client hosting lightweight and prevent server crashes.

1. **Authentication Handshake:**
   * Upon plugin activation, the site administrator clicks **Connect to JustSimple Engine**.
   * Registers or links the domain to an engine `tenant_id`.
   * Securely saves `publishable_key` and encrypted `server_secret` in `wp_options`.
2. **Admin Dashboard Interface:**
   * Embedded React Single-Page Application (SPA) loaded inside the standard `wp-admin` menu.
   * WordPress exchanges the `server_secret` for a short-lived administrative JWT scoped to the current user's role (`owner`, `manager`, or `staff`).
3. **Frontend Integration:**
   * Provides `[booking_widget]` shortcode and a native Gutenberg block.
   * Renders a lightweight Custom Web Component `<justsimple-booking-widget>`.
   * Booking form traffic connects directly from the visitor's browser to the FastAPI gateway.

---

## 4. Unified Resource Model & Database Schemas

All 5 business verticals use a polymorphic **Resource Allocation Model**: a booking reserves one or more `resources` (`staff`, `table`, `bay`) over a specific timestamp interval (`tstzrange`).

### 4.1. Core PostgreSQL Schemas

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS postgis;

-- 1. Booking Policies & Rules
CREATE TABLE booking_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    name VARCHAR(100) NOT NULL,
    default_status VARCHAR(20) NOT NULL,            -- 'pending' | 'confirmed'
    min_notice_booking INTERVAL NOT NULL,           -- e.g. '2 hours'
    min_notice_cancel INTERVAL NOT NULL,            -- e.g. '24 hours'
    booking_window INTERVAL NOT NULL,               -- e.g. '60 days'
    pending_timeout INTERVAL NOT NULL               -- e.g. '30 minutes' (auto-reject hold)
);

-- 2. Unified Resources (Staff, Tables, Workstation Bays)
CREATE TABLE resources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    type VARCHAR(20) NOT NULL,                      -- 'staff' | 'table' | 'bay'
    name VARCHAR(150) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',   -- 'active' | 'inactive'
    capacity INT,                                   -- Table seat count (NULL for staff/bays)
    attributes JSONB NOT NULL DEFAULT '{}'          -- VIP status, min spend, gender, skills
);

-- 3. Resource Shifts & Operating Hours
CREATE TABLE resource_shifts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id UUID NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    weekday SMALLINT NOT NULL,                      -- 0 (Sun) to 6 (Sat)
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    break_start TIME,
    break_end TIME
);

-- 4. Global & Resource Days Off
CREATE TABLE days_off (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    resource_id UUID REFERENCES resources(id) ON DELETE CASCADE, -- NULL = Company Holiday
    name VARCHAR(150) NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    repeat_yearly BOOLEAN NOT NULL DEFAULT FALSE
);

-- 5. Main Bookings Table
CREATE TABLE bookings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    status VARCHAR(30) NOT NULL,                    -- pending, confirmed, in-progress, waiting-for-parts, completed, cancelled, rejected, no-show
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    policy_snapshot JSONB NOT NULL,                 -- Immutable snapshot of policy at booking time
    party_size INT,
    vehicle_id UUID,
    address_id UUID,
    meeting_link TEXT,
    special_request TEXT,
    health_notes_enc BYTEA,                         -- AES-256 encrypted health data (PDPA)
    hold_expires_at TIMESTAMPTZ,                    -- Hold deadline for pending state
    source VARCHAR(20) NOT NULL DEFAULT 'web'       -- 'wordpress' | 'headless' | 'admin'
);

-- 6. Resource Allocations (Guaranteed Anti-Double-Booking Engine)
CREATE TABLE booking_allocations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    booking_id UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    resource_id UUID NOT NULL REFERENCES resources(id),
    period TSTZRANGE NOT NULL,
    state VARCHAR(20) NOT NULL,                     -- 'held' | 'active' | 'released'
    
    -- Absolute zero-conflict exclusion constraint
    EXCLUDE USING gist (
        resource_id WITH =,
        period WITH &&
    ) WHERE (state IN ('held', 'active'))
);
```

---

## 5. Vertical-Specific Allocation Strategies

The backend applies dedicated allocation engines depending on the business vertical:

| Vertical | Strategy Class | Business Rule Solved |
| :--- | :--- | :--- |
| **Restaurant** | `RestaurantAllocator` | • Capacity-matching: assigns smallest fitting table.<br>• Splitting solver: uses subset-sum optimization if `allow_separate_seating = yes`.<br>• Isolation: VIP and normal tables are never combined. |
| **Spa / Clinic** | `StaffAllocator` | • Shift & break filtering.<br>• "Any Available" auto-routes to qualified, least-loaded staff.<br>• Encrypts health notes per PDPA requirements. |
| **Salon / Barber** | `StylistUpsellAllocator`| • Add-ons dynamically extend interval.<br>• Real-time alert if in-progress upsell overlaps stylist's next appointment.<br>• Deducts retail inventory upon completion. |
| **Automotive** | `WorkshopAllocator` | • **Double Constraint:** Slot requires both a qualified mechanic AND an approved bay.<br>• Multi-service sequential segment stacking.<br>• `waiting-for-parts` state truncates allocation to free bays immediately. |
| **Professional** | `FieldServiceAllocator` | • **PostGIS Radius Validation:** Rejects requests outside `service_radius_km`.<br>• Dynamic travel buffer added before and after on-site appointments.<br>• Auto-generates Google Meet links for virtual consultations. |

---

## 6. Expected Query Performance (PostgreSQL 16 + Redis)

| Database Operation | Engine Mechanism | Target Latency |
| :--- | :--- | :--- |
| **Live Day Availability (Cached)** | Redis key fetch (`GET availability:{tenant}:{date}`) | **0.2ms – 0.5ms** ⚡ |
| **Live Day Availability (Uncached)** | `tstzrange` GiST intersection with shift intervals | **1.2ms – 3.0ms** 🚀 |
| **Mechanic + Bay Double Check** | Multi-resource GiST candidate intersection | **1.8ms – 3.5ms** 🚀 |
| **Atomic Booking Reservation** | `INSERT INTO booking_allocations` with GiST lock | **3.0ms – 6.0ms** 🚀 |
| **Auto-Reject Expired Holds** | Background Celery task processing expired holds | **5.0ms – 10.0ms** (Async) |

---

## 7. Development Roadmap

1. **Phase 1 — Core Platform:** Multi-tenant auth, API Gateway, WordPress plugin connector, Core Scheduling engine with Staff allocation (Spa/Clinic & Salon/Barber).
2. **Phase 2 — Restaurant Engine:** Table capacity solver, open-table daily rules, minimum-spend agreements, separate-seating combinations.
3. **Phase 3 — Automotive Engine:** Vehicle compatibility matrix, service bay assignments, double-constraint booking solver, parts ordering status flow.
4. **Phase 4 — Professional Field Services:** PostGIS geofencing, dynamic travel buffer calculations, two-step inspection-to-service workflow, Google Meet integration.
5. **Phase 5 — Notifications & Channels:** Meta WhatsApp Cloud API templates, SMS/Email fallbacks, tenant webhooks.
