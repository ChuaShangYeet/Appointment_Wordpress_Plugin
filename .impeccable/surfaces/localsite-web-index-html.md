---
version: 1
slug: "localsite-web-index-html"
primary_target: "localsite/web/index.html"
related_targets: ["demo/access.py","demo/pages.py"]
---

# Demo website (localsite/web): booking page, My bookings, business admin, sign-up

Mode: Operate. Audience: Malaysian small-business owners evaluating JustSimple (confirmed 2026-10-22); testers second.
Scope: new look plus layout changes, every function and wording kept; the online demo's log-in / register pages match.

## Direction contract

THESIS: The category standard played straight, at the finish level of Cal.com and Amelia (user's benchmarks). Refuses the old tinted-green SaaS card stack with three typefaces and uppercase micro-labels.
OWN-WORLD: Neutral ground #f6f7f6, white surfaces split by 1px #e3e6e4 borders, no resting shadows; one face, Hanken Grotesk, tabular figures; near-black #151917 actions and selections; brand green #157150 only on the mark, the current step, free-day counts, confirmed status and focus. Radii 8 / 6 / pill chips. Outline icons (Lucide paths).
STORY: An owner books as a customer (service, who, time, details, confirm) seeing the business and a live "Your booking" summary beside the steps, then switches to the admin and finds the booking in the list, schedule board and drawer.
FIRST VIEWPORT: White product bar (mark, area nav, Messages, Reset) over a context row (business, role). Booking: 340px business pane (cover, monogram, name, rating, address, live summary) left; the step card right with back arrow plus step bar, step heading, choices, sticky footer with total and the near-black Continue. Phone: stacked, summary folded into the footer.
FORM: Category standard (canon), chosen by the user over the dealt Queue ticket direction; code-led. seed:9cc9cfd6eefa949a
SIGNATURE: Directional step transitions (forward slides in from the right, Back from the left, 240ms ease-out) and the admin drawer arriving from the right / as a bottom sheet on phones; animations play only on arrival, never on the page's frequent re-renders; scale(0.97) press feedback everywhere.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
