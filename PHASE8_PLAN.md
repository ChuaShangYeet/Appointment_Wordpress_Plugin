# Phase 8 plan: a live demo of the plugin

Status: **confirmed as recommended (separate Showcase plugin, front page, 03:00 reset; InnoDB fix committed on its own as `84eb7d8`); built and checked locally, and checked on the host on 29 Oct 2026 (booking 0.1.2 and Showcase 0.1.0 active, five demo pages, a live booking through the front page with both emails caught, Reset now in 0.5 s leaving five fresh pages and no caught mail); not committed.** Branch `standalone-wp-plugin`; last commit `84eb7d8`.

The marketing page's demo replays answers recorded from the engine. Phase 8 makes it live: a real WordPress site with the plugin, five demo businesses that reset every night, and the marketing page served from that same site so its widget books against the real engine.

The site exists: **https://js-appointment-demo.infinityfreeapp.com** (InfinityFree, WordPress 7.0.2, PHP 8.4, plugin 0.1.1, System status all green).

---

## 8.1 What we found that decides the design

- **InfinityFree's browser check.** A request from outside a browser that has visited the site gets an HTML page running `/aes.js` instead of JSON (checked with curl). Requests from a page *on* the site pass (checked in the browser pane: 200 JSON). So the marketing page must be served by the demo site itself; hosted anywhere else, its widget could not reach the API.
- **No command line on the host.** `tests/seed-demo.php` and `record-demo.php` are CLI scripts; the demo data must instead be loaded from inside WordPress (a button, and a scheduled job).
- **Real email.** On a live host, `wp_mail()` really sends. Demo visitors type any address into the booking form, so the demo must not send mail to them.
- **Demo data does not belong in the product.** Every business that installs the booking plugin would otherwise carry five fake businesses, a reset job and a marketing page.

## 8.2 The shape: a second plugin, "JustSimple Showcase"

A small plugin in `showcase/` (its own zip, never part of the booking plugin's zip), installed only on the demo site. It needs the booking plugin to be active and does nothing otherwise.

| Piece | What it does |
| :--- | :--- |
| **Demo data** | "Load demo businesses" builds the five pages (Klinik Dr Wong, Mei Hair Studio, Rumah Makan Seri, Bengkel Ah Kow, CoolAir Services) through the booking plugin's own REST routes, as `seed-demo.php` does, so the engine allocates every booking. Ported from the CLI script into a class both can use |
| **Reset** | "Reset the demo now" button on its admin page; nightly at a set hour via WP-Cron (default 03:00 Kuala Lumpur, as the Render demo). Removes only what the showcase made or visitors added on those pages: everything is tagged to the demo pages by id |
| **No mail** | While the showcase is active, every `wp_mail()` is caught and kept in a short log on its admin page ("what would have been sent"), never sent |
| **The marketing page** | Served at the site's front page from the built `marketing/` files (an option chooses front page or `/plugin/`). Its widget uses the live site: the real `RestSource` against the same site |
| **Fallback** | If the live API answers with something other than JSON (the host check, an outage), the page switches that widget to the recorded `DemoSource` and says so in its note |

The booking plugin itself changes in one way only: the widget's demo note becomes a choice of the page ("live demo" vs "recorded demo"), and nothing else.

## 8.3 Files

| File | What |
| :--- | :--- |
| `showcase/justsimple-showcase.php` | Plugin header, requires the booking plugin, wires the parts |
| `showcase/src/DemoData.php` | The five businesses, built through REST; used by the button, the cron job and `tests/seed-demo.php` |
| `showcase/src/Reset.php` | Removes the demo pages and their bookings, rebuilds them; the nightly schedule |
| `showcase/src/MailCatcher.php` | Catches mail while active; keeps the last 50 |
| `showcase/src/Front.php` | Serves the marketing page and its files; tells `page.js` the demo pages' ids |
| `showcase/admin/` | One admin page: last reset, Reset now, reset hour, caught mail |
| `marketing/page.js` | Live source first, recorded fallback; ids come from the site |
| `marketing/build.php` | Also builds `justsimple-showcase-<version>.zip` with the marketing files inside |
| `tests/showcase.php` | Integration checks for load, reset, mail catching and the served page |

## 8.4 Checks

1. **Local first**, on the XAMPP test site: install the showcase zip as you would on the host; Load demo, Reset now, the nightly job (run by hand), caught mail; `tests/showcase.php`.
2. The marketing page served by the local WordPress: the five demo lines booked by keyboard against the live engine (`marketing/test-demo.mjs`, pointed at the site); then with the API switched off, the same lines on the recorded fallback.
3. The design inspector and linter on the served page, 0 FAIL at three widths.
4. The booking plugin's suites stay green (120 engine, 293 integration).
5. **Then on your host**, with you uploading the two zips: System status, Load demo, a booking through the marketing page in the browser pane, Reset now, and the caught-mail list.

## 8.5 Decisions for you

1. **Where the demo code lives.**
   - **Recommended:** the separate "JustSimple Showcase" plugin above, only on the demo site.
   - Or inside the booking plugin, switched off by default (every install carries it).
2. **Where the marketing page appears on the demo site.**
   - **Recommended:** the front page (`https://js-appointment-demo.infinityfreeapp.com/`), with the booking pages at their own addresses.
   - Or at `/plugin/`, keeping WordPress's normal front page.
3. **Nightly reset hour.** **Recommended:** 03:00 Kuala Lumpur, like the Render demo. Note that WP-Cron runs only when someone visits, so a reset can run later than the hour on a quiet night.
4. **Commit the InnoDB fix now, on its own** (it is a real fix for any host), or with this phase. **Recommended:** now, on its own.
