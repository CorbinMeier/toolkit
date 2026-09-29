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
- No runtime network from `web/` (Tailwind vendored); no sudo/new packages; not a timer-driven wallpaper image or widget framework.

### Surfaces
#### Wallpaper surface - `bin/dashd-host`, one per output [live]
- BOTTOM layer-shell: above `cosmic-bg`, below every normal window.
- Pointer-interactive (`display.interactive`, default true); keyboard mode stays
  `NONE` so a click never steals focus (#36).
- Right-click opens our own themed context menu: Profiles / Cards / Settings (#66, #94).
- Each monitor is detected by `dashd-host` (unique key per output, #93) and lays out
  its own `outputs.<key>.cards`; profiles snapshot every monitor's cards (#94).
#### System panel, left half [live]
- CPU / MEM / BAT, Storage, Network as `.sys-grid` rows with three-tier step graphs
  for CPU and Memory (#12, #17, #57, #58, #59).
- Music: MPRIS via `playerctl`, with transport controls; idle shows a placeholder
  rather than hiding (#26, #65).
- Projects: per-repo GitHub stats, paginated (#62, #64); Sessions: since-boot logins (#71).
#### Forecast panel, right half [live]
- Current conditions, hourly, week, sun/moon - one panel (#10), beside a Calendar
  month grid (#22). AQI kv-grid row (#79) and an NWS alert banner (#80, distinct
  from `weather.alert`) - both hidden entirely when there's nothing to show.
#### Log panel, below both [live]
- Matched and highlighted lines only, never a full tail (#28, #72).
#### Mobile control surface - `bin/dashd-control` [built, unit not enabled]
- QR-paired, token-gated config for units/display/logs (#30).

### External services
- Open-Meteo: no key, no auth. Forecast, plus `geocoding-api.open-meteo.com` to resolve
  a city (#77), proxied server-side - `web/` never calls out. Default Chico, CA.
- GitHub through `gh` (`lib/ghstats.py`): the authenticated user plus every org they
  belong to, `totalCount` sub-selections only.
- Local only, degrading to `available: False` when absent: `playerctl` (MPRIS),
  `journalctl`, systemd-logind, `lsblk`, `/sys/class/power_supply`.
- Tracker: GitHub, `CorbinMeier/desktop`.

## Decisions

`Over:` is off the table: raise the limit, don't switch. Superseded entries move to `.corbin/history.md`.

### Surface
- **BOTTOM layer** - 2026-08-20
  - Over: BACKGROUND
  - Why: `cosmic-bg` owns BACKGROUND and draws over it, so the surface is invisible there
- **Input region covers the whole surface** - 2026-08-27
  - Over: scoped per-widget click-through rects, and the JS-to-Python rect bridge they need
  - Why: user - "I don't use my desktop (nothing on my desktop)" (#36)
- **Transparent is a live Tint choice, off by default (#94)** - 2026-09-28
  - Over: nothing removed; narrows the entry below
  - Why: user - "there should be a transparent tint which removes it completely"
- **`display.transparent` = false by default; tint is a real time-of-day color, not "barely present"** - 2026-09-21
  - Over: RGBA/backdrop-blur over `cosmic-bg` (#89); slate-only "barely present" palette (#78/#88, retired by #90)
  - Why: user - "still transparent... shouldn't be" (#89); "doesn't feel like midnight" (#90) - panels stay readable via their own 88% opacity
  - Supersedes: "= true, with a time-of-day slate tint to come" (2026-09-21, #78)

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
- **Pressure/cloud stay unrendered** - 2026-09-21
  - Over: plain rows, or pressure with a trend (needs `dashd-collect` reading
    `weather.json`, a coupling #14 avoided); cloud duplicates desc/code (#84)

### Process
- **Work lands on `dev`; `main` is the integration branch** - 2026-09-21
  - Over: committing straight to `main` - user - "it's just a standard we follow";
    #77 found `main` missing the Scope its contract needed (#86)

### Data
- **`config.json` is tracked definitions; `data/config.local.json` holds this
  machine's selections** - 2026-09-01
  - Over: one writable `config.json`, and a full snapshot instead of just the overrides
  - Why: user - selections like the theme change often; a snapshot would freeze defaults (#74)
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
- TBD: #36/#64/#65, #66, #71/#72 and #77 are merged but not eyeballed live.

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
