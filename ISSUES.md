# Issues

Newest first. No GitHub remote on this project, so this file is the tracker.

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
