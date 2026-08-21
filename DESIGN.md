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
  display:
    fontFamily: "Fira Sans, system-ui, sans-serif"
    fontSize: "clamp(2.4rem, 8vmin, 6.5rem)"
    fontWeight: 100
    lineHeight: 0.85
    letterSpacing: "-0.04em"
  headline:
    fontFamily: "Fira Sans, system-ui, sans-serif"
    fontSize: "clamp(0.9rem, 2.3vmin, 1.7rem)"
    fontWeight: 300
    lineHeight: 1.2
  title:
    fontFamily: "Fira Sans, system-ui, sans-serif"
    fontSize: "clamp(0.8rem, 2vmin, 1.5rem)"
    fontWeight: 300
    lineHeight: 1.3
  body:
    fontFamily: "Fira Sans, system-ui, sans-serif"
    fontSize: "clamp(0.48rem, 1.02vmin, 0.74rem)"
    fontWeight: 400
    lineHeight: 1.3
    fontFeature: "tnum"
  label:
    fontFamily: "Fira Sans, system-ui, sans-serif"
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
  icon-led-base:
    backgroundColor: "oklch(0.15 0.03 25)"
    rounded: "{rounded.circle}"
  icon-led-fill:
    gradient: "radial-gradient(circle at 38% 32%, oklch(0.85 0.08 25) 0%, {colors.distress-red} 55%, oklch(0.30 0.07 25) 100%)"
    rounded: "{rounded.circle}"
---

# Design System: Desktop Dashboard

## Overview

**Creative North Star: "The Night Ops HUD"**

This is not a wallpaper image — it's a live console rendered as the desktop background: an always-on heads-up display watching CPU, memory, battery, disk, network, and the sky outside, sitting one layer above the desktop background and one layer below every window. The philosophy is tactical and utilitarian: every glowing edge, every uppercase label, every accent color means something specific. Nothing lights up decoratively. Glow is a status signal (a battery actually charging, a metric running hot, a data feed gone stale), not an ambient effect applied for its own sake.

The system is precise and instrumented — icon, label, value, and chart line up in real table columns so a glance reads cleanly, numerals never jitter thanks to tabular-nums everywhere. It's also quiet and restrained at rest: panels sit low-contrast against a dark gradient void, and the only things that actively call for attention are the ones that should (an offline banner, a hot CPU, a charging battery). And it's dense and confident — a huge amount of live, frequently-updating data (three time-tiered charts per metric, a full partition list, network throughput) packed into roughly half the screen without padding it out or apologizing for the density.

The signature visual device, inherited from the CyberpunkUIKit template this system started from, is the **notch**: every panel's bottom-right corner is diagonally chamfered and capped with a small glowing accent tab, standing in for a border-radius the system otherwise never uses. There is no rounded-card language here — the notch *is* the corner language.

**Key Characteristics:**
- Notched, chamfered-corner panels — never rounded corners — each carrying a single committed accent (teal, gold, or red)
- Flat surfaces at rest; glow (text-shadow / box-shadow used as light emission, never elevation) is reserved for real status
- Dense, table-aligned metric rows: icon → label → value → chart or badge, every column locked to the same width across every table in the system
- Tabular numerals everywhere a value updates, so nothing visually jitters on refresh
- A dark, slowly-drifting ambient gradient background (42s/55s cycles, deliberately desynced) is the only "alive" surface that isn't tied to real data

## Colors

Three committed accents plus a cool, low-saturation neutral scale. Panels never mix accents — the accent is decided once per region (System = teal, Forecast = gold, alerts = red) and every glow, border, and underline in that region reads from the same one.

### Primary
- **Signal Teal** (`#2be4ea`): the System region's accent — CPU/memory/battery/storage/network panel borders, corner-tab glow, section-header underlines, and the default "nominal" tone for every metric chart and icon.

### Secondary
- **Ember Gold** (`#fed33f`): the Forecast region's accent (current conditions, hourly, week, sun & moon) — and doubles system-wide as the "hot/warning" tone once a metric crosses its threshold (CPU/memory/disk > 88%, battery < 20% and unplugged, sun-arc daytime marker).

### Tertiary
- **Distress Red** (`#e8615a`): reserved for things that are actually wrong or actively drawing power — the offline banner and its status dot, and the CPU/MEM/BAT utilization LEDs' fill and glow (a deep near-black red at rest, brightening toward full Distress Red as CPU/memory utilization approaches 100%, or solid while the battery is genuinely charging). Never used as a passive accent the way teal and gold are.

### Neutral
- **Signal White** (`oklch(0.96 0.01 250)`): primary text — numeric readouts, temperature, weather description.
- **Console Grey** (`oklch(0.70 0.03 255)`): secondary text — units, sun/moon times, less-emphasized numbers.
- **Recessed Grey** (`oklch(0.52 0.03 258)`): tertiary text — every uppercase label, tier captions, footer stats.
- **Deep Console** (`oklch(0.19 0.015 260 / 0.88)`): the panel fill itself — dark, slightly translucent, blurred behind.
- **Hairline Grey** (`oklch(0.70 0.04 260 / 0.14)`): the only border/divider color that isn't an accent — separates Storage/Network/footer sub-sections within one panel.

### Dim border variants
Each accent carries a desaturated "dim" twin (`#1f6a6e` teal / `#8a6a12` gold / `#9c3230` red) used exclusively for the 2px panel border — the full-saturation accent is reserved for glow, text, and fills; the dim variant is reserved for structural edges.

### Reserved
- **Standby Green** (`#2bfea0`): defined in the token set (inherited from the CyberpunkUIKit accent triad this system was ported from) but not yet wired to any element. The natural next use is a genuine "online"/"nominal" counterpart to the offline banner's red dot.

### Named Rules
**The One Accent Per Region Rule.** Every panel commits to exactly one accent — border, corner-tab glow, and section-header underline all read from the same `--panel-accent`/`--panel-border` pair. Accents never mix within a single panel.

**The Status-Only Glow Rule.** Glow (box-shadow-as-light, text-shadow) never decorates a resting element. It appears only when something is true: a metric is hot, the battery is actually charging, the feed is offline. A panel's corner tab and section-header underline are the one standing exception — they're the system's constant "this region is live" signature, not a status readout.

## Typography

**Display/Body Font:** Fira Sans, with `system-ui, sans-serif` fallback
**Label/Mono Font:** Fira Mono is declared in the token set but not actually applied anywhere — every numeric alignment need is met by `font-variant-numeric: tabular-nums` on Fira Sans instead of switching families. Treat `--font-mono` as reserved, not a live typographic voice.

**Character:** One typeface doing every job, kept legible at wallpaper viewing distance through weight and tracking alone — thin (100) for the one hero number, light (300) for secondary large text, default weight for dense data, and wide uppercase tracking for every label. No serif, no display face; the system deliberately doesn't reach for a second typeface to signal "brand."

### Hierarchy
- **Display** (weight 100, `clamp(2.4rem, 8vmin, 6.5rem)`, line-height 0.85, tracking -0.04em): the current temperature only. The single glow-text element that earns hero treatment.
- **Headline** (weight 300, `clamp(0.9rem, 2.3vmin, 1.7rem)`): the four weather-stat numbers (Wind/Humidity/Rain/UV).
- **Title** (weight 300, `clamp(0.8rem, 2vmin, 1.5rem)`): the weather description line ("Partly Cloudy"); a lighter-weight variant handles the week list's day names and hi/lo range.
- **Body** (weight 400, tabular-nums, `clamp(0.48rem, 1.02vmin, 0.74rem)` scaling up to `~1rem` for hourly temps): every live metric readout — system-table values, hourly temps, footer stats. This is the workhorse size; most of the screen's text lives here.
- **Label** (weight 400, uppercase, tracking 0.1em–0.3em, `clamp(0.4rem, 0.82vmin, 0.8rem)`): section headers ("System", "Storage", "Network", "Hourly", "Week", "Sun & Moon"), table row labels (CPU/MEM/BAT/NET, tier captions like "30S"/"5M"), and weather-stat keys. Wider tracking (0.3em) marks a section header; tighter tracking (0.1em) marks a row label — the tracking width itself signals the hierarchy level.

### Named Rules
**The Tabular Numerals Rule.** Every element carrying a value that updates on a poll gets `font-variant-numeric: tabular-nums`. A refreshing CPU percentage or download rate never shifts its neighbors horizontally.

## Layout

The page is a single fixed viewport (`overflow: hidden` on `html`/`body` — this is a kiosk surface, never a scrolling one) sized in `vmin` units throughout rather than fixed pixels, so the same proportions hold whether the surface renders at 1440×900 or 2560×1080. The one hard breakpoint is Tailwind's `lg` (1024px): below it the two regions stack in a single column; at or above it they sit side by side as an exact 50/50 grid (`grid-cols-1 lg:grid-cols-2`).

Page padding is `2.2vmin` on the sides, and top/bottom padding adds a configurable safe-area inset (`--safe-top`/`--safe-bottom`, sourced from `config.json`, default 48px) so content never renders under the user's own desktop panels. The grid gap between the two halves is `1.8vmin`.

Within each half, panels are content-sized, not stretched: rows pack toward the top and leftover vertical space below them is left unfilled, deliberately. A panel never distributes its rows to fill the available height. Related but distinct data within one panel (System's cpu/mem/bat rows vs. its Storage table vs. its Network table; Forecast's current-conditions vs. Hourly vs. Week vs. Sun & Moon) is separated by a `1px` hairline border-top with a small heading, not by breaking into a second panel card — one notched panel per region, internally divided.

Every metric table shares one `<colgroup>` proportion (icon ≈ `1.7em`, label auto, value `24–26%`, chart/badge `32–36%`) across System, Storage, and Network, so columns line up not just within a table but across the three stacked tables in that region.

## Elevation & Depth

Flat by design — there is no drop-shadow-based elevation anywhere in the system. Depth comes from two mechanisms instead: `backdrop-filter: blur(6px)` on every panel (rising to `blur(18px) saturate(1.3)` if the wallpaper is running in transparent mode over a photo background), and layered ambient radial/linear gradients in the page background that drift slowly (`42s`/`55s`, deliberately different periods so the two glow blobs never fall into a visible sync). `box-shadow` and `text-shadow` exist in the system, but they're never used to lift a surface — only to make an accent-colored edge or piece of text glow, which is a status signal, not a depth cue (see Colors' Status-Only Glow Rule).

### Named Rules
**The Flat-By-Default Rule.** No element receives a drop shadow for hierarchy. If something needs to stand out, it gets an accent-colored glow (glow = "this is live/important"), not a lift (lift = "this is above the surface").

## Shapes

The corner is the system's one recurring geometric signature: every panel is `clip-path`-cut from a rectangle into a pentagon, chamfering the bottom-right corner at a fixed `1.15rem` diagonal, with a small glowing accent-colored tab (`1.55rem × 3px`, rotated −45°) sitting in the resulting notch. Panel borders are a flat `2px` solid line in the region's dim accent color — no border-radius anywhere on a panel.

Outside the panel level, corner treatment drops to two simple cases: perfect circles (the `0.55em` status dot on the offline banner, and the `0.85em` utilization LED after each CPU/MEM/BAT row label), and everything else sharply notched. There is no intermediate rounded-rectangle vocabulary (no `8px`/`16px` card-radius family, no barely-rounded badge) — a shape is either sharply notched or a perfect circle, with nothing in between.

## Components

Every surface in this system is read-only — there are no buttons, form inputs, or navigation, since the dashboard has no interactive affordances at all. The components below are the actual repeating primitives: the panel container, the metric row, the utilization LED, the step-line chart, the section header, and the status dot/banner pairing.

### Panel (Notched Container)
- **Shape:** pentagon `clip-path` chamfering the bottom-right corner (`1.15rem` cut), `2px` solid border in the region's dim accent color, `backdrop-filter: blur(6px)`.
- **Fill:** Deep Console (`oklch(0.19 0.015 260 / 0.88)`), constant across every accent region — only the border and corner tab change color.
- **Corner tab:** a `1.55rem × 3px` bar in the full-saturation accent, glowing (`box-shadow: 0 0 6px`), rotated into the notch. This is the panel's one constant "alive" signal — present at rest, not tied to a status condition.
- **Padding:** `1.5–2.2vmin` depending on region density (System's panel is tighter than Forecast's).

### Metric Row (System Table Row)
- **Shape:** one `<tr>` in a shared-`<colgroup>` table — icon cell, uppercase label cell, right-aligned tabular-nums value cell (with an optional smaller Recessed-Grey sub-line), chart/badge cell.
- **Icon:** a hand-rolled 24×24 stroke-only SVG (`stroke-width: 1.6`, `currentColor`-driven), colored by the row's status tone (Signal Teal nominal, Ember Gold once past threshold). Unchanged by the Utilization LED (below) — the LED sits after the label text, not on the icon.
- **Tier sub-rows:** a metric with historical data (CPU, Memory, Battery) gets 2–3 additional slim rows directly beneath its value row — no icon, just a small tracked label (`30S`/`5M`/`30M`, or `30M`/`4H`/`24H` for Battery) and a step-line chart filling the chart column. This is what makes one metric read as a small instrument cluster rather than a single number.

### Utilization LED
- **Shape:** two stacked `0.85em` circles inline immediately after the CPU/MEM/BAT row's label text — close to that text's own cap-height, sitting right on the text rather than floating independently of it. (An earlier version was a much larger `1.6em` ring behind the icon; it read as too heavy and was replaced, ISSUES.md #16.)
- **Base (inert):** deep near-black red (`oklch(0.15 0.03 25)`), always present — reads as the LED's off state.
- **Fill:** a radial gradient (bright highlight center → full Distress Red → a darker red edge), whose opacity *and* glow (`box-shadow`, capped at a modest `5px` blur / `1.2px` spread so the halo stays proportionate to a small LED instead of blooming into a blob) both scale together from `0` to `1` with the metric's percentage — dim at rest, brightening to fully lit and glowing as it approaches 100%. A real LED brightens; it doesn't grow, so this is opacity-driven rather than the scale-transform the earlier ring version used. Transitions `0.6s ease`.
- **Battery variant:** ignores percentage and reflects a discrete charge state instead — solid + fully glowing while genuinely charging (not merely plugged in; see `sysinfo.battery_charging()`), dim (`0.25` glow, no boosted box-shadow) at rest, and flashing opaque/`0.12`-opacity on a hard `2s steps(1)` cycle (1s on, 1s off) when below 20% and not charging. Charging always wins over the low-battery flash. Respects `prefers-reduced-motion` (flash becomes a steady dim-ish state).
- Not interactive — a read-only status indicator, same as everything else in this system.

### Step-Line Chart
- **Style:** a "staircase" (step-after) SVG path, `stroke-width: 2`, rounded caps/joins, `preserveAspectRatio="none"` so it fills its cell exactly — deliberately not a smoothed/interpolated line, since the underlying data is polled samples, not a continuous signal.
- **Multi-series:** the Network chart plots two step lines (download in Signal Teal, upload in Ember Gold) on one shared scale, rather than two independently-scaled charts.
- **Sizing:** small enough to sit on one text line next to its tier label — this is the system's answer to "a chart, but compact."

### Section Header
- **Style:** Recessed Grey, uppercase, `0.3em` letter-spacing, with a full-width `1px` accent-colored underline that glows (`box-shadow: 0 0 6px`) — the same accent-glow language as the panel corner tab, applied to text instead of a shape.

### Status Dot + Offline Banner
- **Dot:** a `0.55em` solid circle in the banner's accent color, `50%` border-radius, sitting inline before the message text.
- **Banner:** a small notched panel (same pentagon `clip-path` as every other panel) in the Distress Red accent, fixed to the top-center of the viewport, hidden by default and shown only after two consecutive failed polls — deliberately debounced so a single dropped request never flashes the banner.

## Do's and Don'ts

### Do:
- **Do** give every panel exactly one accent (border + corner-tab glow + section-header underline all match) — see the One Accent Per Region Rule.
- **Do** use `font-variant-numeric: tabular-nums` on any text that holds a value which updates on a poll.
- **Do** let a panel's content determine its height; leave unused vertical space rather than stretching rows to fill it.
- **Do** reserve glow (box-shadow-as-light, text-shadow) for a real status condition — hot, charging, offline, or the panel corner tab's constant "this region is live" signal.
- **Do** keep every metric table's `<colgroup>` proportions identical (icon / label / value / chart) so columns line up within and across tables in the same region.

### Don't:
- **Don't** use large rounded corners or a soft-card look anywhere. The notch (pentagon `clip-path`, `1.15rem` chamfer) is this system's entire corner language; a `border-radius: 8px+` card would read as a different design system grafted on.
- **Don't** add a drop shadow for hierarchy or lift. This system has no elevation model — depth comes from blur and ambient background layering only (see the Flat-By-Default Rule).
- **Don't** mix two accents on one panel, or apply an accent color to an element outside its region.
- **Don't** add an interactive affordance (button, input, hover-driven control) — this is a read-only, no-input wallpaper surface. If a status element (like the utilization LED) looks like it could be a button, that's a bug, not an invitation to wire up a click handler.
