# Issues

Newest first. No GitHub remote on this project, so this file is the tracker.

## 15. Utilization "power gauge" ring around CPU/MEM/BAT icons; remove CHRG badge

Status: closed
Source: user request this session
Date: 2026-08-20

Circle the CPU/MEM/BAT row icons with a two-layer badge: a fixed deep/dark
red base disc, plus a second same-size disc on top that grows from the
center and glows brighter (radial gradient + box-shadow) as utilization
approaches 100%, reading as a status light powering up rather than a
progress bar. Battery ignores the percentage-driven fill and instead
reflects a discrete charge state: solid + glowing while charging, dim at
rest, and flashing (1s on / 1s off) when below 20% and not charging.

The CHRG text badge is removed -- the battery ring now carries that same
"is it actually charging" signal, so the two would be redundant.

Shipped as specified. DESIGN.md + `.impeccable/design.json` updated (Status
Badge (CHRG) component replaced with Utilization Ring; Distress Red's role
expanded to cover the continuous CPU/MEM fill; the now-unused
`rounded.badge` token dropped). Verified functionally: full lint→test→build
chain green (32 unit tests + smoke runs including the headless-Chrome
render pass), plus a manual screenshot against an isolated worktree-local
server confirming the ring actually renders and scales -- dim at 29% CPU,
brightly lit and glowing at 87% MEM, dim at 79% battery/discharging. Merged
to `main`.

Started at: 2026-08-20T17:38:53-07:00
Ended at: 2026-08-20T17:53:08-07:00
Time elapsed: 14m 15s

## 14. Standalone metrics collector -- decouple SQLite writes from dashd-serve

Status: closed
Source: user request this session
Date: 2026-08-20

dashd-serve currently piggybacks its historical-metrics SQLite write
(`maybe_sample_metrics`) on the /api/state HTTP poll -- a deliberate choice
at the time (see its docstring), but it means metrics are only collected
while some view is actively polling, and mixes a write concern into the
request-serving path. Split it: a new standalone `bin/dashd-collect`
script owns every write to `data/metrics.db`, sampling on its own timer
(`config.json`'s `refresh.metrics_sample_seconds`) independent of whether
dashd-host has any view open. dashd-serve keeps its fast live psutil poll
for the "current value" numbers shown in the panel (unchanged, still 5s-
fresh) and becomes purely a reader against SQLite for `/api/history` --
per the user's explicit direction, a mix of both: live values stay live,
past values are queried from what the collector persisted with a
timestamp.

Shared cpu/mem/battery/net sampling logic (net-throughput delta, real
charging-vs-plugged-in state, cpu temp) moves into a new `lib/sysinfo.py`
so dashd-serve and dashd-collect can't drift out of sync on how a metric is
actually measured.

`bin/dashd-collect` + `lib/sysinfo.py` shipped; `dashd-serve` re-exports the
moved names for back-compat and is now read-only against `metrics.db`. New
`systemd/desktop-dashboard-collect.service` is written and linked but
**not enabled** -- Gatekeeper Protocol, needs explicit approval. Verified
functionally: full lint→test→build chain green (32 unit tests + 19-check
smoke run against an isolated port), plus a manual end-to-end run of
`sysinfo.sample_stats()` through `insert_sample`/`query_history` against
real hardware data. Merged to `master`.

Started at: 2026-08-20T17:38:53-07:00
Ended at: 2026-08-20T17:44:56-07:00
Time elapsed: 6m 3s

## 13. Document the visual design system (DESIGN.md)

Status: closed
Source: user request this session (`/impeccable document`)
Date: 2026-08-20

Generate `DESIGN.md` (plus its `.impeccable/design.json` sidecar) capturing
the visual design system that emerged across issues #4-#12 -- the
CyberpunkUIKit-derived accent triad, notched panels, step-line charts,
table-aligned metric rows -- so future AI-driven design work on this
project stays on-brand instead of re-deriving conventions from scratch.

Ran in scan mode (existing rendered code, no PRODUCT.md needed for a
scoped docs-generation command). Extracted the full token set from
`web/index.html`'s dual `@theme`/`:root` blocks and cross-checked actual
usage in `web/app.js` -- found `--color-online` and `--font-mono` are both
declared but never consumed anywhere, documented as reserved rather than
silently dropped or invented a use for. Confirmed qualitative direction
with the user in two rounds: North Star "The Night Ops HUD", tactical/
utilitarian voice, "Night Signal" color naming (Signal Teal / Ember Gold /
Distress Red), flat elevation as a hard invariant, no rounded-corner/
soft-card look as a standing Don't. Wrote both the canonical 8-section
DESIGN.md and its `.impeccable/design.json` sidecar (tonal ramps, shadow/
motion/breakpoint extensions, 6 component HTML/CSS snippets: Panel,
Metric Row, Step-Line Chart, Section Header, CHRG Badge, Status Dot +
Offline Banner).

Corrected a process error mid-task: the two files were first written
directly in the main checkout, outside the §8 worktree flow. Deleted them
before committing anything, filed this issue, and redid the same writes
inside `issue-13-design-md`.

Started at: 2026-08-20T17:17:55-07:00
Ended at: 2026-08-20T17:20:45-07:00
Time elapsed: 2m 50s

## 12. Multi-tier step-line charts for CPU/MEM/BAT; dedicated Storage/Network sections

Status: closed
Source: user request this session
Date: 2026-08-20

Add a step line chart (compact enough to sit inline with text, one per
line) for CPU and Memory. Battery goes directly below Memory. Storage gets
its own dedicated, visually separated area within System, and below that a
dedicated Network section with another step-line chart carrying two lines
(upload and download).

Store metrics history for CPU, Memory, and Battery specifically (network
doesn't need long-term storage). CPU probably wants three step charts:
30 seconds, 5 minutes, and 30 minutes. Same tiers for Memory. Battery
would probably benefit more from 30 minutes, 4 hours, and 24 hours.

New `stepChart()` (step-after "staircase" interpolation, replacing the old
diagonal `miniSpark()`) renders one or more series on a shared scale, so
Network's download/upload lines share one chart. CPU/Memory each get a
value row plus three tier sub-rows (30S/5M/30M); Battery gets a value row
plus three tier sub-rows (30M/4H/24H) directly below Memory. Storage and
Network moved into their own `<table>`s under a "Storage"/"Network"
sec-head, separated from the cpu/mem/bat rows by a hairline.

Two independent trend sources, since neither one alone covers every tier:
a client-side ring buffer (this session's own ~5s `/api/state` polls)
backs the 30-second CPU/Memory tier and the Network chart -- the DB's
~30s sample interval can't resolve a 30-second window at all, and would
flatten brief network bursts even at a longer window. Everything >= 5
minutes reads from `/api/history` instead (`trendHistory`, fetched once
per minute for the whole retention window and sliced per tier
client-side), since that persists across a page reload and the DB's
resolution is fine by then. `battery_pct` was added to
`lib/metrics.py`'s `COLUMNS`, with a self-migrating `ALTER TABLE ADD
COLUMN` in `_connect()` so the already-running production `metrics.db`
(cpu/mem/net only, from #7) didn't 500 on first touch. `dashd-serve` also
now exposes `config.metrics_retain_hours` in `/api/state` so the
frontend's history-fetch window tracks the server's actual retention
instead of a second hardcoded constant that could silently drift from it.

Started at: 2026-08-20T16:58:11-07:00
Ended at: 2026-08-20T17:03:19-07:00
Time elapsed: 5m 8s

## 11. System panel: table layout, icons, lsblk-sourced disk/partition list, battery CHRG indicator

Status: closed
Source: user request this session
Date: 2026-08-20

Further compress the System panel -- it's still too wide, and the bars are
longer than they need to be. Show CPU and Memory as a single line each:
`CPU: %00 [HISTOGRAPH]` / `MEM: %00 [HISTOGRAPH]`, no separate full-width
progress bar. Disk should enumerate everything lsblk reports (not just
psutil's mounted-partition view) and show each partition's utilization.
Battery becomes `BAT: %00` plus a `CHRG` status-indicator badge: deep red
when unplugged, glowing red only while actually charging (not just
plugged-in-and-full). Lean on icons throughout (CPU icon before the CPU
row, etc.), laid out in an actual `<table>` so every row's columns line up.

System now renders as `<table class="sys-table">`: hand-rolled cpu/mem/
disk/battery icon SVGs, one `<tr>` per metric via a `sysRow()` builder so
icon/label/value/tail columns line up via `<colgroup>`. CPU and Memory
dropped their full-width progress bar for a single line (`42%` + small
temp/freq sub-line) with `miniSpark()` as the "[HISTOGRAPH]" -- the same
inline sparkline function from #7, reused rather than duplicated. Per-core
strip and the old vertical disk-bar cluster were both dropped in favor of
the table. Disk rows now come from a new `disk_tree()` in dashd-serve that
shells out to `lsblk -J -b -o NAME,TYPE,SIZE,FSTYPE,FSUSE%,MOUNTPOINT` and
walks the block-device tree for every partition with a mountpoint or a
real FSUSE% (falls back to the old psutil-only view if lsblk is missing).
Battery gets a real `charging` flag from a new `battery_charging()`, which
reads `/sys/class/power_supply/BAT*/status` ("Charging" vs "Full"/
"Discharging") since `power_plugged` alone can't distinguish charging from
plugged-in-and-full; the CHRG badge (`.chrg-badge`/`.is-charging` CSS)
glows only in the former case.

Started at: 2026-08-20T16:29:15-07:00
Ended at: 2026-08-20T16:34:50-07:00
Time elapsed: 5m 35s

## 10. Consolidate weather/forecast/sun-moon into one panel, right half of screen

Status: closed
Source: user request this session
Date: 2026-08-20

Forecast becomes its own single panel (current conditions, hourly, week,
then sun/moon, stacked as sub-sections of one panel) rather than three
separate panel cards. That panel occupies the right half of the screen;
System occupies the left half.

Grid dropped from three unequal columns to `lg:grid-cols-2` (exact halves).
Right half is one `.panel` with hairline-divided sub-sections (current
conditions, Hourly, Week, Sun & Moon) under a single amber accent, instead
of three separately-notched panel cards.

Started at: 2026-08-20T16:29:15-07:00
Ended at: 2026-08-20T16:34:50-07:00
Time elapsed: 5m 35s

## 9. Remove the date/location readout

Status: closed
Source: user request this session
Date: 2026-08-20

The top-left date + city panel is redundant -- the user already knows
where they are. Remove it entirely (date text, location text, and the
panel that held them).

Removed `#date`/`#loc` and the panel that held them, plus `tickClock()`
and its 250ms interval and the `$('loc')` assignment in `apply()` -- there
was nothing else driving those elements once the readout was gone.

Started at: 2026-08-20T16:29:15-07:00
Ended at: 2026-08-20T16:34:50-07:00
Time elapsed: 5m 35s

## 8. Redesign weather forecast panels — remove hourly graph, compact week

Status: closed
Source: user request this session
Date: 2026-08-20

The "Next 24 hours" panel is vague and its sparkline graph takes way too
much vertical space. Replace it with a compact horizontal list of the next
few hours (time + icon + temp, no chart), under a clearer header. The
"Week" list is fine conceptually but needs major compacting (denser rows,
smaller icons/type).

Replaced with a 6-hour compact list (time+icon+temp, no chart) under a
plain "Hourly" header. Server now tags each hourly entry with a day/night
-aware icon (added `is_day` to the Open-Meteo hourly request) so the
frontend can reuse weatherIcon() like it already does for daily entries.
Week list rows got tighter spacing/type/icon sizing and no longer stretch
to fill the column.

Started at: 2026-08-20T16:05:08-07:00
Ended at: 2026-08-20T16:16:30-07:00
Time elapsed: 11m 22s

## 7. Redesign System panel — heavy compaction + SQLite historical metrics

Status: closed
Source: user request this session
Date: 2026-08-20

Heavily compact the System panel's rows. Add SQLite-backed historical
storage for CPU utilization, CPU temp, CPU frequency, memory usage, and
network throughput, sampled on a slower independent interval so the DB
stays small. Disk usage moves from horizontal bars to a compact vertical
bar cluster (like the existing per-core strip).

Added `lib/metrics.py` (SQLite, WAL, prune-on-insert retention) storing
cpu_pct/cpu_temp_c/cpu_freq_mhz/mem_pct/net_down/net_up. dashd-serve
samples into it by piggybacking on the existing /api/state poll (at most
once per `metrics_sample_seconds`, default 30s) rather than a second timer
thread, since psutil's cpu_percent and this file's own net-delta tracking
both key off "since the last call by anyone" and a second independent
caller would corrupt both. New `/api/history?hours=N` route, clamped to
`metrics_retain_hours` (default 24). Frontend polls it separately (60s,
2h window) and renders compact inline sparklines next to the CPU and
Memory bars; temp/freq/network stay numeric-only in v1 (history is stored
and queryable regardless) to avoid fighting the compaction goal with too
many charts. Disk usage now renders as a vertical-bar cluster (diskStrip,
mirroring the existing per-core coreStrip) with a short mount label +
free space under each bar.

Started at: 2026-08-20T16:05:08-07:00
Ended at: 2026-08-20T16:16:30-07:00
Time elapsed: 11m 22s

## 6. Configurable safe-area padding for top/bottom desktop bars

Status: closed
Source: user request this session
Date: 2026-08-20

Panel content can hide under the user's top/bottom system bars. Add
top/bottom safe-area padding, driven by config.json so it's tunable
without a code change.

Added `display.safe_area_top`/`safe_area_bottom` (default 48px each --
an unverified guess at typical COSMIC panel height, tune as needed),
applied as `--safe-top`/`--safe-bottom` CSS vars added on top of the
existing grid padding. Not visually confirmed against this machine's
actual panels -- the user is doing visual verification this round.

Started at: 2026-08-20T16:05:08-07:00
Ended at: 2026-08-20T16:16:30-07:00
Time elapsed: 11m 22s

## 5. Remove the clock/time readout

Status: closed
Source: user request this session
Date: 2026-08-20

The user's system already shows the current time; the dashboard's own
digital clock is redundant. Remove it, keep date + location.

Removed #clock/#seconds and their tickClock() logic (date still ticks
locally every 250ms). Also removed the now-dead `clock24`/`pad()` that
only existed to format the removed readout.

Started at: 2026-08-20T16:05:08-07:00
Ended at: 2026-08-20T16:16:30-07:00
Time elapsed: 11m 22s

## 4. Restyle dashboard panels with Cyberpunk UIKit visual language

Status: closed
Source: user request this session
Date: 2026-08-20

Port the visual language of the `ui/cyberpunk/CyberpunkUIKit` template
component (crimson/cyan/amber accents, notched cut-corner cards, glowing
accent underlines/text, status dots) into `web/index.html`'s CSS so the
dashboard panels read as cyberpunk-themed. CSS-only: app.js already sources
its colors from CSS custom properties, so no JS changes are needed. No
Google Fonts (Rajdhani/VT323) — this project vendors everything and makes no
network calls at runtime, so the kit's font fallback (font-sans/font-mono)
is kept deliberately.

One column per accent (cyan=clock/system, amber=weather, crimson=forecast/
sun-moon), notched `.panel` cards with a glowing corner tab, glowing
section-header underlines, glow text on the clock/temp readouts, and a
status dot on the offline banner. Verified with an isolated worktree-local
`dashd-serve` instance (temp port, reverted before commit) plus live
`getComputedStyle` checks in a real browser tab, not just a screenshot.

Surfaced and fixed a latent bug along the way: the vendored Tailwind v4
browser JIT only emits an `@theme` token onto `:root` when its scanner sees
the token's name in an HTML class attribute — never for a bare `var()`
reference inside a separate `<style>` tag or in `app.js`. `--color-warm` had
been emitted only as a side effect of the offline banner carrying
`text-warm`; recoloring that banner (this change) silently broke every other
`var(--color-warm)` consumer app-wide until caught live. Documented in
CLAUDE.md's gotchas.

Started at: 2026-08-20T15:44:07-07:00
Ended at: 2026-08-20T15:55:56-07:00
Time elapsed: 11m 49s

## 3. Per-output `layout` config is plumbed but has no effect

Status: open
Source: self-identified during build
Date: 2026-08-20

`config.json` `outputs.<name>.layout` is read by `app.js` and written to
`document.documentElement.dataset.layout`, but no CSS keys off `[data-layout]`
yet. Both monitors therefore render the same three-column grid, differing only
by Tailwind's viewport breakpoints. It looks correct on both, so this is a
missing feature rather than a defect — but the config key currently implies
more than it delivers.

Unblocks: decide whether the 1440x900 laptop should drop a column (e.g. clock
+ weather only, no week/sun panels) or keep parity with the ultrawide.

## 2. `display.transparent: true` — mechanism verified, not adopted

Status: closed
Source: self-identified during build
Date: 2026-08-20

The transparent path works: cosmic-comp does composite an RGBA layer-shell
surface over `cosmic-bg`'s wallpaper, panels and `backdrop-filter` blur
included. Confirmed live on both outputs.

Not adopted — it does not read well against the current
`Wallpaper_Set_YT_Black_Hole_Universe` imagery, which is too busy behind the
panels. Reverted to `transparent: false` (CSS gradient background). The code
path stays in place and is a one-key flip in `config.json` plus a host
restart, so it is worth retrying against a calmer wallpaper.

Follow-up if revisited: a subtle scrim behind text would keep the wallpaper
visible while making type legible over busy images.

Started at: 2026-08-20T15:32:26-07:00
Ended at: 2026-08-20T15:34:08-07:00
Time elapsed: 1m 42s

## 1. Service units are written but not enabled

Status: closed
Source: Gatekeeper Protocol (sibling project `../claude/CLAUDE.md`)
Date: 2026-08-20

`systemd/desktop-dashboard-{serve,host}.service` linked via `install.sh` and
enabled with `systemctl --user enable --now` on explicit user approval. Both
are `enabled` against `graphical-session.target` and confirmed
`active (running)`, with the host mapping both outputs on the BOTTOM layer.
The dashboard now survives logout.

Started at: 2026-08-20T15:32:26-07:00
Ended at: 2026-08-20T15:33:00-07:00
Time elapsed: 34s
