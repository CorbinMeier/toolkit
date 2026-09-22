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

The page is a single fixed viewport (`overflow: hidden` on `html`/`body`, and now on `#colLeft`/`#colCenter`/`#colRight` too — this is a kiosk surface, never a scrolling one) sized in `vmin` units throughout rather than fixed pixels, so the same proportions hold whether the surface renders at 1440×900 or 2560×1080. #91 replaced the old `lg`-breakpoint 50/50 split with `#root` as a real CSS Grid of up to three column containers, keyed off `data-layout` (`narrow`/`wide`, resolved in `app.js` from `outputs.<name>.layout` — `'auto'` picks by measured aspect ratio, ≥2 counting as wide). `narrow` keeps the pre-#91 grouping in two columns (System/Projects/Sessions | Forecast/Music/Log); `wide` — the 2.37-aspect Dell, which has genuine width to spare — moves to three, with Sessions regrouped into a new center column under Forecast rather than either column simply growing wider with nothing new in it. `app.js`'s `placePanels()` moves each panel `<section>` into the column its resolved layout wants: a CSS `grid-template-areas` switch can't do this, since each column stacks panels of different natural heights and named areas must form rectangles.

Page padding is `2.2vmin` on the sides, and top/bottom padding adds a configurable safe-area inset (`--safe-top`/`--safe-bottom`, sourced from `config.json`, default 48px) so content never renders under the user's own desktop panels. The grid gap between columns is `1.8vmin`.

Within each column, panels are content-sized, not stretched, and leftover vertical space below them is left unfilled, deliberately — the same rule as before #91, and why the Dell's center column can legitimately end well above the fold once Forecast+Sessions are done. The one deliberate exception (#91): a panel whose row count is genuinely open-ended (Sessions, Log) gets a `max-height` sized to the column it's currently in — retuned per `data-layout`/column via a CSS custom property, `--sess-rows`/`--log-rows`, the same single-source-of-truth shape #64 established for Projects' `--proj-page` — rather than growing without bound; anything past that fold reveals through the scroll-dwell Auto-Cycle Reveal loop (#29, see Components below) instead of clipping. That is *bounding* content, not *stretching a panel to fill the column* — the two are easy to conflate but opposite: the box still shrinks to nothing when the column is short on data, it just can never grow past its cap. Related but distinct data within one panel (System's cpu/mem/bat rows vs. its Storage grid vs. its Network grid; Forecast's current-conditions vs. Hourly vs. Week vs. Sun & Moon) is separated by a `1px` hairline border-top with a small heading, not by breaking into a second panel card — one notched panel per region, internally divided.

Every metric grid is compact, not stretched (#17): none carries `w-full` — each sizes to its own content instead of filling the panel, so a narrow value or a small right-aligned chart doesn't leave a dead gap between itself and its column boundary. Every System sub-panel now shares one `.sys-grid` shape (label / value+status-bar / sub, all auto-sized to content, #57) — CPU/MEM/BAT's table used to diverge first with a leading status LED column (#17, retired #31), then an icon column (retired #57); all three System sub-panels share the same plain grid shape now.

### Key:Value Grid (`.kv-grid`, #50)

A "spreadsheet" pattern for any panel whose content is fundamentally a list of named readings: a two-column CSS grid (`grid-template-columns: auto 1fr`), key column left (uppercase Label styling, left-aligned), value column right (Body styling, right-aligned, `.num` for tabular figures). Every row shares the same grid, so the value column starts at one consistent x position from the first row to the last — the thing that makes it read as a spreadsheet rather than a stack of independently-laid-out lines.

The Weather/Forecast panel is the first and, so far, only user: Condition, Temp, Feels, High/Low, Wind, Humidity, Rain, and UV are all one `.kv-grid`, replacing an earlier layout that paired unrelated values into a 2×2 table (description next to feels-like, temp next to high/low) above a separately-laid-out flex list for Wind/Humidity/Rain/UV — two different layout mechanisms whose value columns didn't align with each other.

System now expresses the same key-left/value-right idea through its own `.sys-grid` (#57, see Components below) rather than moving to `.kv-grid` itself — a two-column grid (label | value + status bar/chart) carrying a status visualization `.kv-grid`'s plain two-column model has no slot for, plus an optional sub line pinned directly under the value column (`grid-column:2`, #58) for what used to be CPU/MEM/BAT's stacked second line. It shares every other convention `.kv-grid` established: no icons, one font size for every cell. Music still uses its own bespoke layout (cover art + title/artist/progress), not a row grid at all. Calendar stays a literal month grid; it isn't a list of named readings and reorganizing it into key:value rows wouldn't make sense. (Devices used to share System's old icon-led row shape too, but the panel was removed entirely — #56.)

### Session Row Grid (`.sess-grid`, #71)

The Sessions panel's four-column variant of the same idea: `user | where | login | duration`, one grid so the three trailing columns line up top to bottom. Those three are auto-width, right-aligned, and tabular-figured, so a duration growing from `5m` into `1h 20m` doesn't shift its neighbours. Like `.proj-grid` and unlike `.sys-grid`, it takes `width: 100%` rather than `fit-content` — the user column is the flexible one and is what a long name should truncate into.

One state is encoded visually: a session that is **still open** takes the region's own accent (Signal Teal), while ended sessions stay neutral ink. That reuses System's accent rather than introducing a fourth — the One Accent Per Region Rule — and is the panel's only color signal. A session whose end was never recorded renders `?` for its duration rather than `0m`, because the row is real but its span genuinely isn't knowable; inventing a number there would assert something the data can't support.

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
| Background | `--z-bg` (-1) | The surface tint (`#suntint`, #78) — the opaque background itself since #89, recolored to real time-of-day hues by #90 — with the drifting ambient glow blobs (`.glow`) hidden under an opaque surface since #90 (only reappear if `display.transparent` reverts to `true`) |
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

This system was read-only for most of its life — no buttons, no inputs, no navigation — because the wallpaper surface had no pointer reach at all. That premise ended at #36: `dashd-host` now leaves the layer-shell input region at its default instead of blanking it, so the page receives pointer events. Interaction is therefore allowed, but it is the *exception*, and the bar is high: a control has to do something a passive display genuinely cannot (turn a page, command a player). Status indicators stay read-only — see the Read-Only-By-Default Rule under Don'ts.

The components below are the repeating primitives: the panel container, the metric row, the utilization bars, the compact tier graph, the step-line chart, the section header, the status dot/banner pairing, the control button, the context menu, and the surface tint.

### Panel (Notched Container)
- **Shape:** pentagon `clip-path` chamfering the bottom-right corner (`1.15rem` cut), `2px` solid border in the region's dim accent color, `backdrop-filter: blur(6px)`.
- **Fill:** Deep Console (`oklch(0.19 0.015 260 / 0.88)`), constant across every accent region — only the border and corner tab change color.
- **Corner tab:** a `1.55rem × 3px` bar in the full-saturation accent, glowing (`box-shadow: 0 0 6px`), rotated into the notch. This is the panel's one constant "alive" signal — present at rest, not tied to a status condition.
- **Padding:** `1.5–2.2vmin` depending on region density (System's panel is tighter than Forecast's).
  **#87:** the `impeccable` design hook flags every `.panel` for `cramped-padding`
  ("children flush against border+bg, no inset") — a false positive, not a
  defect. Its static analyzer can't resolve arbitrary-value Tailwind vmin
  classes (`px-[2vmin] py-[1.5vmin]`, Weather's `px-[2.4vmin] py-[1.8vmin]`),
  so it reads every panel as zero-padding. Measured with `getComputedStyle`
  over CDP at a real 2560×1080 render: actual padding is ~15–18px vertical /
  ~20–24px horizontal on every panel — above the hook's own 12–16px "ideal",
  not below its 8px floor. Persisted as a sanctioned ignore
  (`cramped-padding=*` scoped to `web/index.html` in `.impeccable/config.json`)
  rather than re-litigated each session.

### Metric Row (`.sys-grid` Row, #57)
- **Shape:** one `.sys-grid` row — uppercase label cell (left), value cell (right, tabular-nums, the row's status bar/chart attached inline), sub cell (right, dash-prefixed, Recessed-Grey). No icon and no separate tail column — both retired at #57 in favor of Weather's `.kv-grid` conventions (one font size for every cell, no stacked second line). CPU/MEM/BAT used to diverge with a leading LED cell (#17), then an icon cell (#31); both are gone now.
- **Alert:** a row's value takes the same filled white-on-red badge Weather uses (see Weather Value Alert Badge, #49) once it crosses its hot threshold (CPU/MEM/Storage > 88%, Battery ≤ 35% unplugged) — this replaced the retired icon-tone signal (Signal Teal nominal, Ember Gold once past threshold) as the row's status alert. The status bar itself (Utilization Bars, mini fill bar, step chart) is unrelated and keeps its own independent coloring regardless of the badge.

### Utilization Bars
- **Shape:** a row of ten slanted parallelogram segments (SVG `<polygon>`, sharing the leaning-right skew of the notch/corner-tab language) attached to the value cell of every CPU/MEM/BAT row, replacing the retired status LED (#31 — user feedback: the LED "wasn't working out"). Sized to sit inline with the row, same treatment Storage uses for its fill bar. Network no longer has an inline status graphic at all — its chart moved to its own row (#59).
- **Fill count:** `round(pct / 100 * 10)` segments read as lit; the rest sit unlit.
- **Color:** each lit segment takes a fixed spectrum color by its position in the bar — Distress Red at the low end, through Ember Gold, to Standby Green at the high end (see Status Bands under Colors) — not the metric's own live percentage. A half-full bar always shows the same red-to-gold gradient regardless of which metric it belongs to; only the *count* of lit segments encodes the percentage. Unlit segments are a flat, low-opacity Hairline-adjacent grey (`oklch(0.32 0.02 260 / .45)`), reading as the empty track.
- Not interactive — a read-only status indicator, same as everything else in this system.

### Step-Line Chart
- **Style:** a "staircase" (step-after) SVG path, `stroke-width: 2`, rounded caps/joins, `preserveAspectRatio="none"` so it fills its cell exactly — deliberately not a smoothed/interpolated line, since the underlying data is polled samples, not a continuous signal.
- **Fill:** an optional area fill under the line, a vertical gradient in the row's own tone (`.35` opacity at the line, fading to `0` at the baseline), for more visual presence at wallpaper viewing distance than a bare stroke. Currently used nowhere in the shipped UI (CPU/MEM's filled Trend Graph, the component that used it, was removed — #34, not wanted on the wallpaper; metrics collection continues server-side) but the `stepChart(..., fill: true)` option stays for a future consumer.
- **Multi-series:** one chart accepts several series sharing its x/y scale. Network used this to plot download and upload as two lines in one box; since #59 it draws them as two separate boxes side by side (see Network Graph Row below), so the multi-series path has no live consumer today and the shared-scale guarantee moved into the explicit `domain` option.
- **Shared domain:** `stepChart(series, cls, domain)` takes an optional `[lo, hi]` overriding the scale the chart would otherwise derive from its own values. Two charts drawn side by side are two SVGs and cannot share a scale implicitly the way two series in one box do; passing both the same domain is what keeps them comparable. Never let two boxes of the same metric self-scale independently — a 40K/s upload would draw the same shape as a 40M/s download.

### Network Graph Row (#59)
- **Shape:** a row inside System's `.sys-grid` spanning both columns (`.sys-graph`, `grid-column: 1 / -1`), directly beneath the NET metric row with a `0.35vmin` top margin so it reads as its own band rather than a third line of the row above. The metric row keeps the standard Metric Row shape — this is an extra row under it, not a change to its geometry — so System's columns still line up across CPU/MEM/BAT, Storage, and Network. The metric row itself carries no value/sub of its own (#60) once the chart row is showing; down/up render under their own chart instead, so the same two numbers never appear twice.
- **Content:** two captioned step charts side by side (`0.8vmin` gap) — DOWN in Signal Teal, UP in Ember Gold — sharing one domain spanning both series, each with its own rate (`text-ink`, e.g. "down 611K/s") directly underneath (#60) — a caption identifies which line is which, but reading the actual number used to mean looking away to the disconnected NET row above.
- **Captions:** the same faint uppercase treatment as a `.sys-key` label, not tinted to match their line. A caption sitting directly above its own chart already identifies it; coloring the text would spend an accent on a label to say what position already says.
- **Fallback:** under two ring-buffer samples (briefly, right after a poll starts) no chart can draw at all — the NET row falls back to a plain label/value/sub row (down as value, up as sub) so the rate is still visible rather than showing nothing.
- **Sizing:** each chart carries an explicit width clamp rather than `w-full`. `.sys-grid` is `width: fit-content`, so a percentage width would resolve against whatever the text rows happen to measure; sizing the charts and letting the grid widen to them keeps "every metric grid is content-sized" (#17) true with a graph row in it.
- **Empty state:** under two samples every series is a single point and the chart draws nothing, so the whole graph row is omitted rather than rendering a pair of empty boxes.

### Section Header
- **Style:** Recessed Grey, uppercase, `0.3em` letter-spacing, with a full-width `1px` accent-colored underline that glows (`box-shadow: 0 0 6px`) — the same accent-glow language as the panel corner tab, applied to text instead of a shape.

### Status Dot + Offline Banner
- **Dot:** a `0.55em` solid circle in the banner's accent color, `50%` border-radius, sitting inline before the message text.
- **Banner:** a small notched panel (same pentagon `clip-path` as every other panel) in the Distress Red accent, fixed to the top-center of the viewport, hidden by default and shown only after two consecutive failed polls — deliberately debounced so a single dropped request never flashes the banner.

### NWS Alert Banner (#80)
- **Shape:** a plain inline block inside the Weather panel, between the header and `.kv-grid` — not the fixed viewport-top Offline Banner above, since this is a panel-scoped fact, not a connectivity condition. Reuses `.alert-badge` (filled white-on-red) rather than a new treatment or swapping the panel's own `accent-amber`, same "highlight the value, not the card" call #48 made for `weather.alert`.
- **Content:** the most severe active `api.weather.gov` alert (event name + expiry), or nothing — hidden entirely with zero active alerts, never an empty "no alerts" row (a row that's always the same teaches the eye to stop reading it).
- **Distinct from `weather.alert`:** the threshold-derived signal (#46) and an official NWS alert answer different questions and can both be true at once; neither overwrites the other.

### Control Button (`.ctl-btn`, #36)
- **Shape:** a `1px` solid rectangle in the panel's own dim accent (`--panel-border`), sharp-cornered. No radius — the notch is the system's only corner language, and a rounded button would read as a grafted-on design system (see Don'ts).
- **States:** Recessed Grey at rest → Bone White text with a full-accent border on hover → full accent text while active. Deliberately **no glow on hover**: glow is reserved for a real status condition (see the Status-Only Glow Rule), and spending it on "your cursor is here" would dilute the one signal that means "this is live".
- **Disabled:** `0.3` opacity, default cursor, control stays in place. Never hide a control that has become unavailable — a pager whose buttons vanish at the ends reflows the row every time you reach one.
- **Reflow discipline:** a control whose state changes on a poll (Music's play/pause) keeps a fixed footprint; swap the glyph, never the element's size.
- **Consumers:** Projects pagination (#64), Music transport (#65).

### Context Menu (`.ctx-menu`, #66)
- **Trigger:** right-click anywhere on the surface. Replaces WebKit's own default menu (Reload / Inspect Element / Open Frame in New Window ...), which #36 made reachable the moment the layer-shell surface stopped blanking its input region — suppressed both client-side (`contextmenu` → `preventDefault()`) and host-side (`dashd-host`'s `context-menu` signal handler, belt-and-braces).
- **Shape:** the `.panel` notched container reused wholesale — same pentagon `clip-path`, border, corner-tab glow — fixed-positioned at the click point and clamped inside the viewport, sized to its own content rather than full-width. Picks `.accent-cyan`, System's own accent, rather than adding a fifth palette entry.
- **Items:** a data-driven list (`menuItems()` in `app.js`), not two hardcoded buttons — the first step toward a fully customizable interface (reposition/toggle panels, per-container options) per #66's issue body. Each row is a flat, borderless button (`.ctx-menu-item`) — a wash of the panel's own accent on hover, full accent text while pressed, no glow (same Status-Only Glow Rule as Control Button).
- **Content today:** Reload (`location.reload()`) and one row per `display._theme_options`, the active theme checked. Persists through `POST /api/config` (an `EDITABLE`-style whitelist on `dashd-serve`, mirroring `dashd-control`'s own — see #66) rather than a client-only toggle, so it survives the next poll and propagates to every output's surface automatically.
- **Dismissal:** any click outside the menu, or Escape.

### Surface Tint (`#suntint`, #78)
- **Problem:** the wallpaper surface was purely functional — HUD panels over whatever cosmic-bg is showing — with no ambient sense of time of day at all. User request, verbatim (#78): "a subtle gray slate and blue fade [...] change color to an orange gray slate based on the time of day [...] morning sunrise, afternoon blue, evening sunset, and midnight purple." Two rounds (#78, #88) instead tuned this to near-invisibility under a "subset of slate gray, barely present" bar; #90 withdrew that bar outright after the user, looking at the finally-opaque (#89) surface, said midnight "doesn't really feel like midnight" and asked for real sunrise/noon/sunset/midnight colours. #92 then asked for the gradient's *geometry* to move too — "like a skybox" — since a palette that only cycles colour in place still has the light arriving from the same place at every hour.
- **Shape (geometry rewritten by #92):** a full-viewport, THREE-stop `linear-gradient(180deg, top 0%, glow <pos>%, bottom 100%)` painted by a dedicated fixed element on the Background layer (`--z-bg`), behind every panel. **`display.transparent` is `false` since #89** — `#suntint` paints at alpha `1` and *is* the background; cosmic-bg's wallpaper is fully hidden beneath it. `top` is the sky (darkens toward `bottom` as the sun nears the horizon — "a still-dark sky" under a sunrise/sunset glow, in the issue's own words — and holds the plain accepted hue at noon/midnight); `glow` is a warm middle stop whose **position** climbs and falls continuously with solar altitude, not just its colour; `bottom` is the deep base, untouched by any of this — same role it always had.
- **Color (repainted by #90; hues unchanged by #92 — this issue moves them, it does not re-pick them):** four phases, each a real, recognisable colour rather than a near-neutral variant of a shared slate base — sunrise (warm orange/rose, `TINT_PHASES.sunrise`, over a still-dark sky), noon (`afternoon` key — open daylight blue), sunset (deeper and redder than sunrise, so the two never read as interchangeable), midnight (a genuinely dark blue-violet, low luminance, not a purple wash). "A subset of slate gray, barely present" governed this palette for #78/#88/#89 and is retired by #90 — it was the bug the user was reporting, not a bar to keep meeting. `TINT_NEUTRAL` (the Off state) is unchanged from #89.
- **Geometry (#92):** `app.js`'s `solarAltitude()` derives a continuous altitude in `[-1, 1]` from the same real `weather.sun.sunrise`/`sunset` times the four phases already anchor to (see *Phase timing* below) — 0 at sunrise/sunset, `+1` at solar noon, `-1` at true midnight (the midpoint of the night span) — via a smooth two-piece sine, not a real solar-elevation calculation (the issue calls that welcome but not required). `tintGlowPos()` maps altitude to the glow stop's vertical position: ~82% (near the bottom edge — the horizon) at altitude 0, climbing to 15% (near the top) at altitude `+1`, and symmetrically back down through sunset. `tintIntensity()` maps altitude to how far the glow stop departs from the plain two-stop gradient it sits on top of: `1` at altitude 0 (strongest at dawn/dusk, "low sun, long light"), floored at `0.15` through the daytime span so noon still reads as a faint wash rather than the stop vanishing into the gradient for the whole middle third of the day, and unfloored (reaching exactly `0`) through the night, so the glow is genuinely gone by true midnight. Both curves are continuous through the sunrise/sunset boundary (both pieces evaluate to the same altitude there), so a time-lapse never shows a discrete jump — see `.claude/rules/verification.md`'s `?forcetime=` parameter for how to render one.
- **Strength (`display.tint_strength`, #88; re-scoped to colour departure by #89; range re-tuned by #90; also now gates the #92 geometry):** blends from `TINT_NEUTRAL` toward each phase's tuned target color — Off (`0`) is the flat neutral slate with no time shift **or motion** at all (the darken-toward-horizon and glow-stop-position effects are both scaled by `min(strength, 1)`, so Off collapses the three-stop gradient to an exact static two-stop one, indistinguishable from the pre-#92 Off state), never a transparent surface; Medium (`1`, the config default) lands on the tuned per-phase target with the geometry at full strength; Strong (`1.6`, the user's own live pick since #88) extrapolates the *colour* further past target as before, but the geometry's own amount is capped at `1` even at Strong — letting it extrapolate too would overshoot the glow past its own target hue and read as a knee brighter than the sky rather than a horizon glow under it. Subtle (`0.6`) sits between Off and Medium. Same context-menu Tint submenu and `data/config.local.json` persistence as #88/#89/#90 — only the meaning of "how far" grew a second (geometry) dimension.
- **Contrast:** `.panel`'s fill (`--color-panel`) is 88% opaque, so at most ~12% of any tint value bleeds into a panel's own background regardless of how saturated the phase or how strong the glow stop is. Measured (WCAG contrast, panel-blended, `oklch()` values converted to sRGB) at Strength 1.6 across the widest fields #92 can produce — the glow stop at full intensity at dawn and dusk, the noon sky/glow near the top of the gradient (glow ≈ sky there, so this is effectively "pure phase top", the tightest field the geometry can produce), and midnight with the glow floored to 0: `--color-ink` stays >=14.4:1, `--color-muted` >=6.1:1, `--color-faint` >=2.9:1, the accent triad (cyan/warm/crimson) >=4.8:1. These track #90's own figures (14.88/6.29/3.05/5.02) within the conversion's own margin of error — the noon field is marginally the tightest because sampling near the top of the gradient is closer to the phase's pure, unaveraged top colour than #90's own "average of top and bottom" measurement was, which was already an approximation of what a panel positioned near the top edge would actually see. No raw text renders directly against `#suntint` — the boot screen and offline banner both have their own opaque layers above it — so the panel's own opacity is what keeps content readable, not restraint on the tint; reported here rather than quietly narrowed to make the numbers move.
- **Phase timing:** centers follow the real sun, not the clock. `weather.sun.sunrise`/`sunset` (already in `/api/state`, for the configured location) anchor the sunrise and sunset phases directly; afternoon centers on their midpoint (solar noon) and midnight on the midpoint of the night span, so all four phases drift with the season together rather than two following the sun and two pinned to `12:00`/`00:00`. Missing/stale weather falls back to a fixed plausible day (06:30/18:30) so the tint still cycles instead of freezing on one phase — same degrade-gracefully posture as `now_playing()`/`ghstats`. Unchanged since #78 — #90 and #92 both kept it exactly as built.
- **Motion:** `app.js`'s `sunTint()` is a plain continuous linear interpolation between the two nearest phase centers for colour, recomputed fresh on every poll (~5s), composed with the #92 altitude-driven geometry above. There is no discrete phase boundary anywhere in the implementation — the four "phases" are anchor points on a continuous wheel, not four fixed states with a switch between them — which is what keeps a phase change, or the glow stop's motion, from ever reading as the step-change-on-a-wallpaper the issue explicitly calls out as eye-catching.
- **Theme interaction — theme-independent, one set of four phases:** decided over "four phases per theme" because this is a daylight cue tied to the real sun, not a HUD-chrome color. Since #89 the tint is opaque, so it also fully covers Retro Terminal's/Orange's own flat CRT body backgrounds (`#040a06`/`#0a0503`) — those colors are still declared (the one-key revert to `display.transparent: true` needs them) but nothing currently paints them.
- **Ambient glow blobs (`.glow`, #90):** the crimson/cyan blurred blobs were dormant for as long as the surface was transparent (`body.transparent .glow{display:none}` hid them); #89's opaque surface put them on the same `--z-bg` layer as `#suntint` and revealed them fighting it directly — a cyan blob over a warm sunrise, or crimson over noon's open blue, muddies exactly the signal this component exists to send. **Removed under the opaque surface**: `body:not(.transparent) .glow{display:none}` now hides them whenever the surface is opaque, the same call `retro_terminal`/`retro_orange` already made unconditionally for their own reasons. `.g1`/`.g2` stay declared (not deleted) so flipping `display.transparent` back to `true` — the documented one-key revert from #89 — restores the original cyberpunk-over-wallpaper look, glow blobs included, in one change.
- **Not interactive** — a read-only ambient layer.

### Auto-Cycle Reveal
- **Problem:** the desktop surface *had* no pointer or keyboard reach — it sits below every window — so a panel whose content outgrew its box had no scrollbar a user could ever grab (ISSUES.md #29). Since #36 the surface does accept pointer input, so this is no longer the only option: Projects, its one shipped consumer at the time, moved to real pagination at #64 instead (still the right call there — it's the one panel worth manually paging through).
- **Mechanism:** opt in per-element with `class="auto-cycle"` plus a CSS height/max-height so it can actually overflow (`overflow-y:hidden` — no visible scrollbar, since it's never user-driven). `app.js`'s `refreshAutoCycles()` (called every `apply()`) scrolls it slowly to the bottom, dwells, resets to the top, and repeats; an element that already fits its box is left alone.
- **Status:** Sessions and Log (#91) are the current shipped consumers — passive lists nobody is expected to page through by hand, bounded by `.sess-scroll`/`.log-scroll`'s `max-height`. Music's scrolling title still uses the horizontal variant for a too-long track name.

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
- **Don't** make a status element interactive. **The Read-Only-By-Default Rule.** The surface accepts pointer input since #36, but that is not licence to wire up click handlers: a control earns its place only by doing something a passive display cannot (paginate, command a player, reload the surface, switch the theme). If a status element — the Utilization Bars, a step chart, a metric row — merely *looks* like it could be a button, that is still a bug, not an invitation. Anything clickable must look clickable (`.ctl-btn`, `.ctx-menu-item`), and anything that isn't must not. The Context Menu (#66) is the carve-out stated explicitly: a right-click is itself an interactive affordance by definition, not an accidental one, and its whole job is to do things (reload, re-theme) a passive display structurally cannot.
- **Don't** let a control glow on hover, or grow/shrink as its state changes. Glow means "live status"; size changes reflow the panel. See Control Button.
