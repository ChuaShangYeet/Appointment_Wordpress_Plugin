---
name: JustSimple Booking Demo
description: The category-standard booking page and business admin, finished to Cal.com level, on the real JustSimple backend.
colors:
  ground: "#f6f7f6"
  surface: "#ffffff"
  raised: "#f2f4f3"
  active: "#e9ecea"
  border: "#e3e6e4"
  rule: "#edf0ee"
  control: "#cfd4d1"
  control-hover: "#a9b1ad"
  text: "#121614"
  muted: "#59625e"
  faint: "#6a726e"
  primary: "#151917"
  primary-hover: "#2b302d"
  on-primary: "#ffffff"
  accent: "#157150"
  accent-ink: "#0f5a3f"
  accent-soft: "#e3f2eb"
  ok: "#1f7a4a"
  ok-soft: "#e1f4e8"
  ok-ink: "#14532f"
  warn: "#b7791f"
  warn-soft: "#fdf3dc"
  warn-ink: "#7a4a07"
  warn-line: "#f2d28b"
  bad: "#c4242a"
  bad-soft: "#fdecea"
  bad-ink: "#8a1c20"
  info: "#2f5f9e"
  info-soft: "#e8f0fa"
  info-ink: "#1f426f"
typography:
  display:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "clamp(1.7rem, 3.4vw, 2.3rem)"
    fontWeight: 600
    lineHeight: 1.15
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "1.6rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  title:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  title-sm:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.005em"
  body:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "0.87rem"
    fontWeight: 500
    lineHeight: 1.55
  caption:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "0.8rem"
    fontWeight: 500
    lineHeight: 1.55
  figure:
    fontFamily: "Hanken Grotesk, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "1.3rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.01em"
    fontFeature: "tnum"
  code:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.45
rounded:
  block: "5px"
  control: "6px"
  card: "8px"
  panel: "10px"
  sheet: "14px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  2xl: "28px"
  3xl: "40px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.control}"
    padding: "0 16px"
    height: "42px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-primary-disabled:
    backgroundColor: "{colors.active}"
    textColor: "{colors.muted}"
  button-ghost:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "0 16px"
    height: "42px"
  button-ghost-hover:
    backgroundColor: "{colors.raised}"
  button-danger:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.bad-ink}"
    rounded: "{rounded.control}"
  button-danger-hover:
    backgroundColor: "{colors.bad-soft}"
  button-small:
    padding: "0 12px"
    height: "34px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "9px 12px"
    height: "42px"
  card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.card}"
    padding: "20px"
  card-flat:
    backgroundColor: "{colors.raised}"
    rounded: "{rounded.card}"
    padding: "20px"
  choice:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.card}"
    padding: "15px 18px"
  slot:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "9px 6px"
    height: "44px"
  slot-selected:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
  day:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.card}"
    width: "72px"
  day-selected:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
  day-closed:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.faint}"
  chip:
    backgroundColor: "{colors.active}"
    textColor: "{colors.muted}"
    rounded: "{rounded.pill}"
    padding: "2px 9px"
  chip-confirmed:
    backgroundColor: "{colors.ok-soft}"
    textColor: "{colors.ok-ink}"
  chip-pending:
    backgroundColor: "{colors.warn-soft}"
    textColor: "{colors.warn-ink}"
  chip-in-progress:
    backgroundColor: "{colors.info-soft}"
    textColor: "{colors.info-ink}"
  chip-cancelled:
    backgroundColor: "{colors.bad-soft}"
    textColor: "{colors.bad-ink}"
  nav-item:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.control}"
    padding: "7px 12px"
  nav-item-current:
    backgroundColor: "{colors.active}"
    textColor: "{colors.text}"
  toast:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.card}"
    padding: "11px 18px"
---

# Design System: JustSimple Booking Demo

## Overview

**Creative North Star: "The Finished Counter"**

The category standard played straight. A booking page a salon owner would hand to their own customers without apology, and an admin that reads like a working tool: the same calm finish as Cal.com or Amelia, nothing borrowed from a startup landing page. The system wins on restraint and exactness: one face, one near-black ink for every action and selection, white surfaces separated by hairline borders, and a single brand green that appears only where something is meant to be noticed.

Density is product density, not marketing density. Body text sits at 15px, controls are 42px tall (44px on time slots and phone controls), and spacing steps in 4px units. Pages re-render their markup on every state change, so motion is reserved for arrivals (a new step, the drawer, the Messages panel) and press feedback; nothing animates on an ordinary re-render.

Light theme only. The old direction this replaced (a tinted-green SaaS card stack with three typefaces and uppercase micro-labels) is the confirmed anti-reference.

**Key Characteristics:**
- Neutral grey-green ground, white surfaces, 1px borders, no resting shadows.
- Hanken Grotesk for everything, tabular figures for times, prices and counts.
- Near-black (#151917 family) carries every primary action and every selection.
- Brand green is a signal, never a fill for actions.
- Outline icons from one set (Lucide paths, 1.75 stroke), drawn at text size in text colour.
- Motion only on arrival; scale(0.97) press feedback on everything pressable.

## Colors

A near-neutral palette with a faint green cast, one near-black ink for action, one brand green for signal, and four status families that appear only on status.

### Primary
- **Counter Ink** (primary): Every primary button, the selected day and time slot, the selected choice's 2px outline, the active tab underline, done step bars, the count badge on Messages, and the toast. Hovers to **Lifted Ink** (primary-hover). Text on it is pure white (on-primary).

### Secondary
- **JustSimple Green** (accent): The brand mark, the business monogram, the current step's bar, the confirmed booking ticket band, the passed human check, meter fills and the 2px focus outline. Its darker **Deep Green** (accent-ink) carries links, "N free" counts under each day, and confirmed schedule blocks' text; **Mint Wash** (accent-soft) fills confirmed schedule blocks and text selection.

### Tertiary (status families)
- **Confirmed** (ok, ok-soft, ok-ink): confirmed chips and success notices.
- **Pending / attention** (warn, warn-soft, warn-ink, warn-line): pending chips and dashed pending blocks, the attention counts in the admin, warning notices, the "near limit" meter.
- **Cancelled / error** (bad, bad-soft, bad-ink): cancelled, rejected and no-show chips, error text, invalid field borders, the danger button, the now-line on the schedule board, the "full" meter.
- **In progress** (info, info-soft, info-ink): in-progress chips and schedule blocks only.

### Neutral
- **Shop Floor** (ground): the page background behind everything.
- **Paper** (surface): cards, the step card, product bar, inputs, choices, slots.
- **Shelf** (raised): flat cards, info notices, ghost hover, closed days, completed blocks, code boxes.
- **Pressed** (active): current nav item, segmented-control track, disabled primary button, unfilled step bars and meters, neutral chips.
- **Hairline** (border) and **Inner Rule** (rule): border separates surfaces; rule divides rows inside one surface (list rows, summary total, board rows).
- **Control Edge** (control): input, ghost-button and back-button strokes; darkens to **Control Hover** (control-hover) on hoverable choices, slots and days.
- **Ink Text** (text), **Quiet Text** (muted), **Faint Text** (faint): body, secondary, and tertiary/placeholder text.

### Named Rules
**The Ink Decides Rule.** Actions and selections are near-black. A primary button, a picked slot, a picked day and a picked choice all use the primary ink; none of them is ever green.

**The Green Is a Signal Rule.** Brand green appears only on the mark and monogram, the current step, free-day counts, confirmed status, the passed human check, meter fills, links and focus. If green would mean "press here", it is wrong.

**The Status Stays on Status Rule.** Warn, bad, ok and info colours appear only on status chips, schedule blocks, notices, meters, error text and the danger button; never as decoration.

## Typography

**Display Font:** Hanken Grotesk (with system-ui, -apple-system, Segoe UI, Roboto, sans-serif)
**Body Font:** Hanken Grotesk (same stack)
**Label/Mono Font:** JetBrains Mono, only for API keys and code samples on the online demo's own pages

**Character:** One humanist grotesk at four weights (400, 500, 600, 700) does all the work; hierarchy comes from weight and size, never from a second display face or uppercase tracking.

### Hierarchy
- **Display** (600, clamp(1.7rem, 3.4vw, 2.3rem), 1.15, -0.02em): the online demo's log-in / register and info page titles only.
- **Headline** (600, 1.6rem, 1.2, -0.015em; 1.4rem under 520px): page titles. The business name in the storefront runs at 1.45rem (1.3rem on phones).
- **Title** (600, 1.25rem, 1.2): step headings ("When?") and section headings; the confirmation ticket heading at 1.4rem.
- **Title Small** (600, 1rem, 1.2, -0.005em): card headings, choice names.
- **Body** (400, 15px, 1.55): all running text; inputs at 16px to prevent phone zoom.
- **Label** (500, 0.87rem, muted, sentence case): field and group labels ("History", "Update status", "How many people?").
- **Caption** (500, 0.74 to 0.8rem): step names, day names, slot sub-lines, board hour ticks, the product-bar tag.
- **Figure** (600, 1.3rem, tabular, -0.01em): the date numeral in the day strip; times, prices, totals and counts all use tabular figures at their local size.

### Named Rules
**The One Face Rule.** Hanken Grotesk sets every word on the booking page, My bookings, the admin and sign-up. The only second face is JetBrains Mono for literal keys and code on the online demo pages.

**The Tabular Figures Rule.** Anything a person compares in a column (times, prices, totals, counts, references, dates) uses `font-variant-numeric: tabular-nums`.

**The Sentence Case Rule.** Labels are sentence case at 500 weight in the muted colour. No uppercase, letter-spaced micro-labels.

## Layout

A sticky white product bar (max 1240px inner width, 60px minimum height) holds the mark, the area nav, Messages and Reset; a second context row under a rule holds the business and role pickers. Below 960px the nav and controls fold into a menu panel with 44px targets.

Page frames are 28px top, 20px sides, 72px bottom (18 / 16 / 64 under 520px), in three widths: narrow (620px) for My bookings and sign-up, the booker (1080px), and wide (1200px) for the admin.

The booker is a two-column grid from 900px: a 340px sticky business pane (4:3 cover, overlapping monogram, name, rating, address, live "Your booking" summary) beside a white step card (10px radius, 28px 32px padding) whose footer bar sticks to the bottom with the total and the Continue button. Under 900px everything stacks and the step card loses its frame; under 520px inactive step names collapse to bars and the current one widens.

The admin is a list (or 780px-minimum schedule board) with a 340 to 420px drawer column that opens on the right; at 860px and below the drawer becomes a bottom sheet over a 36% ink scrim.

Spacing steps on a 4px base: 4, 8, 12, 16, 20, 28, 40, with 10, 14, 18, 22 and 24 used inside components for optical fit. Stacks use 12px (tight) and 22px (loose) gaps.

## Elevation & Depth

Flat by default. Depth comes from the ground/surface split and 1px borders; rows inside a surface are divided by the lighter rule. Shadows exist only for things that float above the page, plus two small state cues.

### Shadow Vocabulary
- **Float** (`box-shadow: 0 1px 2px rgb(18 22 20 / 6%), 0 12px 32px -6px rgb(18 22 20 / 18%)`): the Messages panel and the toast only.
- **Segment lift** (`box-shadow: 0 1px 2px rgb(18 22 20 / 12%)`): the selected button in a segmented control.
- **Focus halo** (`box-shadow: 0 0 0 3px rgb(18 22 20 / 10%)`): text inputs on focus, with the border turning primary; red-tinted at 18% bad on an invalid field.
- **Selection ring** (`box-shadow: inset 0 0 0 1px var(--primary)`): a selected choice, doubling its border to 2px.

### Named Rules
**The Float-Only Shadow Rule.** A surface that sits in the page flow has no shadow at rest. Only the Messages panel and the toast carry the float shadow.

**The Border Not Shadow Rule.** Separate surfaces with the 1px hairline; separate rows inside a surface with the inner rule.

## Shapes

Gently rounded, never soft. Controls (buttons, inputs, slots, nav items, back and close buttons) use the 6px control radius; cards, choices, day tiles, notices, the list and the board use the 8px card radius; the step card, Messages panel, confirmation ticket and drawer use the 10px panel radius, as do the online demo's cards. The phone bottom sheet rounds its top corners at 14px. Chips and attention filters are full pills. Schedule blocks are 5px. The mark is a 28px rounded square (7px), and the business monogram a 54px tile at 10px with a 3px white ring where it overlaps the cover.

Pending schedule blocks use a dashed warn border; private notes use a dashed control border. The travel block is a 45-degree stripe of shelf and paper.

## Components

### Buttons
Quiet, solid and decisive.
- **Shape:** gently rounded (6px), 42px tall, 600 weight at 0.95rem, 8px icon gap.
- **Primary:** counter ink with white text, padding 0 16px; hover to lifted ink; disabled goes to the pressed grey with muted text.
- **Ghost:** paper with a control-edge stroke and ink text; hover to shelf.
- **Danger:** paper with bad-ink text and a 35% bad stroke; hover to bad-soft.
- **Small:** 34px tall, padding 0 12px, 0.87rem. **Link:** deep-green underlined text, 1px underline thickening to 2px on hover.
- **Press:** every pressable thing scales to 0.97 on `:active` (choices and list rows to 0.99) over 160ms with the ease-out curve; hover rules apply only under `(hover: hover) and (pointer: fine)`.
- **Busy:** a 14px currentColor spinner with the action's present-participle label ("Sending…").

### Chips
- **Style:** full pill, 2px 9px, 0.78rem at 500, with a 6px leading dot in the current colour.
- **State:** one per booking status: confirmed (ok family), pending and waiting for parts (warn), in progress (info), cancelled / rejected / no-show (bad), completed (neutral). A flag chip turns its dot into a warn diamond.

### Cards / Containers
- **Corner Style:** 8px (cards), 10px (step card, ticket, drawer, Messages panel).
- **Background:** paper; the flat variant is shelf with no border.
- **Shadow Strategy:** none at rest (see Elevation & Depth).
- **Border:** 1px hairline.
- **Internal Padding:** 20px (16px under 520px); drawer cards 22px.

### Inputs / Fields
- **Style:** paper, 1px control-edge stroke, 6px radius, 9px 12px padding, 42px tall, 16px text. Selects carry an outline chevron. Labels sit above at 500, 0.92rem, with "(optional)" in muted 400.
- **Focus:** border turns counter ink plus a 3px 10% ink halo; non-input focus is a 2px green outline offset 2px.
- **Error / Disabled:** invalid fields take the bad border and a red halo, with bad-ink error text at 0.87rem 500 below.

### Navigation
- **Product bar:** white, sticky, hairline bottom border. Nav items are muted 500 text at 0.94rem in 7px 12px pills of 6px radius; the current item sits on pressed grey in ink; hover shows shelf. Messages and Reset are 36px outline buttons with muted icons; Messages carries an ink count badge.
- **Tabs (My bookings, demo log-in):** muted 500 text over a hairline; the current tab is ink 600 with a 2px ink underline.
- **Segmented control:** pressed-grey track at 8px radius, 3px inset; the selected segment is paper with the segment lift.
- **Mobile:** below 960px a menu button opens a stacked panel with 44px rows.

### Step Bar (signature)
A row of equal 3px bars with the step name beneath at 0.8rem: done steps are ink, the current step is brand green with its name in ink 600, upcoming steps are pressed grey with faint names. A 36px back arrow sits to its left. Steps enter from the right going forward and from the left going back (14px slide with fade, 240ms, `cubic-bezier(0.23, 1, 0.32, 1)`), only on the render that follows a step change.

### Day Strip and Time Slots (signature)
72px day tiles (8px radius): weekday caption, a tabular 1.3rem numeral, and a deep-green "N free" count; closed and full days drop to shelf with a struck-through faint numeral. Slots are a fill grid of 100px-minimum, 44px-tall buttons (6px radius) with tabular 600 times. Selection on either is solid counter ink with white text and 78% white secondary text.

### Choice Rows
Full-width paper buttons (8px radius, 15px 18px padding): name and price on the first line, muted description and icon-led facts below. Hover darkens the border to control-hover; selection adds the ink selection ring.

### Booking Drawer and Schedule Board
The drawer opens in the right column (16px slide, 260ms, `cubic-bezier(0.32, 0.72, 0, 1)`) and becomes a bottom sheet rising 100% on phones (320ms, same curve). The board is a paper surface with 168px resource names, rule-coloured hour lines, status-coloured blocks and a 2px red now-line; hovering a block draws a 1px ink ring outside a 2px paper gap.

### Confirmation Ticket
A 10px-radius paper panel with a brand-green band (warn-soft when the booking awaits approval) holding the heading and a letter-spaced tabular reference, then the summary rows below.

## Do's and Don'ts

### Do:
- **Do** use counter ink (#151917) for every primary action and every selected day, slot, choice, tab and segment.
- **Do** keep brand green (#157150) to the mark, the monogram, the current step, free counts, confirmed status, meter fills, links (as #0f5a3f) and the focus outline.
- **Do** separate surfaces with a 1px #e3e6e4 border and rows inside them with the #edf0ee rule.
- **Do** set times, prices, totals, counts and references in tabular figures.
- **Do** draw icons from the one outline set (Lucide paths, 1.75 stroke, 1em, currentColor) via the shared icon helper.
- **Do** animate only arrivals (step change, drawer, Messages) and gate them so a re-render never replays them; fall back to a 160ms fade under reduced motion.
- **Do** give everything pressable the scale(0.97) press and keep hover styles behind `(hover: hover) and (pointer: fine)`.
- **Do** keep controls at 42px and touch targets at 44px on phones.

### Don't:
- **Don't** fill a primary button or a selection with brand green.
- **Don't** put a shadow on a card, choice, list or step card at rest; the float shadow belongs to the Messages panel and the toast.
- **Don't** add a second typeface to the website; JetBrains Mono is for literal keys and code on the online demo pages only.
- **Don't** use uppercase, letter-spaced micro-labels or tinted-green card stacks.
- **Don't** use status colours for decoration.
- **Don't** add a dark theme; the product is light only.
- **Don't** use text glyphs or emoji as icons.
