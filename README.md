# Appointment_Wordpress_Plugin

Booking for five kinds of business: restaurants, spas and clinics, salons and barbers, car workshops, and home or field services. This repository holds two ways of running it.

| | What it is | Where |
| :--- | :--- | :--- |
| **JustSimple Booking**, the standalone WordPress plugin | The whole booking engine ported to PHP, running inside WordPress with no outside service, so it works on free hosting. Current version **0.1.7** | `justsimple-booking/` (plugin), `showcase/` (demo-site plugin), `marketing/` (marketing page and build), `tools/` |
| The hosted engine | A multi-tenant FastAPI + PostgreSQL backend with a thin WordPress connector, a local test site and an online demo | `backend/`, `localsite/`, `demo/`, `prototype/`; see `SESSION_SUMMARY.md` and `backend/README.md` |

The standalone plugin is built on the branch `standalone-wp-plugin` (pushed to GitHub as `wordpress_demo`); `main` holds the hosted engine.

## The WordPress plugin

What it does, how to install it and its changelog are in [`justsimple-booking/readme.txt`](justsimple-booking/readme.txt) (the file WordPress shows on the plugin's details screen). In short: an owner uploads the zip, makes a booking page of one of five kinds, fills in its setup and puts its shortcode or block on a page. Customers book with a name and a mobile number (checked for the country they choose) and get a booking code and, by email, a link to change or cancel. It sends email only and takes no payments.

**Live demo:** https://js-appointment-demo.infinityfreeapp.com (InfinityFree). The front page is the marketing page, served by the JustSimple Showcase plugin, and its booking form books live against five demo businesses that are reset every night at 03:00 Kuala Lumpur time; emails there are caught, not sent. The Showcase also keeps a log of what visits to the page do, without cookies or IP addresses, under Tools › JustSimple Showcase.

How it was planned and built: [`WP_PLUGIN_PLAN.md`](WP_PLUGIN_PLAN.md) (the overall plan), then `PHASE2_PLAN.md` to `PHASE8_PLAN.md`, one per phase, each confirmed before it was built.

### Building

```bash
php marketing/build.php
```

writes `marketing/dist/`: the marketing page, `justsimple-booking-<version>.zip` (the plugin as WordPress installs it, without `tests/`) and `justsimple-showcase-<version>.zip` (the demo-site plugin, with the page inside). Every file the page loads carries a fingerprint of the build (`page.css?v=…`), because some hosts tell browsers to keep files for a year.

`python tools/make-phone-plans.py` rewrites the phone numbering plans (`justsimple-booking/src/Booking/phone-plans.php`, `assets/front/phone-countries.json`, `tests/phone-cases.json`) from Google's libphonenumber; it needs the Python packages `phonenumbers` and `babel`.

### Testing

On a local WordPress (XAMPP on Windows, the plugin folders linked into `C:/xampp/htdocs/jsb-test`; its logins are in `dev/local-test-site.env`, which is not in git):

| Command | What it checks |
| :--- | :--- |
| `php justsimple-booking/tests/run.php` | The booking engine, without WordPress (120 cases) |
| `php justsimple-booking/tests/phone.php` | The mobile-number check gives the backend's answer for 11,608 numbers of every country |
| `php justsimple-booking/tests/integration.php C:/xampp/htdocs/jsb-test` | The REST routes, data, emails, admin and page output against a real WordPress (307 checks) |
| `php justsimple-booking/tests/concurrency.php …` | Parallel races: started several times at once, exactly one booking gets a time |
| `php showcase/tests/showcase.php C:/xampp/htdocs/jsb-test http://localhost/jsb-test` | The demo site: loading, the nightly reset, caught mail, the front page, the visit log (36 checks) |
| `node marketing/test-demo.mjs [site]` | Books each of the five demo businesses by keyboard: the static page, a live site, or `?demo=recorded` |

### Putting a new version on the live demo

1. `php marketing/build.php`.
2. In the demo site's WordPress, upload `justsimple-booking-<version>.zip` and `justsimple-showcase-<version>.zip` under Plugins › Add New Plugin › Upload Plugin, choosing **Replace current with uploaded**.
3. Click **Performance › Purge All Caches** (W3 Total Cache is active there; otherwise the front page can stay on an old copy).
