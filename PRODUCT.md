# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary (confirmed 2026-10-22): owners of small Malaysian service businesses (salons and barbers, spas and clinics, restaurants, car workshops,
home and field services) deciding whether to adopt JustSimple. They open the online demo on a laptop or phone, play the customer booking a slot,
then switch to the business admin to see how the booking lands. Success: they trust the booking page enough to put it in front of their own customers.

Secondary: testers and developers clicking through the local test site or building against the public API.

## Product Purpose

JustSimple is a multi-tenant booking engine (Python/FastAPI) with a WordPress connector plugin and an embeddable widget. The demo website shows
the whole loop on real backend rules: the customer booking page (service, who, time, details, confirm), My bookings (sign-in by code or emailed link),
the business admin (bookings list, schedule board, set-up, plan and website) for owner, manager and staff roles, and self-service business sign-up.

## Positioning

One engine that handles five kinds of business with their real rules: staff shifts and leave, tables and private rooms, bays and parts waits,
visit addresses and service areas, approval holds, and "price after inspection".

## Operating Context

Malaysia: prices in RM, times in Malaysia time, addresses in Kuala Lumpur and surrounds. Messages (WhatsApp, SMS, email) are not really sent in
the demo; they appear in a Messages panel. The online demo is shared, behind an email register / log-in page, and its sample data resets nightly.

## Capabilities and Constraints

- Plain static HTML/CSS/JS, no build step; one page re-renders its markup on every state change (innerHTML).
- The same pages serve the local test site (`localsite/web`) and the online demo (`demo/server.py`); the demo's own pages live in `demo/pages.py` and `demo/access.py` (strict CSP: inline styles, Google Fonts only).
- Light theme only (decided earlier). Wording and behaviour of every screen stay as they are unless a layout change asks for it.

## Evidence on Hand

Five sample businesses with photos in `localsite/web/img` (free Unsplash photos, see CREDITS.md). No customers, testimonials or statistics exist;
none may be invented.

## Product Principles

1. The customer's booking page must feel finished enough to show a business's own customers.
2. Show the real rule at work rather than describing it.
3. The admin is a working tool: scanable, calm, fast to act in.
