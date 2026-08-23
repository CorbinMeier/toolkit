---
name: Desktop Dashboard
description: A tactical, always-on HUD wallpaper for COSMIC/Wayland — system health, weather, and forecast in a notched cyberpunk console.
colors:
  signal-white: "oklch(0.96 0.01 250)"
  console-grey: "oklch(0.70 0.03 255)"
  recessed-grey: "oklch(0.52 0.03 258)"
  deep-console: "oklch(0.19 0.015 260 / 0.88)"
  hairline-grey: "oklch(0.70 0.04 260 / 0.14)"
  signal-teal: "#2be4ea"
  ember-gold: "#fed33f"
  distress-red: "#e8615a"
  signal-teal-dim: "#1f6a6e"
  ember-gold-dim: "#8a6a12"
  distress-red-dim: "#9c3230"
  standby-green: "#2bfea0"
typography:
  title:
    fontFamily: "Fira Mono, ui-monospace, monospace"
    fontSize: "clamp(0.8rem, 2vmin, 1.5rem)"
    fontWeight: 400
    lineHeight: 1.3
  body:
    fontFamily: "Fira Mono, ui-monospace, monospace"
    fontSize: "clamp(0.48rem, 1.02vmin, 0.74rem)"
    fontWeight: 400
    lineHeight: 1.3
    fontFeature: "tnum"
  label:
    fontFamily: "Fira Mono, ui-monospace, monospace"
    fontSize: "clamp(0.44rem, 0.95vmin, 0.8rem)"
    fontWeight: 400
    letterSpacing: "0.1em to 0.3em"
    textTransform: "uppercase"
rounded:
  none: "0px"
  circle: "50%"
spacing:
  xs: "0.6vmin"
  sm: "1.1vmin"
  md: "1.8vmin"
  lg: "2.2vmin"
  xl: "2.4vmin"
components:
  panel:
    backgroundColor: "{colors.deep-console}"
    padding: "1.5vmin 2vmin"
  util-bars-segment:
    shape: "skewed parallelogram (SVG polygon)"
    litSpectrum: "{colors.distress-red} to {colors.ember-gold} to {colors.standby-green}"
    unlitFill: "oklch(0.32 0.02 260 / .45)"
---

# Design System: Desktop Dashboard

## Overview

**Creative North Star: "The Night Ops HUD"**

This is not a wallpaper image — it's a live console rendered as the desktop background: an always-on heads-up display watching CPU, memory, battery, disk, network, and the sky outside, sitting one layer above the desktop background and one layer below every window. The philosophy is tactical and utilitarian: every glowing edge, every uppercase label, every accent color means something specific. Nothing lights up decoratively. Glow is a status signal (a battery actually charging, a metric running hot, a data feed gone stale), not an ambient effect applied for its own sake.

The system is precise and instrumented — icon, label, value, and chart line up in real table columns so a glance reads cleanly, numerals never jitter thanks to tabular-nums everywhere. It's also quiet and restrained at rest: panels sit low-contrast against a dark gradient void, and the only things that actively call for attention are the ones that should (an offline banner, a hot CPU, a charging battery). And it's dense and confident — a huge amount of live, frequently-updating data (CPU/memory/battery utilization, a full partition list, network throughput) packed into roughly half the screen without padding it out or apologizing for the density.

The signature visual device, inherited from the CyberpunkUIKit template this system started from, is the **notch**: every panel's bottom-right corner is diagonally chamfered and capped with a small glowing accent tab, standing in for a border-radius the system otherwise never uses. There is no rounded-card language here — the notch *is* the corner language.

**Key Characteristics:**
- Notched, chamfered-corner panels — never rounded corners — each carrying a single committed accent (teal, gold, or red)
- Flat surfaces at rest; glow (text-shadow / box-shadow used as light emission, never elevation) is reserved for real status
- Dense, grid-aligned metric rows, each grid compact and sized to its own content rather than stretched to fill the panel: every System row (CPU/MEM/BAT included) shares one shape — label → value (+ status bar/chart), with an optional dash-prefixed sub line directly underneath (#57 dropped icons and the separate tail column in favor of Weather's `.kv-grid` conventions — one font size everywhere; #58 settled the sub-line under the row rather than beside it as a third column); the Utilization Bars/mini bar/step chart status visualization is the one thing kept, now attached to the value cell
- Tabular numerals everywhere a value updates, so nothing visually jitters on refresh
- A dark, slowly-drifting ambient gradient background (42s/55s cycles, deliberately desynced) is the only "alive" surface that isn't tied to real data

## Colors

Three committed accents plus a cool, low-saturation neutral scale. Panels never mix accents — the accent is decided once per region (System = teal, Forecast = gold, alerts = red) and every glow, border, and underline in that region reads from the same one.

### Primary
- **Signal Teal** (`#2be4ea`): the System region's accent — CPU/memory/battery/storage/network panel borders, corner-tab glow, section-header underlines, and the default "nominal" tone for every metric chart and icon.

### Secondary
- **Ember Gold** (`#fed33f`): the Forecast region's accent (current conditions, hourly, week, sun & moon) — and doubles system-wide as the "hot/warning" tone once a metric crosses its threshold (CPU/memory/disk > 88%, battery < 20% and unplugged, sun-arc daytime marker).

### Tertiary
- **Distress Red** (`#e8615a`): reserved for things that are actually wrong — the offline banner and its status dot, and the low end of the Utilization Bars spectrum. Never used as a passive accent the way teal and gold are.

### Neutral
- **Signal White** (`oklch(0.96 0.01 250)`): primary text — numeric readouts, temperature, weather description.
- **Console Grey** (`oklch(0.70 0.03 255)`): secondary text — units, sun/moon times, less-emphasized numbers.
- **Recessed Grey** (`oklch(0.52 0.03 258)`): tertiary text — every uppercase label, tier captions, footer stats.
- **Deep Console** (`oklch(0.19 0.015 260 / 0.88)`): the panel fill itself — dark, slightly translucent, blurred behind.
- **Hairline Grey** (`oklch(0.70 0.04 260 / 0.14)`): the only border/divider color that isn't an accent — separates Storage/Network/footer sub-sections within one panel.

### Dim border variants
Each accent carries a desaturated "dim" twin (`#1f6a6e` teal / `#8a6a12` gold / `#9c3230` red) used exclusively for the 2px panel border — the full-saturation accent is reserved for glow, text, and fills; the dim variant is reserved for structural edges.

### Status Bands (Utilization Bars spectrum)
Each Utilization Bars segment is colored off its fixed position in the bar, not the metric's live percentage band: **Distress Red** (`#e8615a`) at the low end, through **Ember Gold** (`#fed33f`) at the midpoint, to **Standby Green** (`#2bfea0`) at the high end (see the Utilization Bars component, #31). This replaced the earlier CPU/MEM status LED, which banded its single dot's hue off the metric's own percentage (green below 70%, gold 70–89%, red from 90%, #17) — the LED read as too heavy and was retired.

### Named Rules
**The One Accent Per Region Rule.** Every panel commits to exactly one accent — border, corner-tab glow, and section-header underline all read from the same `--panel-accent`/`--panel-border` pair. Accents never mix within a single panel.

**The Status-Only Glow Rule.** Glow (box-shadow-as-light, text-shadow) never decorates a resting element. It appears only when something is true: a metric is hot, the battery is actually charging, the feed is offline. A panel's corner tab and section-header underline are the one standing exception — they're the system's constant "this region is live" signature, not a status readout.

## Typography

**Font:** Fira Mono, with `ui-monospace, monospace` fallback — the single voice for every role, every theme (#50). Previously Fira Sans was the base body font and Fira Mono was reserved for the two CRT themes only (`retro_terminal`/`retro_orange`); monospace is now universal, reinforcing the "instrumented console / spreadsheet of live data" read the system has always gone for, rather than being a CRT-only costume.

**Character:** One typeface doing every job, kept legible at wallpaper viewing distance. Fira Mono ships only Regular (400) and Bold (700) as static faces on this machine — there is no thin/light weight to lean on, so hierarchy comes from size, color, tracking, and (sparingly) bold, not a wide weight range. No serif, no display face; the system deliberately doesn't reach for a second typeface to signal "brand."

### Hierarchy
- **Title** (weight 400, `clamp(0.8rem, 2vmin, 1.5rem)`): the weather description line ("Partly Cloudy"); a similar-weight variant handles the week list's day names and hi/lo range.
- **Body** (weight 400, tabular-nums, `clamp(0.48rem, 1.02vmin, 0.74rem)` scaling up to `~1rem` for hourly temps): every live metric readout — system-table values, hourly temps, footer stats. This is the workhorse size; most of the screen's text lives here.
- **Label** (weight 400, uppercase, tracking 0.1em–0.3em, `clamp(0.4rem, 0.82vmin, 0.8rem)`): section headers ("System", "Storage", "Network", "Weather"), table row labels (CPU/MEM/BAT/NET, tier captions like "30S"/"5M"), and every key column in a `.kv-grid` (see Layout). Wider tracking (0.3em) marks a section header; tighter tracking (0.1em) marks a row label — the tracking width itself signals the hierarchy level.

A **Display** role (weight 100, `clamp(2.4rem, 8vmin, 6.5rem)`, a single hero-sized current-temperature readout) previously existed in this system but is retired — the Weather panel dropped the oversized hero number in favor of uniform rows (#40/#41, formalized into `.kv-grid` at #50); nothing in the current markup uses it. Don't resurrect it without a real design decision to do so.

### Named Rules
**The Tabular Numerals Rule.** Every element carrying a value that updates on a poll gets `font-variant-numeric: tabular-nums`. A refreshing CPU percentage or download rate never shifts its neighbors horizontally.

## Layout

The page is a single fixed viewport (`overflow: hidden` on `html`/`body` — this is a kiosk surface, never a scrolling one) sized in `vmin` units throughout rather than fixed pixels, so the same proportions hold whether the surface renders at 1440×900 or 2560×1080. The one hard breakpoint is Tailwind's `lg` (1024px): below it the two regions stack in a single column; at or above it they sit side by side as an exact 50/50 grid (`grid-cols-1 lg:grid-cols-2`).

Page padding is `2.2vmin` on the sides, and top/bottom padding adds a configurable safe-area inset (`--safe-top`/`--safe-bottom`, sourced from `config.json`, default 48px) so content never renders under the user's own desktop panels. The grid gap between the two halves is `1.8vmin`.

Within each half, panels are content-sized, not stretched: rows pack toward the top and leftover vertical space below them is left unfilled, deliberately. A panel never distributes its rows to fill the available height. Related but distinct data within one panel (System's cpu/mem/bat rows vs. its Storage grid vs. its Network grid; Forecast's current-conditions vs. Hourly vs. Week vs. Sun & Moon) is separated by a `1px` hairline border-top with a small heading, not by breaking into a second panel card — one notched panel per region, internally divided.

Every metric grid is compact, not stretched (#17): none carries `w-full` — each sizes to its own content instead of filling the panel, so a narrow value or a small right-aligned chart doesn't leave a dead gap between itself and its column boundary. Every System sub-panel now shares one `.sys-grid` shape (label / value+status-bar / sub, all auto-sized to content, #57) — CPU/MEM/BAT's table used to diverge first with a leading status LED column (#17, retired #31), then an icon column (retired #57); all three System sub-panels share the same plain grid shape now.

### Key:Value Grid (`.kv-grid`, #50)

A "spreadsheet" pattern for any panel whose content is fundamentally a list of named readings: a two-column CSS grid (`grid-template-columns: auto 1fr`), key column left (uppercase Label styling, left-aligned), value column right (Body styling, right-aligned, `.num` for tabular figures). Every row shares the same grid, so the value column starts at one consistent x position from the first row to the last — the thing that makes it read as a spreadsheet rather than a stack of independently-laid-out lines.

The Weather/Forecast panel is the first and, so far, only user: Condition, Temp, Feels, High/Low, Wind, Humidity, Rain, and UV are all one `.kv-grid`, replacing an earlier layout that paired unrelated values into a 2×2 table (description next to feels-like, temp next to high/low) above a separately-laid-out flex list for Wind/Humidity/Rain/UV — two different layout mechanisms whose value columns didn't align with each other.

System now expresses the same key-left/value-right idea through its own `.sys-grid` (#57, see Components below) rather than moving to `.kv-grid` itself — a two-column grid (label | value + status bar/chart) carrying a status visualization `.kv-grid`'s plain two-column model has no slot for, plus an optional sub line pinned directly under the value column (`grid-column:2`, #58) for what used to be CPU/MEM/BAT's stacked second line. It shares every other convention `.kv-grid` established: no icons, one font size for every cell. Music still uses its own bespoke layout (cover art + title/artist/progress), not a row grid at all. Calendar stays a literal month grid; it isn't a list of named readings and reorganizing it into key:value rows wouldn't make sense. (Devices used to share System's old icon-led row shape too, but the panel was removed entirely — #56.)

## Themes

The system ships two selectable visual themes, toggled via `config.json`'s
`display.theme` field (`"night_ops"` | `"retro_terminal"`, ISSUES.md #32) and
applied by `app.js`'s `apply()` as `data-theme` on `<html>`. A theme is
*only* a palette swap plus fx-layer content — no component's markup or
shape branches on which theme is active; every component reads color
exclusively through the `--color-*`/`--panel-accent`/`--panel-border`
custom properties, so overriding those under `html[data-theme="..."]` in
`web/index.html` is sufficient to reskin the whole page. The toggle surface
is deliberately just a config field for now — the natural future home is
the Control Backend (#30) once it exists, but implementation doesn't block
on that.

### Night Ops HUD (default)
The system described everywhere else in this document — teal/gold/red
accent triad, Fira Mono, backdrop blur, drifting ambient gradient glow.

### Retro Terminal
A CRT/green-phosphor console look. Same panel shapes, table layout, and
component structure as Night Ops HUD — only the palette and surface
treatment change:
- **Palette:** every accent (teal/gold/red) converges on one phosphor
  green (`#39ff88`) plus a near-black green-tinted panel fill, so
  `accent-cyan`/`accent-amber`/`accent-crimson` regions read as a single
  monochrome instrument instead of the tri-color HUD — matching the
  reference aesthetic of a single-color terminal, not a stylistic
  shortcut.
- **Typography:** unchanged — Fira Mono is now the base token set's font
  for every theme (#50), not a Retro Terminal-only switch.
- **Surface:** panel `backdrop-filter` blur is turned off (flat, not
  glassy — closer to a real terminal) and the ambient drifting glow blobs
  (`.glow`, `.g1`/`.g2`) are hidden — that ambient cyberpunk lighting is a
  Night Ops HUD-specific device, not a generic background treatment.
- **CRT fx overlay:** a scanline + vignette layer with an occasional
  subtle flicker (`#crtfx`, see Rendering Layers below), respecting
  `prefers-reduced-motion`.

## Rendering Layers

Components render on one of a small, fixed set of z-order layers (ISSUES.md
#32) rather than picking ad-hoc `z-index` values — declared once as CSS
custom properties (`--z-bg`, `--z-content`, `--z-fx`, `--z-banner`,
`--z-boot`) in `web/index.html`'s `:root` block:

| Layer | Token | Holds |
|---|---|---|
| Background | `--z-bg` (-1) | The drifting ambient glow blobs (`.glow`) |
| Content | `--z-content` (0) | `#root` — every panel, the System/Forecast structure |
| FX | `--z-fx` (30) | Theme-driven overlay effects, independent of panel content — today, Retro Terminal's `#crtfx` scanline/vignette/flicker overlay |
| Banner | `--z-banner` (40) | The offline banner |
| Boot | `--z-boot` (50) | The startup overlay, dismissed on first successful render |

The FX layer is the reason this exists as a named mechanism rather than a
single extra `div`: a CRT-style overlay needs to sit **above** panel
content (so scanlines cross it) but **below** the offline banner and boot
overlay (so a real alert is never dimmed by a decorative effect), and it
needs to do so independently of which theme is active — the layer is
theme-agnostic infrastructure; only its content (`#crtfx`'s background/
animation) is theme-conditional. A future effect that needs the same
"above content, below alerts" placement reuses the FX layer rather than
inventing another `z-index`.

## Elevation & Depth

Flat by design — there is no drop-shadow-based elevation anywhere in the system. Depth comes from two mechanisms instead: `backdrop-filter: blur(6px)` on every panel (rising to `blur(18px) saturate(1.3)` if the wallpaper is running in transparent mode over a photo background), and layered ambient radial/linear gradients in the page background that drift slowly (`42s`/`55s`, deliberately different periods so the two glow blobs never fall into a visible sync). `box-shadow` and `text-shadow` exist in the system, but they're never used to lift a surface — only to make an accent-colored edge or piece of text glow, which is a status signal, not a depth cue (see Colors' Status-Only Glow Rule).

### Named Rules
**The Flat-By-Default Rule.** No element receives a drop shadow for hierarchy. If something needs to stand out, it gets an accent-colored glow (glow = "this is live/important"), not a lift (lift = "this is above the surface").

## Shapes

The corner is the system's one recurring geometric signature: every panel is `clip-path`-cut from a rectangle into a pentagon, chamfering the bottom-right corner at a fixed `1.15rem` diagonal, with a small glowing accent-colored tab (`1.55rem × 3px`, rotated −45°) sitting in the resulting notch. Panel borders are a flat `2px` solid line in the region's dim accent color — no border-radius anywhere on a panel.

Outside the panel level, corner treatment drops to two simple cases: a perfect circle (the `0.55em` status dot on the offline banner), and everything else — including the Utilization Bars' angled parallelogram segments — sharply notched or angular. There is no intermediate rounded-rectangle vocabulary (no `8px`/`16px` card-radius family, no barely-rounded badge) — a shape is either sharply cut or a perfect circle, with nothing in between.

## Components

Every surface in this system is read-only — there are no buttons, form inputs, or navigation, since the dashboard has no interactive affordances at all. The components below are the actual repeating primitives: the panel container, the metric row, the utilization bars, the compact tier graph, the step-line chart, the section header, and the status dot/banner pairing.

### Panel (Notched Container)
- **Shape:** pentagon `clip-path` chamfering the bottom-right corner (`1.15rem` cut), `2px` solid border in the region's dim accent color, `backdrop-filter: blur(6px)`.
- **Fill:** Deep Console (`oklch(0.19 0.015 260 / 0.88)`), constant across every accent region — only the border and corner tab change color.
- **Corner tab:** a `1.55rem × 3px` bar in the full-saturation accent, glowing (`box-shadow: 0 0 6px`), rotated into the notch. This is the panel's one constant "alive" signal — present at rest, not tied to a status condition.
- **Padding:** `1.5–2.2vmin` depending on region density (System's panel is tighter than Forecast's).

### Metric Row (`.sys-grid` Row, #57)
- **Shape:** one `.sys-grid` row — uppercase label cell (left), value cell (right, tabular-nums, the row's status bar/chart attached inline), sub cell (right, dash-prefixed, Recessed-Grey). No icon and no separate tail column — both retired at #57 in favor of Weather's `.kv-grid` conventions (one font size for every cell, no stacked second line). CPU/MEM/BAT used to diverge with a leading LED cell (#17), then an icon cell (#31); both are gone now.
- **Alert:** a row's value takes the same filled white-on-red badge Weather uses (see Weather Value Alert Badge, #49) once it crosses its hot threshold (CPU/MEM/Storage > 88%, Battery ≤ 35% unplugged) — this replaced the retired icon-tone signal (Signal Teal nominal, Ember Gold once past threshold) as the row's status alert. The status bar itself (Utilization Bars, mini fill bar, step chart) is unrelated and keeps its own independent coloring regardless of the badge.

### Utilization Bars
- **Shape:** a row of ten slanted parallelogram segments (SVG `<polygon>`, sharing the leaning-right skew of the notch/corner-tab language) attached to the value cell of every CPU/MEM/BAT row, replacing the retired status LED (#31 — user feedback: the LED "wasn't working out"). Sized to sit inline with the row, same treatment Storage/Network use for their chart/badge.
- **Fill count:** `round(pct / 100 * 10)` segments read as lit; the rest sit unlit.
- **Color:** each lit segment takes a fixed spectrum color by its position in the bar — Distress Red at the low end, through Ember Gold, to Standby Green at the high end (see Status Bands under Colors) — not the metric's own live percentage. A half-full bar always shows the same red-to-gold gradient regardless of which metric it belongs to; only the *count* of lit segments encodes the percentage. Unlit segments are a flat, low-opacity Hairline-adjacent grey (`oklch(0.32 0.02 260 / .45)`), reading as the empty track.
- Not interactive — a read-only status indicator, same as everything else in this system.

### Step-Line Chart
- **Style:** a "staircase" (step-after) SVG path, `stroke-width: 2`, rounded caps/joins, `preserveAspectRatio="none"` so it fills its cell exactly — deliberately not a smoothed/interpolated line, since the underlying data is polled samples, not a continuous signal.
- **Fill:** an optional area fill under the line, a vertical gradient in the row's own tone (`.35` opacity at the line, fading to `0` at the baseline), for more visual presence at wallpaper viewing distance than a bare stroke. Currently used nowhere in the shipped UI (CPU/MEM's filled Trend Graph, the component that used it, was removed — #34, not wanted on the wallpaper; metrics collection continues server-side) but the `stepChart(..., fill: true)` option stays for a future consumer.
- **Multi-series:** the Network chart plots two unfilled step lines (download in Signal Teal, upload in Ember Gold) on one shared scale, rather than two independently-scaled charts — the System region's one live use of this component today.

### Section Header
- **Style:** Recessed Grey, uppercase, `0.3em` letter-spacing, with a full-width `1px` accent-colored underline that glows (`box-shadow: 0 0 6px`) — the same accent-glow language as the panel corner tab, applied to text instead of a shape.

### Status Dot + Offline Banner
- **Dot:** a `0.55em` solid circle in the banner's accent color, `50%` border-radius, sitting inline before the message text.
- **Banner:** a small notched panel (same pentagon `clip-path` as every other panel) in the Distress Red accent, fixed to the top-center of the viewport, hidden by default and shown only after two consecutive failed polls — deliberately debounced so a single dropped request never flashes the banner.

### Auto-Cycle Reveal
- **Problem:** the desktop surface has no pointer or keyboard reach — it sits below every window — so a panel whose content outgrows its box has no scrollbar a user could ever grab (ISSUES.md #29).
- **Mechanism:** opt in per-element with `class="auto-cycle"` plus a CSS height/max-height so it can actually overflow (`overflow-y:hidden` — no visible scrollbar, since it's never user-driven). `app.js`'s `refreshAutoCycles()` (called every `apply()`) scrolls it slowly to the bottom, dwells, resets to the top, and repeats; an element that already fits its box is left alone.
- **Status:** general-purpose only — no shipped panel needs it yet. Future components (networked devices, log highlighter, tasks, ...) adopt it as they're built rather than reinventing their own reveal behavior.

## Do's and Don'ts

### Do:
- **Do** give every panel exactly one accent (border + corner-tab glow + section-header underline all match) — see the One Accent Per Region Rule.
- **Do** use `font-variant-numeric: tabular-nums` on any text that holds a value which updates on a poll.
- **Do** let a panel's content determine its height; leave unused vertical space rather than stretching rows to fill it.
- **Do** reserve glow (box-shadow-as-light, text-shadow) for a real status condition — hot, charging, offline, or the panel corner tab's constant "this region is live" signal.
- **Do** keep every System `.sys-grid` shape identical (label / value+status-bar / sub) so their columns line up — CPU/MEM/BAT, Storage, and Network all share it (#57).
- **Do** size every metric grid to its own content, not its parent panel (no `w-full`) — a narrow value or a small chart shouldn't leave a dead gap between itself and its column boundary.
- **Do** reserve icons for cases that genuinely need a pictorial identity (there are none left in System or Weather, #41/#57) — a label in one consistent font already identifies a row; an icon column just adds a column to keep aligned for no informational gain.

### Don't:
- **Don't** use large rounded corners or a soft-card look anywhere. The notch (pentagon `clip-path`, `1.15rem` chamfer) is this system's entire corner language; a `border-radius: 8px+` card would read as a different design system grafted on.
- **Don't** add a drop shadow for hierarchy or lift. This system has no elevation model — depth comes from blur and ambient background layering only (see the Flat-By-Default Rule).
- **Don't** mix two accents on one panel, or apply an accent color to an element outside its region.
- **Don't** add an interactive affordance (button, input, hover-driven control) — this is a read-only, no-input wallpaper surface. If a status element (like the Utilization Bars) looks like it could be a button, that's a bug, not an invitation to wire up a click handler.
