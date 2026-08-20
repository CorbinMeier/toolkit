# Desktop Dashboard

Live HTML rendered as the desktop wallpaper on COSMIC/Wayland. Two processes:
`bin/dashd-serve` (loopback data + static server, :4320) and `bin/dashd-host`
(one WebKit layer-shell surface per monitor pointing at it). Page is plain
HTML + Tailwind v4 browser build, no build step — edit `web/`, then
`systemctl --user reload desktop-dashboard-host` (SIGHUP) to repaint.

Units are written but NOT enabled — never run `systemctl --user enable/start`
autonomously, present and wait for yes/no (same Gatekeeper Protocol as the
sibling `../claude` project).

## Environment (verified 2026-08-20, Pop!_OS 24.04 / COSMIC)

- Session: Wayland, `XDG_CURRENT_DESKTOP=COSMIC`, compositor `cosmic-comp`.
- Outputs are reported by GDK in **logical** px, not physical: the laptop
  eDP-1 is 1920x1200 physical but presents as `1440x900 @0,490` with
  `scale=1`, and its `get_model()` is `"Unknown"` (hence the `monitor-N`
  fallback in `monitor_name()`). The Dell is `2560x1080 @1440,0`.
- Combined framebuffer is 4000x1390 with dead zones, because the two outputs
  are offset — a full-desktop screenshot has blank regions that are *not* a
  rendering bug.

## The layer choice is the whole trick

`cosmic-bg` owns the **BACKGROUND** layer. A layer-shell surface placed there
maps fine, paints fine, and receives frame callbacks at ~30fps — but
`cosmic-bg` draws over it, so it is *invisible*. This wasted a diagnostic
cycle: the frozen "clock" observed on the desktop was `cosmic-bg` still
showing a static PNG, not our stalled surface.

Use **BOTTOM**: above `cosmic-bg`, below every normal window. That is also
what makes `display.transparent` useful — a transparent page composites over
whatever photo wallpaper `cosmic-bg` is still cycling underneath.

Frame callbacks were measured on both layers via a `requestAnimationFrame`
vs `setInterval` counter probe (rAF only advances if the compositor is
actually scheduling frames): steady 30fps on each. Rendering was never the
problem.

## Non-obvious gotchas hit during the build

- **PyGObject version pinning**: `gi.require_version("Gdk", "3.0")` must come
  *before* the `from gi.repository import ...` line, and before anything
  pulls in Gtk 4. Without it Gdk 4.0 loads first and the import dies with
  `Requiring namespace 'Gdk' version '3.0', but '4.0' is already loaded`.
  There is no `gtk4-layer-shell` in the Ubuntu 24.04 repos, so GTK3 +
  `GtkLayerShell-0.1` + `WebKit2-4.1` is the only combination available —
  all three typelibs already ship on this machine, no install needed.
- **`animation-fill-mode: both` + a stagger delay is a blank-wallpaper
  hazard.** It parks each panel at `opacity:0` until the animation runs, so
  anything that throttles or disables animation leaves the desktop empty
  forever. `apply()` now always strips the class back off after 1.6s, and
  `?static=1` skips it entirely. Visibility must never depend on an
  animation completing.
- **`preserveAspectRatio="none"` distorts SVG text and markers**, not just
  the path — labels came out stretched and dots became ellipses. The
  sparkline instead sets its `viewBox` from the measured pixel box at render
  time so 1 unit == 1 px, which is why a `resize` listener has to re-`apply()`.
- `psutil.sensors_battery()` returns `power_plugged`, not `plugged_in`.
- Headless Chrome renders the page for snapshot verification, but virtual
  time never advances CSS animations — always add `?static=1` for those, or
  you screenshot an all-transparent page and think the CSS is broken.

## Data

Open-Meteo, no API key, no auth. Location pinned to Chico, CA
(39.72849, -121.83748) in `config.json`; IP geolocation was tried and
returned nulls, so it is not used. Weather is cached to `data/weather.json`
and refetched past its TTL; a failed fetch serves the stale cache with
`stale: true` rather than raising, because old weather beats an error on a
wallpaper. `data/extra.json` is merged into `/api/state` verbatim, so a cron
job can add panels without touching the server.
