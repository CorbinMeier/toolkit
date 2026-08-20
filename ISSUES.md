# Issues

Newest first. No GitHub remote on this project, so this file is the tracker.

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
