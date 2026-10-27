# Phase 7 plan: the frontends (booking widget, admin screens, marketing page)

Status: **7a committed (`29ba089`); 7b confirmed (Part 4, with the recommended staff view and two halves); 7b committed (`33ce3e3`, `4edade7`); 7c built and checked (Part 5), not committed.** Map: `.birdview/architecture.json` revision 9. Branch `standalone-wp-plugin`.

The engine is finished: five module types and 43 REST paths (71 path-and-method pairs), all tested. Phase 7 gives it faces: the **booking widget** customers use on an owner's site, the **admin screens** owners and staff work in, and the **marketing page** that sells the free plugin. It is three pieces of work, so it runs as **three sub-phases (7a widget, 7b admin, 7c marketing)**, each built and checked before the next. The design below is settled once, for all three.

Method: the `design-taste-frontend` skill (the anti-slop rules) with its own scripts dealing the directions, so the look isn't my default. Admin and widget follow its **app-screen** run; the marketing page follows its **page** run. Dealt sheets: `.taste-skill/app/direction.json` (accent-ladder, seed 1367479260) and `.taste-skill/page/direction.json` (sentence-as-layout, seed 228969607).

---

## Part 1. The design thesis

### 1.1 The idea, in one sentence

> On every surface, **the thing being booked** (a stylist, a chair, a table, a bay, a visit) **is shown, not named**: the widget leads with the real free times of that thing, the admin's main block is the day drawn as those things in rows, and the marketing headline puts that thing inside the sentence where a word would be.

**Competitor test:** put Amelia or Bookly in the sentence and it fails. They book "an appointment with a staff member"; this plugin books a *table for 8 across two tables*, a *bay and a mechanic*, a *visit with 30 minutes of drive each way*. The five kinds of thing are what only this product has (PRODUCT.md, principle 2: "Show the real rule at work rather than describing it").

### 1.2 One brand, three surfaces

| | Choice | Why |
| :--- | :--- | :--- |
| **Brand colour** | **Moss** `#4f6238`, the page's dealt accent. The admin ladder is built from it, replacing the dealt raspberry | One hue across page, widget and admin, as the skill requires. Moss is also close to the green JustSimple already used (`#157150`). The admin sheet allows "the ladder built from the brand's own hue" |
| **Typeface** | **DM Sans**, 400/500/600/700, **bundled in the plugin** (woff2, SIL Open Font License) | WordPress.org accepts only GPL-compatible assets, and a plugin that promises "no outside service" can't call Google Fonts on every visitor's page view. The page's dealt type (Bebas Neue / EB Garamond, then Erode) was redrawn: see 1.5 |
| **Contrast** (measured) | White on moss 6.70:1 · moss on white 6.70 · moss on sand 4.63 · ink on sand 9.95 · muted on sand 5.01 · accent-ink on accent tint 10.62 · muted on white 5.78 | Every pairing passes WCAG AA |

**The moss ladder** (one hue; full strength on small marks only, tints on areas):

```css
:root {
  --brand: #4f6238;        /* full hue: the one filled button, the data series, selected marks */
  --brand-ink: #2e3a20;    /* text on tints */
  --brand-soft: #eef2e8;   /* 50: selected rows, active nav, badges */
  --brand-track: #dde5d3;  /* 100-200: progress tracks, the "held" span, slot hover */
}
```

### 1.3 Surface 1: the booking widget (7a)

**Where it lives:** inside *other people's* themes, which we can't predict. That fixes the architecture before any styling:

- **A web component, `<jsb-booking module="3">`, with a shadow root.** The theme's CSS can't reach inside and ours can't leak out. The shortcode `[justsimple_booking id="3"]` and a Gutenberg block (`block.json`, server-rendered, no build step) both print it. Without JavaScript, it falls back to a plain message with the business's phone number from the module settings.
- **Owner controls (module settings), only two:**
  - *Colour:* one accent, default moss. The tints are computed from it in OKLCH. If the owner's colour fails 4.5:1 against white, the button text switches to ink, so a pale brand colour can't make an unreadable button.
  - *Font:* "use my theme's font" (the default) or DM Sans. Inheriting is right inside someone else's site.
- **Size and shape:** the widget fills its container, from 320px to 760px wide. Square-ish corners (6px controls, 8px panels) and no shadows, so it sits on any theme's ground. Light only in 7a (DESIGN.md's earlier decision); a dark variant is left for later.

**The widget's one question:** *"When can I come?"* The biggest thing is always the **day strip and its free times**. What changes per kind is the small set of choices that decide those times, asked first and kept on the same surface:

| Kind | Asked before the times | The rule the customer sees at work |
| :--- | :--- | :--- |
| Staff (spa, clinic) | service; who (any or named) | times shown are only where that person (or anyone) is free |
| Salon | one base service; add-ons as toggles | adding an add-on *visibly lengthens* each time block (10:00-11:15 → 10:00-11:40) and drops starts that no longer fit |
| Restaurant | party size; normal/VIP; "we can sit at separate tables" | a party of 8 shows "2 tables" on the times that need combining; VIP shows the minimum spend on each time, and the confirm button repeats the amount the customer accepts |
| Workshop | services (stacked); the car (plate, model, year) | an unsupported car is refused at the car step; a part on order shows its lead time, with a required tick before the confirm button |
| Field | service, with *in office / online / at your place* stated; for on-site, the location | at-your-place shows the visit time, never the travel padding; outside the radius says how far and how far they go |

**Flow:** one column, progressive. Each answered step collapses to one summary line ("Haircut + Gel finish · 75 min · change") above the next. Steps: **choose → when → your details → done**.
- *Done* shows the **booking code at display size**. It's the thing people read out over the phone, which is why its alphabet has no 0/O or 1/I/L.
- It also shows what was booked, the address or meeting link, and the cancel deadline.

**The manage page:** the same component given `?jsb_booking=CODE&jsb_token=…` (the `jsb_manage_url` filter from phase 2 points there). It shows the booking and a Cancel button while cancelling is still allowed.

**Interaction thesis (widget):** motion is feedback only; nothing performs an entrance.

| Moment | Behaviour |
| :--- | :--- |
| Press any control | Scale 0.98 for 90 ms, ease-out. Hover only on devices with hover (`@media (hover: hover)`) |
| Choose a time | The chosen time fills with the brand colour at once (no animation); the next step opens below with height + opacity, 200 ms, `cubic-bezier(.2,.7,.2,1)`. Focus moves to the new step's heading |
| Times loading (new day or new choice) | The previous times stay, dimmed to 45% and inert. New times replace them in one swap. Nothing shows for loads under 300 ms; after that, one line of text: "Finding free times…". No skeletons, no spinners |
| The time was taken meanwhile (409 with fresh slots) | The chosen time strikes through, a sentence says so, and the fresh times replace the list with the nearest free time focused. The customer's details are kept |
| VIP price went up (422 with the new amount) | The new amount is shown beside the confirm button with "Accept 300.00 and book"; nothing is booked silently |
| Done | The code appears with no confetti and no checkmark animation; the page scrolls the widget's top into view once |
| `prefers-reduced-motion` | No height or scale travel; opacity changes only, at 120 ms |

**Accessibility (a requirement, not polish):**
- Every step is a `<fieldset>` with a `<legend>`, and times are radio inputs.
- The day strip is a listbox with arrow-key movement.
- Errors are announced through one `aria-live="polite"` region.
- Targets are 44 px on touch screens, focus is visible on every control, and everything can be done with the keyboard alone.

### 1.4 Surface 2: the admin screens (7b)

**Style: accent-ladder**, light, built in moss. I chose it over the other dealt style, framed-sheet, because framed-sheet's whole idea is its own tinted sidebar-and-top-bar frame. Inside wp-admin, WordPress already owns the sidebar and top bar, so that can't be built. Accent-ladder works entirely on the page canvas.

**Shell:** WordPress's own menu is the sidebar ("Booking › Today / Bookings / Modules / Settings / System status"). Inside our pages:
- The title row, with module tabs as underlines (a 2 px moss underline on a full-width 1 px rule).
- The canvas on a ground one step toward the moss tint (`#f8f9f6`), with cards in white.
- No shadows. Corner radii: 8 px for cards, 6 px for controls.
- Colour budget 3-10%, almost all of it tints.

**Tokens:** the dealt sheet's, with the raspberry replaced by the moss ladder above, plus DM Sans at 32 / 18 / 24 / 14 / 12 px, tabular figures on every number, and one 24 px gap. Status pills use the sheet's positive/negative/warning/neutral pairs. "In progress" is moss, "waiting for parts" is warning, "pending" is neutral with a dashed edge.

**Screens, each with its one question:**

| Screen | Its question | Biggest block | Main action, one click away |
| :--- | :--- | :--- | :--- |
| **Today** (per module) | *What's happening today, and what needs me?* | **The day board** (below) | Confirm a waiting request from the "Needs you" rail |
| **Bookings** | *Find a booking and act on it* | The table: status tabs, search, date range, filters (table class, schedule clash) | The row's main next step (confirm / start / complete) |
| **Booking detail** (a side sheet over the list) | *What was booked, and what can I do next?* | The booking's facts by kind: services and add-ons, party and tables, car and parts, address or meeting link, follow-up chain | Only the actions its status and kind allow: add items, parts, waiting for parts, resume, follow-up, actual duration, meeting link |
| **Modules** | *Which booking pages do I have, and where are they?* | One row per module: kind, status, today's count, its shortcode with a copy button | New module |
| **Set up a module** (two-column settings) | *Is this page ready to take bookings?* | A readiness list at the top ("2 of 4 done: add staff hours, set seating times"), then sections by kind | Turn the module on |

**The signature element: the day board.** This is the sheet's "team timeline with kind encoding", applied to the day.
- **Layout:** the module's resources down the left (stylists, tables, bays or professionals), hours across, a 1 px moss line at "now".
- **Booking bars, drawn by status:**
  - confirmed: moss tint fill
  - pending: dashed outline, with the hold's expiry time as its label
  - in progress: full moss
  - waiting for parts: diagonal hatch
  - field travel padding: a paler extension either side of the visit
  - schedule clash: outlined in the negative red and named
- **Off-shift time** is one tone step darker.
- **On a phone:** one resource at a time, swiped.

This board shows the real rules better than any description could. It also becomes the marketing page's interface pictures (7c).

**The finding line** under each title states the one fact that needs attention, with its baseline. For example: "2 requests waiting; the first hold expires at 15:30" or "Bay 2 runs into the next job by 25 min". If nothing needs attention, it says so: "Nothing waiting. 6 bookings today, 2 done."

**Interaction thesis (admin):** feedback, not performance.
- A row's status change updates in place, after a 160 ms tint flash on that row.
- The detail sheet slides in from the right (220 ms, and the list stays usable behind it).
- A button shows its pending state on press and **never updates before the server answers**, because the engine decides.
- Saving settings shows "Saved" beside the button for 2 s.
- Destructive actions (reject, cancel, delete a module) ask in place, never in a modal.
- Keyboard: `j`/`k` move through bookings, `Enter` opens the detail, `Esc` closes it.

**How it's built:** plain JavaScript modules with no build step, as in phases 1-6. Requests go through `wp.apiFetch`, which WordPress already ships, so it sends the nonce for us. DM Sans is enqueued from the plugin.

**Every block has every state:** empty (a new module: "No bookings yet. Your booking page is at …, copy the shortcode"), loading, error, a zero that reads as a zero, and a dash for a missing value.

### 1.5 Surface 3: the marketing page (7c)

**Direction: "The sentence is the layout"** (dealt from three; chosen because its device, a picture *inside* the headline where a word would be, is exactly the idea in 1.1). Hero: "Title in the sky, line on the horizon". Palette: **sand and moss** (ground `#ddd6c6`, ink `#2a2a26`, accent moss on the one action, one deep-leaf field `#3f5a2a` to close). Signature motion: the **shutter image**.

**Redraws, with reasons (the skill requires each to be recorded):**

1. **Type → DM Sans** (from Bebas Neue / EB Garamond, then Erode). The page must show the real widget and admin, and the skill requires page and app to share one typeface. The first pairing is, in the sheet's own words, for "crafted physical products, spirits, outdoor goods". The second has a licence WordPress.org won't accept for bundling. DM Sans is set at display size at weight 600, tracking -0.04 em, leading 0.95.
2. **Nothing else redrawn.** Hero, sections and palette are adopted as dealt.

**The headline** (one slot, the thing being booked, cycling):

> **Book any [ slot ] online.**
> *(about 24 characters; two lines on a phone with the slot on its own line between them)*

The slot is one line-height tall and 2-3 ems wide. It cycles through five crops: the **real widget's time list** for a stylist, a table plan, a bay, a visit with its travel padding, a consultation link. Each holds for 2.4 s. It stops when the pointer is over it, and under reduced motion it shows only the first crop. The supporting sentence, split across the horizon:
> *A free WordPress plugin for salons, clinics, restaurants, workshops and home services. Every booking is worked out on your own site.*

**The page's argument** (claims, then proof):

| Section (dealt blueprint) | Claim | Proof on the page |
| :--- | :--- | :--- |
| Hero (title in the sky) | It books the *thing* your business has, not just "appointments" | The cycling slot, which shows the real widget |
| Showcase (headline you can type) — **the one live thing** | Each kind of business has its own rules, and they work | A real `<input>` prefilled with "Table for 8, Friday 8pm". Below it, **the real widget in demo mode** (bundled sample data, no server) answers: 2 tables, the times that fit. Three example buttons refill the line: "Haircut and gel, Saturday", "Brake service for a 2018 City", "Aircon check at my place". A few keywords pick the module kind; anything else falls back to the five buttons |
| List (statement, table pushed right) | Five kinds, each with its real rule | Five rows: kind · what it books · the rule ("Workshop · a mechanic and a bay · both must be free for every service"), with the project's own photograph of that kind beside |
| How it works (stages as one bar) | Minutes to set up, then it just runs | One bar whose segments are true to time: "Install, 1 minute · Make a module, 5 minutes · Paste the shortcode, seconds · Takes bookings, all day" |
| FAQ (wide heading, indented questions) | The worries | Six real questions: does it work on WordPress.com's free plan (no, and why); where is the data (your database); what if two people book the last slot (exactly one gets it, tested with 10 at once); payments (not taken); is there an account (no); what happens on uninstall (data kept unless you choose) |
| CTA (the idea inverted) | Get it | "Book any [slot] online" echoed on the moss field, with one button: "Download the plugin" |
| Footer (contact as headline) | Talk to us | The contact address as the display line. Photo credits and the city small |

**Pictures (the skill's floor is 4):**
- The project's six Unsplash photos, credited, as atmosphere for each kind. They show the world of each kind, never "our customers".
- The real widget and day-board fragments, marked `data-ui`.
- One muted looping clip if an openly licensed one exists (searched with the skill's `assets.mjs`); otherwise a slow drift on the restaurant photograph.

**No invented proof:** no customer counts, logos, quotes or star ratings. The only figures on the page are the product's own facts (five kinds, PHP 7.4+, the race test's "1 of 10").

**Interaction thesis (page):**
- The **shutter** opens the hero slot once (clip-path from the left, 1.1 s, with the picture settling from 1.12 to 1).
- Each section's heading, text and picture enter staggered (70 ms, rising 24 px, once).
- Every control has press, hover and focus feedback.
- The cycling slot is the one moving picture.
- Only transform and opacity animate, and every move has a reduced-motion fallback.

**Where it lives:** a static page in `marketing/` (outside the plugin zip), with the demo widget loaded from the plugin's own `assets/front` in demo mode. Publishing it (a site, or a shareable artifact link) is decided when 7c is done.

---

## Part 2. Scope and build order

### 7a. Booking widget (first, because 7b and 7c show it)

- **Code:**
  - `src/Front/Shortcode.php`, `src/Front/Block.php` + `blocks/booking/block.json` (server-rendered, no build)
  - `assets/front/jsb-booking.js` (the web component, ES module), `assets/front/jsb-booking.css` (loaded into the shadow root)
  - `assets/fonts/dm-sans-*.woff2` + `OFL.txt`
- **Engine additions, small:** the module's look (`settings.accent`, `settings.font`); the public routes return the business phone for the no-script fallback; a `demo` data set and loader for 7c. No schema change.
- **Checks:**
  - The skill's inspector in app mode (`inspect.mjs --app`) at 1440, 768 and 390.
  - The widget embedded in **Twenty Twenty-Three, Twenty Twenty-Four and Twenty Twenty-Five** (already on the test site) and **one popular classic theme**, to prove theme CSS can't break it.
  - A full keyboard-only booking for each of the five kinds; a screen-reader pass over the step structure (NVDA, if installed); reduced motion.
  - The 409 race shown in the widget (two browser tabs).

### 7b. Admin screens

- **Code:**
  - `src/Admin/Menu.php` (new pages)
  - `assets/admin/*.js` (Today, Bookings, Detail, Modules, Setup), `assets/admin/admin.css` (the tokens above)
  - Sample data comes from the real REST routes on the test site, never invented in the page.
- **Checks:**
  - `lint.mjs --app` and `inspect.mjs --app` (no FAIL) at three widths.
  - The skill's seven app-screen questions answered in writing for each screen, three rounds at most.
  - Every state looked at (empty, loading, error, zero).
  - Two roles walked through: an owner, and a stylist linked to a staff member (own appointments only).

### 7c. Marketing page

- **Code:** `marketing/index.html`, `marketing/page.css`, `marketing/page.js`; photos and credits; the demo widget.
- **Checks:**
  - The skill's full page gate: `lint.mjs`, then `inspect.mjs` with **0 FAIL at 1440 / 768 / 390**, every screenshot opened.
  - The written critique (three rounds at most), and the motion floor of at least 8 moving elements.
  - A 5-second first-screen test: what is it, and what do I do?

### Birdview

Each sub-phase gets its own planned → editing → verifying → completed events. The map adds the front-widget module's evidence and turns its `visitor → widget → REST` relationships from "uncertain" to "supported".

### Left out of phase 7

- Customer sign-in ("my bookings"), saved vehicles and addresses, and rescheduling by the customer.
- A dark widget variant, and translations beyond the text domain being ready.
- The WordPress.org listing assets (banner, screenshots), which come after 7c.

---

## Part 3. Decisions for you

1. **Order:** **7a → 7b → 7c, each confirmed separately (recommended)**, or all three in one go.
2. **The on-site location in the widget.** Phase 6 needs map coordinates for at-your-place services, and there's no geocoding.
   - **Recommended:** a **"Use my location" button** (the browser's own, free, no outside service). Then an **optional map**, loaded only when the customer taps "Pick on a map". It uses OpenStreetMap tiles with attribution, and the owner can switch it off. Loading tiles sends the visitor's IP address to OpenStreetMap; the usage policy allows light use.
   - **Strict alternative:** location button only, with no map at all. A customer whose browser can't share location (or who is booking for another address) can't book an on-site visit online and is shown the business's phone number.

---

## Part 4. 7b scope plan: the admin screens

The design is settled in 1.4. This part pins down what gets built, what the server must add, and how it is checked.

### 4.1 What exists and what is missing

- **Exists:** 25 admin routes cover modules, services, staff, policies, days off, tables, bays, vehicles, products, shifts, hours, seating, table rules, booking list/detail/status/items/parts/resume/meeting link/follow-up/actual duration/history. The setup screen and the booking detail need nothing new.
- **Missing 1, the day board.** `GET /admin/modules/{id}/bookings` returns one span per booking. The board needs each resource's own bars (a mechanic per service, a visit with its travel padding) and each resource's working hours that day, and shifts are readable only by managers.
- **Missing 2, search.** The bookings list filters by status, date, table class and clash, but cannot find "Tan" or "0123" or a code.
- **Missing 3, readiness.** "2 of 4 done" must come from the server (one rule per kind, testable), not be guessed in the page.
- **Today's admin page** is phase 1's placeholder ("Creating modules arrives in the next release").

### 4.2 Server additions (small, read-only)

| Addition | Shape | Notes |
| :--- | :--- | :--- |
| `GET /admin/modules/{id}/day?date=` | `{ date, now, hours: [open, close], resources: [{ id, kind, name, mine, working: [[from, to]...] }], bars: [{ booking_id, resource_id, start, end, service_start, service_end, status, label, clash }], needs_you: [...], counts: {...} }` | `working` is shifts minus breaks minus days off. `needs_you`: pending requests (with hold expiry), clashes, jobs waiting for parts. Capability `jsb_work_bookings` |
| `q` on `GET /admin/modules/{id}/bookings` | code, customer name or phone (digits only compared) | Prefix match on code; `LIKE` on name; limit stays 500 |
| `readiness` on `GET /admin/modules/{id}` | `[{ key, done, label, link }]` | New `src/Data/Readiness.php`. Staff/salon: a service, a staff member with hours, each service someone can do. Restaurant: tables, seating times. Workshop: services, a mechanic with hours, a bay, a supported car. Field: services, a professional with hours, the business location if any service is at the customer's place. All kinds: the module is on, and its shortcode is on a page (`page_url`) |

No schema change. The engine is untouched.

### 4.3 Screens and files

WordPress's menu stays the sidebar: **Booking › Today · Bookings · Modules · Settings · System status**. `admin.php?page=jsb` becomes Today (it was the placeholder), so old links still land in the plugin.

| File | What |
| :--- | :--- |
| `src/Admin/Menu.php` | Pages, capability per page, one mount `<div id="jsb-admin" data-screen>` per page, config (REST root, timezone, today, the user's linked staff ids, capabilities). Settings and System status keep their WordPress forms and take the tokens |
| `assets/admin/admin.css` | The moss ladder, DM Sans, tabular figures, status pills, board, sheet, table |
| `assets/admin/app.js` | Shared: `wp.apiFetch` wrapper, module tabs, finding line, states (empty, loading, error), in-place confirm, keyboard map |
| `assets/admin/today.js` | The day board + the "Needs you" rail; the phone shows one resource at a time |
| `assets/admin/bookings.js` + `detail.js` | The table (status tabs, search, date range, filters), and the side sheet with only the actions the status and kind allow |
| `assets/admin/modules.js` + `setup.js` | Module rows with shortcode copy; the two-column setup with the readiness list, sections by kind, the widget's look settings with a live preview of the 7a widget |

ES modules, no build step, as in 7a.

### 4.4 Interaction thesis (from 1.4, made concrete)

- **Nothing changes before the server answers.** The button shows its pending state, then the row updates in place with a 160 ms tint flash. A refused change (409/422) shows the engine's own sentence beside the control.
- The detail sheet slides in from the right in 220 ms; the list behind stays usable. `Esc` closes it and focus returns to the row.
- `j`/`k` move through bookings, `Enter` opens one. Destructive actions (reject, cancel, delete a module) ask in place, never in a modal.
- The board scrolls to "now" once on load; it never animates bars. Reduced motion: no slide, the sheet just appears.

### 4.5 Checks

1. Integration: the day route per kind (bars per resource, travel padding, working spans with a break and a day off, needs-you), search, readiness per kind, and a linked staff member's view.
2. The skill's `lint.mjs --app` and `inspect.mjs --app` at 1440, 768 and 390, with every screenshot opened; the seven app-screen questions answered in writing for each screen, three rounds at most.
3. Every state seen: a new empty module, loading, an error (server stopped), a zero day.
4. Two roles walked through by keyboard in the built-in browser: the owner, and a stylist linked to a staff member (test user saved to `dev/local-test-site.env`).
5. Engine and integration suites stay green.

### 4.6 Decisions for you

1. **What a staff member linked to one person sees** (phase 3 already limits their *changes* to their own appointments):
   - **Recommended:** the whole day board, so they can see the shop's day, with **their own row first**. Other people's bookings show time, service and the customer's first name only; phone, email and actions appear only on their own.
   - **Strict:** only their own bookings anywhere (the board shows just their row; the list is filtered on the server).
2. **Build order:** **two halves, each checked before the next (recommended):** 7b-1 Today, Bookings and the detail sheet; then 7b-2 Modules and Setup. Or all of 7b at once.

---

## Part 5. 7c scope plan: the marketing page

The design is settled in 1.5 (direction "The sentence is the layout", seed 228969607, sand and moss, DM Sans; the one signature motion is the shutter on the hero slot). This part pins down what gets built and from what.

### 5.1 What it is made of

- **Real photographs:** the project's six Unsplash photos in `localsite/web/img` (salon, spa, restaurant, workshop, home services, welcome), credits already recorded. Used as the world of each kind, never as "our customers".
- **The product's own interface as pictures** (the sheet's rule for software): the real 7a widget and crops of the real 7b day board, as HTML marked `data-ui`, filled from the demo salon day (`tests/seed-demo.php`) and one day per other kind.
- **One moving picture:** an openly licensed clip if the skill's `assets.mjs --video` finds an honest one (a salon or a workshop at work); otherwise a slow drift on the restaurant photograph.
- **No invented proof:** no customers, quotes, logos or star ratings. The only figures are the product's own: five kinds, PHP 7.4+, "1 of 10" from the race test.

### 5.2 The live thing: the real widget in demo mode

The showcase line ("Table for 8, Friday 8pm") feeds **the real 7a widget**, with its `source` swapped for a `DemoSource` that replays **answers recorded from the real engine**: a small script asks the test site's REST routes for each demo scenario (five kinds, a few days each) and saves the JSON beside the page. Same widget code, same rules shown, no server needed. Booking in demo mode ends on a done screen that says "Demo: nothing was booked".

Keywords pick the kind (table, party → restaurant; haircut, colour → salon; brake, car, plate → workshop; at my place, aircon → field); anything else shows the five example buttons.

### 5.3 Sections (blueprints as dealt)

| Section | Claim | Proof |
| :--- | :--- | :--- |
| Hero: title in the sky, line on the horizon | It books the thing your business has | "Book any [slot] online." with the slot cycling five real widget/board crops; the shutter opens it once |
| Showcase: headline you can type | Each kind's rules really work | The typed line drives the demo widget |
| List: statement, table pushed right | Five kinds, each with its rule | Five rows with the kind's photograph |
| How it works: stages as one bar | Minutes to set up | One bar true to time; the day board crop as its end state |
| FAQ: wide heading, indented questions | The worries | Six real answers (WordPress.com free plan, where data lives, the last slot, payments, accounts, uninstall) |
| CTA: the idea inverted | Get it | The headline on the moss field, one button: download |
| Footer: contact as the headline | Talk to us | The contact address large; photo credits small |

### 5.4 Files

| File | What |
| :--- | :--- |
| `marketing/index.html`, `page.css`, `page.js` | The page; no build step, DM Sans from the plugin's own fonts |
| `marketing/demo/*.json` + `justsimple-booking/assets/front/demo-source.js` | Recorded answers and the replaying source (shipped with the plugin, so the widget's `source` hook stays tested) |
| `tests/record-demo.php` | Records the demo answers from the test site |
| `marketing/img/` | The six photos (copied, credited), any clip and its poster |
| `bin/build-zip.php` (if decision 1 is the zip) | Builds `justsimple-booking.zip` without `tests/` for the download button |

### 5.5 Checks

1. The skill's full page gate: `lint.mjs`, then `inspect.mjs` with **0 FAIL at 1440, 768 and 390**, every screenshot opened; the written critique, three rounds at most; the motion floor (at least 8 moving elements); reduced motion.
2. A 5-second first-screen test, written down: what is it, and what do I do?
3. The demo widget: each of the five example lines reaches times and a demo done screen, by keyboard.
4. The download (if built): the zip installs on the test site as a fresh plugin and activates.
5. Engine and integration suites stay green.

### 5.6 Decisions for you

1. **Where the page lives and what "Download" gives.**
   - **Recommended:** a static page in `marketing/` with a build script that makes the plugin zip beside it; the folder can go on any free static host (GitHub Pages, Netlify) later. I also publish it as a private Artifact link so you can review it before it goes anywhere.
   - Or: the page only, as a private Artifact link, with the download button pointing at a WordPress.org listing to come.
2. **The contact address for the footer's headline** (an email you want public). Without one, the footer leads with the download instead.
