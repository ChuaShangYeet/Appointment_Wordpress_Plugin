# Phase 7 plan: the frontends (booking widget, admin screens, marketing page)

Status: **waiting for confirmation**, design first (Birdview step 3). Map: `.birdview/architecture.json` revision 6. Branch `standalone-wp-plugin` (phases 1-6 committed; last commit `376e252`).

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
