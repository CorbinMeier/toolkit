# Desktop Dashboard

Live HTML rendered as the desktop wallpaper on COSMIC/Wayland — date,
weather, forecast, sun/moon, system stats (with SQLite-backed history for
cpu/mem/net trends). Not a wallpaper image on a timer: a real WebKit view
with CSS animation and JS, one surface per monitor, sitting below the
windows. No digital clock -- the user's own system already shows the time
(#5).

Two processes:

- `bin/dashd-serve` — loopback static + data server on `127.0.0.1:4320`
- `bin/dashd-host` — GTK3 + gtk-layer-shell + WebKit2 surfaces, one per output

Both run as **enabled** systemd user units (see *Current state* below).

## Layout

```
bin/dashd-serve     HTTP server; /api/state merges weather+sys+extra,
                    /api/history serves lib/metrics.py's stored samples
bin/dashd-host      layer-shell surfaces; --list prints monitor names
lib/metrics.py      SQLite historical-metrics store (cpu/mem/net trends)
web/index.html      panel structure (Tailwind utility classes)
web/app.js          all rendering; pure function of last good state
web/vendor/         Tailwind v4 browser build, vendored (no network at runtime)
config.json         location, units, port, display layer, per-output overrides,
                    metrics sample/retain interval
data/               weather.json cache, extra.json, metrics.db (all gitignored)
scripts/smoke.py    the "build" stage — end-to-end render assertions
tests/test_dashd.py unittest suite (hermetic, no network)
systemd/            the two user units
ISSUES.md           the tracker — no GitHub remote on this project
```

## Running and editing

```bash
systemctl --user status desktop-dashboard-serve desktop-dashboard-host
systemctl --user reload desktop-dashboard-host   # SIGHUP → reload all views
journalctl --user -u desktop-dashboard-host -n 30
```

`web/` is plain HTML/CSS/JS with Tailwind compiled in the browser — **there is
no build step and nothing to bundle**. Edit, then `reload`.

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
- `psutil.sensors_battery()` returns `power_plugged`, not `plugged_in`.
- `pkill -f dashd-serve` **kills the shell running it**, because the pattern
  matches that shell's own command line. Resolve PIDs with `pgrep` and skip
  `$$`, or use `systemctl --user stop`.

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
- `ISSUES.md` is the tracker; file work there before fixing it, newest first,
  with `Started at:` / `Ended at:` / `Time elapsed:` lines pulled from `date`.
- The human is the final verification step for anything visual. Verify
  functionally first (smoke stage, curl, journal), then ask them to look —
  a screenshot cannot see past their windows.

## Current state (2026-08-20)

- Both units **enabled and active** against `graphical-session.target`; the
  dashboard survives logout.
- `display.transparent` is **false**. The transparent path was verified to
  work — cosmic-comp does composite an RGBA layer-shell surface over
  `cosmic-bg`'s wallpaper, backdrop blur included — but it reads poorly
  against the current black-hole wallpaper, so it was reverted. One-key flip
  in `config.json` plus a host restart if revisited against calmer imagery.
- Open: `ISSUES.md` #3 — `outputs.<name>.layout` reaches
  `document.documentElement.dataset.layout` but no CSS keys off
  `[data-layout]`, so both monitors render the same three-column grid. Looks
  right on both; just not differentiated. Likely next step is having the
  1440x900 laptop drop a column rather than shrinking the same grid.
- `display.safe_area_top`/`safe_area_bottom` (added for #6) default to 48px
  each, a guess at typical COSMIC top/bottom panel height -- not visually
  confirmed against this machine's actual panels. Tune in `config.json` if
  it's off; no code change needed.
