# Issues

Newest first. No GitHub remote on this project, so this file is the tracker.

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

## 2. `display.transparent: true` has never been visually verified

Status: open
Source: self-identified during build
Date: 2026-08-20

The transparent path is implemented (`win.set_app_paintable`, RGBA visual,
`WebView.set_background_color` alpha 0, `body.transparent` CSS) and was the
mode the user said they wanted available, but every verification so far ran
with `transparent: false`. Unverified specifically: whether cosmic-comp
composites an RGBA layer-shell surface over `cosmic-bg`'s wallpaper without
the panels' `backdrop-filter` blur turning muddy or black.

Unblocks: flip `display.transparent` to `true`, restart the host, look.

## 1. Service units are written but not enabled

Status: open
Source: Gatekeeper Protocol (sibling project `../claude/CLAUDE.md`)
Date: 2026-08-20

`systemd/desktop-dashboard-{serve,host}.service` exist and `install.sh` links
them, but nothing is enabled or started at login — enabling is the user's
call, never autonomous. The dashboard is currently running from two manually
launched foreground processes, so it will not survive a logout.

Unblocks: user runs
`systemctl --user enable --now desktop-dashboard-serve desktop-dashboard-host`.
