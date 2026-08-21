# Desktop Dashboard

Live HTML rendered as the desktop wallpaper on COSMIC/Wayland — weather,
forecast, sun/moon, system stats (with SQLite-backed history for cpu/mem
trends, CPU and Memory each rendering a compact three-tier step-line graph
row; battery has no graphs, #12, #17). Not a
wallpaper image on a timer: a real WebKit view with CSS animation and JS,
one surface per monitor, sitting below the windows. No digital clock,
date, or location readout -- the user's own system already shows all
three (#5, #9). Two halves: System (left) | Forecast — current
conditions, hourly, week, sun/moon, all one panel (right, #10).

Three processes:

- `bin/dashd-serve` — loopback static + data server on `127.0.0.1:4320`
- `bin/dashd-collect` — standalone historical-metrics sampler; the only
  writer of `data/metrics.db` (#14)
- `bin/dashd-host` — GTK3 + gtk-layer-shell + WebKit2 surfaces, one per output

All three run as **enabled** systemd user units (see *Current state* below).

`DESIGN.md` (plus its `.impeccable/design.json` sidecar) documents the
visual system — "The Night Ops HUD": the teal/gold/red accent triad,
notched-panel corners, flat-plus-glow elevation, and table-aligned metric
rows. Read it before any visual change; it's the normative reference, not
this file (#13).

## Layout

```
bin/dashd-serve     HTTP server; /api/state merges weather+sys+extra (and
                    exposes config.metrics_retain_hours so the frontend's
                    history fetch window tracks it), /api/history reads
                    lib/metrics.py's stored samples -- read-only against
                    data/metrics.db, it never writes to it (#14). sys.disks
                    comes from lsblk (sysinfo.disk_tree()), not just
                    psutil's mounted-partition view; battery gets a real
                    charging flag from /sys/class/power_supply (see gotchas)
bin/dashd-collect   standalone timer loop (config.metrics_sample_seconds);
                    the sole writer of data/metrics.db, independent of
                    whether any view or dashd-serve poll is happening (#14)
bin/dashd-host      layer-shell surfaces; --list prints monitor names
lib/sysinfo.py      shared psutil sampling (system_stats() for dashd-serve's
                    full live snapshot, sample_stats() for dashd-collect's
                    smaller persisted row) so the two processes can't
                    measure a metric two different ways
lib/metrics.py      SQLite historical-metrics store (cpu/mem/battery_pct);
                    self-migrates ALTER TABLE ADD COLUMN for an older DB
web/index.html      panel structure (Tailwind utility classes); System
                    renders as three compact <table>s (not w-full -- sized
                    to content, #17), cpu/mem/bat led | icon | label | value
                    (status LED leads the row), Storage/Network
                    icon|label|value|chart-or-badge. CPU/Memory each add one
                    row of three side-by-side step-line graphs (30S/5M/30M,
                    corner-labeled tier name + value range) instead of three
                    stacked rows; Battery has none (#17)
web/app.js          all rendering; pure function of last good state.
                    Two independent trend sources feed the step charts: an
                    in-memory ring buffer (this session's own /api/state
                    polls, for the 30s CPU/Memory tier and the Network
                    chart) and trendHistory (/api/history rows, for every
                    tier >= 5 minutes)
web/vendor/         Tailwind v4 browser build, vendored (no network at runtime)
config.json         location, units, port, display layer, per-output overrides,
                    metrics sample/retain interval
data/               weather.json cache, extra.json, metrics.db (all gitignored)
scripts/smoke.py    the "build" stage — end-to-end render assertions
tests/test_dashd.py unittest suite (hermetic, no network)
systemd/            the two user units
ISSUES.md           legacy tracker, superseded by GitHub issues (origin →
                    CorbinMeier/desktop, see #21); kept only for history
```

## Running and editing

```bash
systemctl --user status desktop-dashboard-serve desktop-dashboard-collect desktop-dashboard-host
systemctl --user reload desktop-dashboard-host   # SIGHUP → reload all views
journalctl --user -u desktop-dashboard-host -n 30
```

`web/` is plain HTML/CSS/JS with Tailwind compiled in the browser — **there is
no build step and nothing to bundle**. Edit, then `reload`.

`scripts/update-desktop.sh` (`pnpm run deploy`) automates that: runs the §6
lint→test→build chain and, only if every stage is green, `reload`s
`desktop-dashboard-host` so the change actually shows up on the wallpaper. A
red chain exits before touching the live surfaces. It never starts/enables a
unit — that stays behind the Gatekeeper Protocol.

Preview without touching the desktop. `?static=1` is required — headless
Chrome's virtual clock never completes a CSS animation, so without it you
screenshot an all-transparent page and conclude the CSS is broken:

```bash
google-chrome --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --window-size=2560,1080 --virtual-time-budget=9000 --screenshot=out.png \
  'http://127.0.0.1:4320/index.html?static=1&output=DELL%20U2913WM'
```

Real desktop capture (no `grim` on this box):
`cosmic-screenshot --interactive=false --modal=false --notify=false -s <dir>`.
It grabs the whole 4000x1390 multi-output framebuffer, which has blank dead
zones because the outputs are offset — that is not a rendering bug. It also
cannot show the dashboard while windows cover it, since the surface is
correctly *below* windows; ask the user to look instead.

## Verification chain (lint → test → build)

```bash
npx eslint web/app.js eslint.config.js   # or: pnpm run lint:js
.venv/bin/ruff check .                   # or: pnpm run lint:py
python3 -m unittest discover -s tests    # or: pnpm run test
python3 scripts/smoke.py                 # or: pnpm run build
```

`scripts/smoke.py` is the build stage: nothing compiles here, so the
equivalent is proving the page renders — it starts the server if needed,
asserts `/api/state` is well-formed, then renders in headless Chrome and
asserts the DOM actually populated. It catches what lint and unit tests
cannot: a JS exception before first paint, a broken Tailwind bundle, a
renamed state key silently blanking a panel. `--no-browser` skips the Chrome
pass.

Fresh clone needs the dev tooling restored (both are gitignored):

```bash
python3 -m venv .venv && .venv/bin/pip install ruff
pnpm install
```

**Do not run the app from `.venv`.** The venv exists only for `ruff`; it has
no `psutil`, so `.venv/bin/python bin/dashd-serve` dies with
`ModuleNotFoundError`. Runtime is system `/usr/bin/python3` plus the system
`python3-psutil` — which is what both unit files invoke.

`ruff.toml` carries `extend-include = ["bin/dashd-*"]`. Without it ruff finds
"no Python files", prints *All checks passed*, and exits 0 having checked
nothing — a silently-green lint stage. Keep it.

## Environment (verified 2026-08-20, Pop!_OS 24.04 / COSMIC)

- Wayland, `XDG_CURRENT_DESKTOP=COSMIC`, compositor `cosmic-comp`. No X11
  wallpaper tool applies (feh/hsetroot/xwinwrap/conky are all dead ends), and
  `cosmic-bg` itself only accepts image files.
- Stack is GTK3 + `GtkLayerShell-0.1` + `WebKit2-4.1`. There is **no**
  `gtk4-layer-shell` in the Ubuntu 24.04 repos, so GTK4 is not an option.
  All three typelibs plus `psutil` and Chrome already ship on this machine —
  **no sudo was needed for any of this**, and none should be.
- Outputs are reported by GDK in **logical** px: the laptop eDP-1 is 1920x1200
  physical but presents as `1440x900 @0,490` with `scale=1`, and its
  `get_model()` is `"Unknown"` — hence the `monitor-N` fallback in
  `monitor_name()`. The Dell is `2560x1080 @1440,0`.

## The layer choice is the whole trick

`cosmic-bg` owns the **BACKGROUND** layer. A layer-shell surface placed there
maps fine, paints fine, and receives frame callbacks at ~30fps — but
`cosmic-bg` draws over it, so it is **invisible**. This cost a full diagnostic
cycle: the "frozen clock" seen on the desktop was `cosmic-bg` still showing a
static PNG, not our stalled surface.

Use **BOTTOM**: above `cosmic-bg`, below every normal window. A test in
`tests/test_dashd.py` asserts the config never regresses to `BACKGROUND`.

Frame scheduling was measured with a `requestAnimationFrame` vs `setInterval`
counter probe — rAF only advances if the compositor is actually scheduling
frames — at a steady 30fps on both layers. **Rendering was never the problem;**
if something looks frozen again, suspect z-order or a stale process, not the
render loop.

## Non-obvious gotchas

- **A `td`-level `pl-[...]`/`pr-[...]` Tailwind utility class silently does
  nothing in the System tables.** `.sys-table td{padding:0.32vmin 0}` (a
  class+element selector) has *higher* specificity than a single-class
  Tailwind utility like `.pl-\[0\.9em\]{padding-left:0.9em}`, so the
  shorthand's `0` left/right padding always wins regardless of source
  order or which class was added later via JS. This was invisible for
  years because the old percentage-based `<colgroup>` widths gave every
  column enough natural separation on its own; it only surfaced once
  tables became compact/content-sized (#17), where a longer label
  ("RECOVERY") would otherwise sit flush against its value with zero gap.
  Fix/pattern: set `element.style.paddingLeft`/`paddingRight` directly
  instead of a `pl-*`/`pr-*` class on any `<td>` in these tables — inline
  styles always win regardless of the shorthand rule's specificity.
- **A `<table>` that is a *direct* child of a `flex flex-col` container
  inherits `align-items: stretch` and gets forced to the container's full
  cross-axis width, even with no `w-full` class anywhere** -- this silently
  undid #17's "compact, sized to its own content" table sizing for exactly
  one table. CPU/MEM/BAT's `<table class="sys-table">` sits directly inside
  `.panel` (itself `flex flex-col`), so it stretched full width; its
  unconstrained label/value/tail `<colgroup>` columns then absorbed that
  extra width, spreading the row edge-to-edge with `value`/`tail`'s
  right-aligned content hugging the far right (#33). Storage/Network never
  hit this because their tables sit one level deeper inside a plain wrapper
  `<div>` -- the div is the flex item and gets stretched, but the table
  inside it is an ordinary block box sized by normal (shrink-to-fit) table
  layout. Fix: `.sys-table{width:fit-content}` overrides the inherited
  stretch regardless of DOM nesting -- applied table-wide since it's a
  no-op for tables that were already content-sized. A second contributing
  factor: CPU/MEM's 30-minute trend graph used to be a colspan row sharing
  the same table's value/tail columns; its internal `width:100%` chart
  compounded the blowout, so it was also pulled out into its own
  `#sysgraphs` sibling container, decoupled from the value table's column
  widths entirely.
- **PyGObject version pinning**: `gi.require_version("Gdk", "3.0")` must come
  before the `from gi.repository import ...` line and before anything pulls in
  Gtk 4, or the import dies with `Requiring namespace 'Gdk' version '3.0', but
  '4.0' is already loaded`.
- **`animation-fill-mode: both` + a stagger delay is a blank-wallpaper
  hazard.** It parks each panel at `opacity:0` until the animation runs, so
  anything throttling or disabling animation leaves the desktop empty forever.
  `apply()` always strips the class back off after 1.6s. Visibility must never
  depend on an animation completing.
- **`preserveAspectRatio="none"` distorts SVG text and markers**, not just
  the path — labels stretch and dots become ellipses. The weather hourly
  sparkline that used to warrant this note is gone (see #8, `renderHourly`
  replaced it with a plain list), but `app.js`'s `miniSpark()` deliberately
  uses `preserveAspectRatio="none"` anyway: it draws a bare path with no
  text/marker children, so there's nothing for the distortion to hit, and
  stretching it to exactly fill a fixed small box is the point for a
  compact inline trend line. Any *new* SVG with text or markers should
  default to measuring its real pixel box and setting `viewBox` from that
  instead (1 unit == 1 px), same as the removed sparkline did, re-triggered
  from the `resize` listener in `apply()`.
- **The vendored Tailwind v4 browser build only emits an `@theme` token onto
  `:root` when its scanner sees the token's name inside an HTML class
  attribute** — a plain utility class (`text-warm`), or the bare `var(...)`
  name spelled out inside `[...]` arbitrary-value syntax (`border-[var(--x)]`
  counts; a `var()` reference buried in a separate `<style>` tag or in
  `app.js` does not, since the scanner never parses those). `--color-warm`
  was for a long time emitted only as a side effect of the offline banner
  carrying `text-warm`; recoloring that banner (2026-08-20, cyberpunk
  restyle) silently broke every *other* `var(--color-warm)` consumer
  app-wide (sun icon, hot-threshold bars, sparkline gradient, sun arc) until
  caught by a live `getComputedStyle` check. Fix/pattern: any color token
  consumed only via plain `var()` — in this stylesheet or in `app.js` — must
  also be restated in the plain (non-`text/tailwindcss`) `<style>` block's
  own `:root{}` rule, which the browser parses unconditionally; `@theme`
  keeps its own copy purely so the Tailwind utility classes that do exist in
  the markup keep compiling. Both blocks in `web/index.html` now carry the
  full palette for exactly this reason — don't de-duplicate them.
- `psutil.sensors_battery()` returns `power_plugged`, not `plugged_in`, and
  it's AC-present, not charge state — a full battery still left plugged in
  reports `power_plugged: True` with no current flowing. The battery
  utilization ring's charging state needs the real thing, so
  `battery_charging()` reads
  `/sys/class/power_supply/BAT*/status` (`"Charging"` vs `"Full"` /
  `"Discharging"` / `"Not charging"`) and only falls back to
  `power_plugged` when no `BAT*` node exists at all.
- **`lsblk -J` keys are lowercased column names, including the `%`**:
  `FSUSE%` becomes JSON key `"fsuse%"`, not `"FSUSE%"` or `"fsuse_pct"`.
  Whole-disk rows (`type: "disk"`) and swap's `type: "crypt"` child carry no
  usable `FSUSE%`; `disk_tree()` only emits `type: "part"` rows that have a
  mountpoint or an `FSUSE%`, which is what keeps a bare, dataless swap slot
  (`nvme0n1p4` on this machine — the actual swap usage lives on its `crypt`
  child, not the partition itself) from showing up as a noise row.
- `pkill -f dashd-serve` **kills the shell running it**, because the pattern
  matches that shell's own command line. Resolve PIDs with `pgrep` and skip
  `$$`, or use `systemctl --user stop`.
- **`CREATE TABLE IF NOT EXISTS` does not add a column to an existing
  table.** `data/metrics.db` is gitignored and had already been running in
  production (cpu/mem/net only) by the time `battery_pct` was added to
  `metrics.COLUMNS` (#12) — the old on-disk table would otherwise 500 on
  first `INSERT`/`SELECT` referencing it. `metrics._connect()` now diffs
  `PRAGMA table_info(samples)` against `COLUMNS` and `ALTER TABLE ADD
  COLUMN`s whatever's missing, every connection. Any *future* `COLUMNS`
  addition needs nothing more than adding the name to the tuple — the
  migration is generic — but removing/renaming a column would need real
  thought (this only ever adds).
- **A 30-second step-line chart cannot be built from `/api/history`** —
  samples land there roughly every `metrics_sample_seconds` (30s default),
  so a 30-second window is 0-1 points. `web/app.js` keeps a separate
  client-side ring buffer (`ring`, fed from every `/api/state` poll, ~5s)
  for exactly that tier, plus the Network chart (throughput bursts are
  brief enough that even 30s DB resolution would flatten them). Anything
  5 minutes or longer reads from `trendHistory` (`/api/history`) instead,
  since by then the DB's resolution is fine *and* it survives a page
  reload, which the in-memory ring buffer does not.

## Data

Open-Meteo, no API key, no auth. Location pinned to Chico, CA
(39.72849, -121.83748); IP geolocation was tried and returned nulls, so it is
not used. Weather caches to `data/weather.json` and refetches past its TTL; a
failed fetch serves the stale cache with `stale: true` rather than raising,
because old weather beats an error on a wallpaper. The forecast `zip()`s use
`strict=True` on purpose — misaligned arrays would pair one day's temperature
with another day's icon, and serving the cache is better than showing that.

`data/extra.json` is merged into `/api/state` verbatim as `extra`, so cron or
any other script can add panels without touching the server.

## Conventions

- **Never run `systemctl --user enable/start` autonomously** — present and
  wait for yes/no (Gatekeeper Protocol, same as the sibling `../claude`
  project). The current units were enabled on explicit approval.
- GitHub (`gh issue`) is the tracker now — `origin` points at
  `CorbinMeier/desktop` (see #21). File work as a `gh` issue before fixing
  it, bracket work with `gh issue comment` `Started at:` / `Ended at:` /
  `Time elapsed:` lines pulled from `date`. `ISSUES.md` is legacy history
  only — do not add new entries to it.
- The human is the final verification step for anything visual. Verify
  functionally first (smoke stage, curl, journal), then ask them to look —
  a screenshot cannot see past their windows.

## Current state (2026-08-20)

- All three units (`serve`/`collect`/`host`) **enabled and active** against
  `graphical-session.target`; the dashboard survives logout.
  `desktop-dashboard-collect` (#14) was linked, approved, and
  `enable --now`'d in-session — Gatekeeper Protocol satisfied, same as the
  original two units (`ISSUES.md` #1).
- `display.transparent` is **false**. The transparent path was verified to
  work — cosmic-comp does composite an RGBA layer-shell surface over
  `cosmic-bg`'s wallpaper, backdrop blur included — but it reads poorly
  against the current black-hole wallpaper, so it was reverted. One-key flip
  in `config.json` plus a host restart if revisited against calmer imagery.
- #17 (2026-08-20): System panel polish. Battery's tier graphs (30M/4H/24H)
  removed entirely -- not needed, its LED already signals charge state.
  CPU/MEM's LED is now traffic-light banded off percentage (green below
  70%, gold 70-89%, red from 90%, flashing from 95%) instead of a fixed
  hue -- Standby Green's first wired use in the system. Storage icons
  became a floppy-disk glyph. The LED moved to lead the row, before the
  icon (was: trailing the label, #16) -- the most immediate signal reads
  first. CPU/Memory's three time-tier charts (30S/5M/30M) collapsed from
  three stacked full-width rows into one row of three small side-by-side
  graphs, each with its own corner labels (tier name left, value range
  right). All three System tables dropped `w-full` -- compact, sized to
  their own content instead of stretched to fill the panel (this also
  surfaced a real `pl-*`/`pr-*`-on-`<td>` specificity bug, see gotchas).
  See DESIGN.md's Utilization LED and Compact Tier Graph components.
- #16 (2026-08-20): shrank the CPU/MEM/BAT utilization indicator from a
  large background ring to a small `0.85em` LED sitting after the row's
  label text, close to that text's own cap-height. Fill mechanic switched
  from scale (grows) to opacity (brightens) -- a real LED doesn't grow --
  and glow blur/spread were capped small so the halo stays proportionate
  instead of blooming into a blob. See DESIGN.md's Utilization LED
  component (supersedes #15's ring version).
- #15 (2026-08-20): CPU/MEM/BAT icons briefly sat inside a two-layer
  utilization ring (deep-red base + a glowing fill that scaled with
  percentage; battery used a discrete charging/dim/low-flash state) --
  replaced the CHRG text badge, which is removed. Ring reworked into a
  small LED by #16 after user feedback that it read as too heavy.
- #14 (2026-08-20): split historical-metrics collection out of dashd-serve
  into a standalone `bin/dashd-collect`, the sole writer of
  `data/metrics.db` now. Shared cpu/mem/battery/net sampling moved into
  `lib/sysinfo.py` so dashd-serve's live `/api/state` numbers and
  dashd-collect's persisted rows can't measure a metric two different
  ways. dashd-serve's "current value" numbers are unaffected — still the
  same fast live psutil poll as before, unrelated to this split.
- Open: `ISSUES.md` #3 — `outputs.<name>.layout` reaches
  `document.documentElement.dataset.layout` but no CSS keys off
  `[data-layout]`, so both monitors render the same two-column grid (System |
  Forecast, since #10). Looks right on both; just not differentiated.
- `display.safe_area_top`/`safe_area_bottom` (added for #6) default to 48px
  each, a guess at typical COSMIC top/bottom panel height -- not visually
  confirmed against this machine's actual panels. Tune in `config.json` if
  it's off; no code change needed.
- #9-#11 (2026-08-20): removed the date/location readout, consolidated
  weather/hourly/week/sun-moon into one right-half "Forecast" panel, and
  rebuilt System as an actual `<table>` (icon | label | value | histograph)
  with lsblk-sourced per-partition disk rows and a CHRG battery indicator.
  Verified functionally (smoke stage against an isolated worktree-local
  server) but not visually — per the user's "no more verifying (I can do
  that)" instruction, this round is theirs to eyeball on the live desktop.
- #12 (2026-08-20): CPU/Memory/Battery now render as a step-line-chart
  stack (30s/5m/30m for CPU+Memory, 30m/4h/24h for Battery), Storage and
  Network split into their own visually separated areas within System
  (Network gets a dual-line down/up step chart, no DB storage -- by
  design, see gotchas). `battery_pct` added to `lib/metrics.py`'s
  `COLUMNS` with a self-migrating `ALTER TABLE` for the already-running
  production DB. Same as #9-#11: functionally verified only, not visually.
