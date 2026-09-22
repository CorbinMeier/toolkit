# Desktop Dashboard - live HTML rendered as the COSMIC/Wayland desktop wallpaper

- Stack: system `/usr/bin/python3` + `python3-psutil` at runtime (never `.venv`, which
  is ruff-only), GTK3 + `GtkLayerShell-0.1` + `WebKit2-4.1`, and plain HTML/CSS/JS with
  a vendored Tailwind v4 browser build. **No build step.**
- Visual system: `DESIGN.md` ("The Night Ops HUD") and its `.impeccable/design.json`
  sidecar. Normative for any visual change, ahead of this file (#13).
- Topic rules load with the code they describe, in `.claude/rules/`: `host-surface.md`,
  `services.md`, `lib-sources.md`, `lib-storage-config.md`, `web-panels.md`,
  `web-styling.md`, `verification.md`.
- Issue-by-issue history, removed panels and superseded decisions: `.corbin/history.md`.

@scripts/README.md

Four processes:

- `bin/dashd-serve` — loopback static + data server on `127.0.0.1:4320`
- `bin/dashd-collect` — standalone historical-metrics sampler; the only
  writer of `data/metrics.db` (#14)
- `bin/dashd-host` — GTK3 + gtk-layer-shell + WebKit2 surfaces, one per output.
  Since #36 the surface **accepts pointer input** (`display.interactive`,
  default true): the empty input region is only applied when that flag is
  false. Keyboard mode stays `NONE` either way, so clicking the dashboard
  never steals focus from a real window
- `bin/dashd-control` — LAN-reachable mobile config/control surface, paired
  by scanning a QR code shown at the desktop (`--print-qr`, or the
  loopback-only `/api/control/qr.svg`); every other route is token-gated
  (#30)

The first three run as **enabled** systemd user units (see
`.corbin/history.md`); `dashd-control`'s unit exists
(`systemd/desktop-dashboard-control.service`) but is **not enabled** — new
units wait for the Gatekeeper Protocol (see *Conventions*), same as the
other three did before their own approval.

`DESIGN.md` (plus its `.impeccable/design.json` sidecar) documents the
visual system — "The Night Ops HUD": the teal/gold/red accent triad,
notched-panel corners, flat-plus-glow elevation, and table-aligned metric
rows. Read it before any visual change; it's the normative reference, not
this file (#13).

## Scope

Status: approved 2026-09-21

### Non-goals
- No digital clock, date, or standalone location line: the system's own panel shows
  all three (#5, #9). The Weather header may name the city whose forecast it shows.
- No Devices panel (removed at #56), and no Tasks or Schedule panel in the frontend
  (deleted at 583229d). Their server-side feeds deliberately stay (#63).
- No network access at runtime from `web/` - Tailwind is vendored for this reason.
- No sudo and no new system packages: everything used already ships on this machine.
- Not a wallpaper image on a timer, and not a general desktop widget framework.

### Surfaces
#### Wallpaper surface - `bin/dashd-host`, one per output [live]
- BOTTOM layer-shell: above `cosmic-bg`, below every normal window.
- Pointer-interactive (`display.interactive`, default true); keyboard mode stays `NONE`,
  so a click never steals focus (#36).
- Right-click opens our own themed context menu: Reload, plus a theme row per
  `display._theme_options` (#66).
#### System panel, left half [live]
- CPU / MEM / BAT, Storage, Network as `.sys-grid` rows with three-tier step graphs
  for CPU and Memory (#12, #17, #57, #58, #59).
- Music: MPRIS via `playerctl`, with transport controls; idle shows a placeholder
  rather than hiding (#26, #65).
- Projects: per-repo GitHub issue stats, paginated (#62, #64).
- Sessions: who logged in since the last boot (#71).
#### Forecast panel, right half [live]
- Current conditions, hourly, week, sun/moon - one panel (#10).
#### Log panel, below both [live]
- Matched and highlighted lines only, never a full tail (#28, #72).
#### Mobile control surface - `bin/dashd-control` [built, unit not enabled]
- QR-paired, token-gated config for units/display/logs (#30).

### External services
- Open-Meteo: no key, no auth. Forecast, plus `geocoding-api.open-meteo.com` to
  resolve a city to coordinates (#77), proxied server-side - `web/` never calls out.
  Default Chico, CA (39.72849, -121.83748); IP geolocation returned nulls, unused.
- GitHub through `gh` (`lib/ghstats.py`): the authenticated user plus every org they
  belong to, `totalCount` sub-selections only.
- Local only, degrading to `available: False` when absent: `playerctl` (MPRIS),
  `journalctl`, systemd-logind, `lsblk`, `/sys/class/power_supply`.
- Tracker: GitHub, `CorbinMeier/desktop`.

## Decisions

Anything under `Over:` is off the table: hit a limit, raise it, don't switch.
Superseded entries move to `.corbin/history.md`.

### Surface
- **BOTTOM layer** - 2026-08-20
  - Over: BACKGROUND
  - Why: `cosmic-bg` owns BACKGROUND and draws over it, so the surface is invisible there
- **Input region covers the whole surface** - 2026-08-27
  - Over: scoped per-widget click-through rects, and the JS-to-Python rect bridge they need
  - Why: user - "I don't use my desktop (nothing on my desktop)" (#36)
- **`display.transparent` = true, with a time-of-day slate tint to come** - 2026-09-21
  - Over: an opaque surface
  - Why: cosmic-comp composites an RGBA layer-shell surface over `cosmic-bg`,
    backdrop blur included. The tint is #78
  - Supersedes: "= false" (2026-08-20), which the tracked config never carried

### Panels
- **Devices removed entirely, not hidden** - 2026-08-23
  - Over: keeping the panel behind a flag
  - Why: user - "remove devices. its basically just clutter" (#56)
- **Music never hides when idle** - 2026-08-27
  - Over: hiding the panel when nothing is playing
  - Why: user - "it should not disappear just because no media is playing, should just sit idle"
- **Projects' green is `--color-actionable`** - 2026-08-27
  - Over: reusing `--color-online`
  - Why: every theme overrides `--color-online`, which collapsed the triad to two colors

### Data
- **`config.json` is tracked definitions; `data/config.local.json` is this machine's
  selections** - 2026-09-01
  - Over: one writable `config.json`, and writing a full snapshot instead of the overrides
  - Why: user - selections like the theme change often and should not be tracked; a
    snapshot would freeze the defaults (#74)
- **OR'd indexed journal field matches** - 2026-09-01
  - Over: a system-wide `journalctl --grep`
  - Why: measured 126ms against 4.3s bounded and 60s+ unbounded, at a 5s poll (#72)
- **systemd-logind's journal for logins** - 2026-09-01
  - Over: utmp/wtmp through `last`/`who`
  - Why: utmp carries no user record on this machine, so the obvious path shows nothing (#71)

## Open questions

- TBD: #3 - `outputs.<name>.layout` reaches `data-layout` but no CSS keys off it, so
  both monitors render the same two-column grid.
- TBD: `display.safe_area_top`/`safe_area_bottom` default to 48px, a guess at COSMIC's
  panel height, never confirmed against this machine.
- TBD: #36/#64/#65, #66 and #71/#72 are merged but not yet eyeballed on the live
  desktop; the human is the final visual check.
- TBD: `data/devices.json` is a dead file left by #56 and has no `.gitignore` entry -
  delete it, or ignore it?

## Conventions

- **Never run `systemctl --user enable/start` autonomously** — present and
  wait for yes/no (Gatekeeper Protocol, same as the sibling `../claude`
  project). The current units were enabled on explicit approval.
- GitHub (`gh issue`) is the tracker — `origin` points at
  `CorbinMeier/desktop` (see #21). File work as a `gh` issue before fixing
  it, bracket work with `gh issue comment` `Started at:` / `Ended at:` /
  `Time elapsed:` lines pulled from `date`.
- The human is the final verification step for anything visual. Verify
  functionally first (smoke stage, curl, journal), then ask them to look —
  a screenshot cannot see past their windows.
