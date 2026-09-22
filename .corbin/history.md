# Desktop Dashboard - history

Why things are the way they are: the issue-by-issue record, superseded
decisions, and removed panels. Never auto-loaded (`~/.claude/CLAUDE.md` §13);
read it when you need the reason behind a decision, not to work.

## Current state (2026-08-20)

- **#74 (2026-09-01): config.json is definitions; selections moved out.**
  User ask, raised while merging #71/#72: "This file should contain
  definitions, but selections (because I change things like the theme
  often) should be in some sort of user config that is not tracked."
  `config.json` stays tracked and is now **never written at runtime**;
  `data/config.local.json` (untracked) holds this machine's selections.
  `lib/userconf.py` deep-merges the two, local winning, and both write
  paths -- `dashd-serve`'s `POST /api/config` (#66) and `dashd-control`'s
  `POST /api/control/config` (#30) -- store only the accepted patch.
  **Only the overrides, never a full snapshot**: a snapshot would freeze
  the defaults at write time, so a later `config.json` edit would appear
  to do nothing, which is a worse failure than the one being fixed.
  `merge_patch()` became `filter_patch()` in both servers accordingly (it
  no longer mutates a config dict -- the patch itself is what persists).
  Dissolves #73 by construction, and the sandbox was tightened to match:
  `config.json` is gone from both units' `ReadWritePaths`, so systemd now
  enforces the never-written property. That also restores the crash-safe
  tmp-then-rename #69/#70 had to abandon, since `data/` is granted at
  directory level. **Both units need `daemon-reload` + a restart** for
  the tightened sandbox to take effect.

- **#71 / #72 (2026-09-01): new Sessions panel, and the Log panel can
  finally see a failed login.**
  - **#71:** `lib/sessions.py` + a Sessions panel in the left column under
    Projects -- who logged in since the last boot, when, from where, and
    for how long, with ended sessions kept so a login that ran a script and
    logged out is still visible. The premise that made this cheap: scoping
    the read with `journalctl -b` *is* the reset-at-boot behaviour, so
    there is no store, no schema, and nothing to prune. The source finding
    is the important part and is written up in gotchas -- utmp/wtmp is
    empty here, so the obvious `last`/`who` implementation would have
    silently shown nothing. Human-sessions-only is a **uid** filter, not a
    `Class=user` filter: a closed session has no logind object left to
    test, but its uid separates the same set and drops the uid-988
    cosmic-greeter row that opens at every boot. "Still logged in" is
    decided by logind's roster, never by a missing stop record -- a
    session logind no longer knows about is over at an unknown time, and
    is reported with a null duration (`?`) rather than one that grows
    forever.
  - **#72:** the Log panel's `Failed login` pattern had never once fired:
    its source was `journalctl -u ssh` and sshd isn't running here, while
    local auth failures come from cosmic-greeter/greetd/sudo across several
    units. The patterns were always right; only the source was wrong.
    Fixed with OR'd indexed field matches. The tempting fix -- a
    system-wide `--grep` -- is 30x-500x too slow for a per-poll read; see
    gotchas.
  - Verified functionally: lint/test/build chain, a `systemd-run --user`
    probe under the real unit sandbox flags, and a live check that the
    2026-09-01T15:31 greeter failure now reaches the Log feed. **Not yet
    eyeballed on the live desktop**, and note `-serve` needs a
    **restart**, not a reload, for either change to ship (see gotchas).

- **#66 (2026-08-27): custom right-click context menu.** Follow-on to #36 --
  right-click now reaches the page, so it gets our own themed menu instead
  of WebKit's default (Reload / Inspect Element / Open Frame in New Window
  ...), suppressed both client-side (`app.js`'s `contextmenu` listener
  `preventDefault()`s) and host-side (`dashd-host`'s `context-menu` signal
  handler returns `True`, belt-and-braces). Two items for now: Reload
  (`location.reload()`) and a Theme row per `display._theme_options`, the
  active one checked. `menuItems()` is data-driven per the issue's stated
  direction (a future customizable-panel-layout feature reuses the same
  list mechanism, not two hardcoded buttons). Theme persists through a new
  `POST /api/config` on `bin/dashd-serve`, an `EDITABLE`-style
  whitelist-not-sanitise route mirroring `dashd-control`'s own (only
  `display.theme` is in it) rather than routing the desktop through the
  LAN-bound control server -- writing `config.json` means the next poll
  propagates the theme to every output automatically. The menu itself
  reuses `.panel` wholesale (same notched clip-path/border/corner-tab) so
  it re-themes live, which is self-testing since re-theming is one of its
  own actions. DESIGN.md gained a Context Menu component entry and the
  Read-Only-By-Default Rule now names the menu as its explicit carve-out
  (a right-click is an interactive affordance by definition). Verified
  functionally (unit tests on `merge_patch`/`EDITABLE`, live curl against
  `/api/config` with both an accepted and a rejected patch, smoke stage);
  not yet eyeballed live -- `-serve` needs a **restart**, not a reload, for
  the new POST route to exist (see gotchas), and the WebKit-menu-popping
  side effect this issue opens with was never confirmed by an actual
  right-click on the live desktop before this fix, only inferred from the
  code path.
- **#36 / #64 / #65 (2026-08-27): the surface is interactive now.** This is
  the biggest premise change the project has had -- "the wallpaper has no
  pointer reach" was load-bearing for several design decisions, and it is
  no longer true.
  - **#36:** `bin/dashd-host` applies its empty input region only when
    `display.interactive` is false (new flag, defaults true). Nothing else
    was needed -- BOTTOM-layer surfaces always could receive pointer
    events; blanking the region was the only thing stopping them. Verified
    with a throwaway GTK probe on BOTTOM before any code changed: motion
    events and 7 clicks arrived. Per the user, the input region covers the
    **whole surface** rather than the scoped per-widget rects #36's body
    originally proposed -- "I don't use my desktop (nothing on my
    desktop)", so there is nothing underneath worth preserving
    click-through for, and the JS->Python rect bridge is unnecessary.
    Keyboard mode stays `NONE`: clicking a control does **not** pull focus
    off the foreground window.
  - **#64:** Projects paginates instead of auto-cycling. `.proj-scroll`'s
    `--proj-page` is the single source of truth -- it computes the box
    height AND is read back by `projPageSize()` for the slice, preserving
    #62's "the visible row count is one layout fact, not a magic number in
    two places" property. This retires #29's only shipped consumer; the
    auto-cycle mechanism itself stays (Music's title marquee still uses
    the `-x` variant).
  - **#65:** Music transport controls. `POST /api/music/command` is the
    first non-GET route in `bin/dashd-serve`; it validates against
    `sysinfo.MUSIC_COMMANDS` as a **whitelist**, so a request body can
    never reach `playerctl`'s argv as anything but one of three literals.
  - **DESIGN.md was rewritten where it asserted the opposite** -- it flatly
    said "there are no buttons" and "Don't add an interactive affordance".
    Replaced with a new Control Button component (`.ctl-btn`) and a
    Read-Only-By-Default Rule: input is allowed but exceptional, a control
    must do what a passive display cannot, and status elements stay
    read-only. Do not treat the old text as still normative.
  - **Not yet eyeballed live.** Verified via the lint/test/build chain and
    a static headless render; whether the controls feel right at wallpaper
    distance is the user's call. `-serve` needs a **restart**, not a
    reload, for the POST route to exist (see gotchas).
- #63 (2026-08-27): documentation-only correction, no rendering change.
  This file had gone on describing the Tasks (#25) and Schedule (#23)
  panels as if they shipped; both were actually deleted from `web/` at
  583229d (2026-08-21) and never returned -- `scripts/smoke.py` had the
  truth in a comment the whole time, this file didn't. Intro paragraph,
  the `web/index.html` Layout entry, and the #25/#23 *Current state*
  entries now say so. The split that made this confusing is worth
  remembering: **the frontend went away, the backend did not.**
  `load_tasks()` still serves `tasks` and `extra.schedule` still passes
  through, both unrendered. Decided to KEEP them rather than finish the
  removal -- harmless (a missing file isn't an error), source-agnostic,
  already asserted by the smoke stage, and they're exactly what a future
  Tasks panel would need. Ripping them out is a code change with test
  fallout, so it wants its own issue and its own sign-off, not a
  drive-by during a doc fix.
- #62 (2026-08-27): new Projects panel -- per-repo GitHub issue stats, in
  the left column under System. Three counts per repo, colored by what they
  ask of the user: RED (`--color-crimson`) = open issues labeled
  `ai-blocked` (waiting on a human), GREEN (`--color-actionable`) =
  actionable = `open - ai_blocked`, VIOLET (`--color-done`) = closed. The
  last two are new palette tokens and no theme overrides them, deliberately
  -- same "carries meaning, not chrome" reasoning as `--color-crimson`.
  **The green is not `--color-online`**: that reuse looked obvious and was
  wrong, because every theme overrides it (retro_orange sets it to
  `#ff9a2e`), so the "green" column rendered amber under the active theme
  and collapsed the triad to two colors. Caught by eyeballing the static
  render, not by any test -- a status color a theme can recolor is not a
  status color. Reuses System's cyan *panel* accent rather than adding a
  fourth, same call #25 made.
  Sort is blocked desc -> actionable desc -> newest push. Plain recency was
  the initial ask and the real data disproved it before any code was
  written: it floats 1-issue repos above a 37-issue one, so recency was
  demoted to a tiebreak. Repos untouched past
  `config.projects.stale_after_days` (90) are dropped server-side. The card
  shows 8 rows; `.proj-scroll`'s max-height (not a JS slice) defines that,
  so the rest is revealed by `refreshAutoCycles()` -- **#29's first shipped
  consumer**, and exactly the no-pointer-reach case it was built for.
  Fetch-once semantics per the ask: 6h TTL cached to `data/projects.json`,
  refreshed on a background thread. Verified functionally (lint/test/build
  chain, live `/api/state` curl against real GitHub data) and via a static
  headless render; not yet eyeballed on the live desktop.
- #59 (2026-08-23, refined 2026-08-24): Network's chart moved off the NET
  metric row onto its own full-width row beneath it
  (`.sys-graph{grid-column:1/-1}`), and split from one dual-line box into
  separate DOWN and UP boxes side by side, each captioned. The NET label
  row keeps the standard `.sys-grid` shape (#57) so System's columns still
  line up. `stepChart()` gained an optional `domain` argument: two
  side-by-side SVGs cannot share a scale implicitly the way two series in
  one box did, so both boxes are drawn on one domain spanning both series
  -- without it a 40K/s upload would draw the same shape as a 40M/s
  download. The charts carry explicit width clamps rather than `w-full`,
  since `.sys-grid` is `width:fit-content` and a percentage would resolve
  against whatever the text rows measure. Refined the same day: each box
  originally captioned itself DOWN/UP with no number, leaving the actual
  rate stranded in the NET row above with no chart next to it -- the rate
  now renders directly under its own chart instead (caption, chart,
  value), and the NET row drops its value/sub once the chart row is
  showing, so the same two numbers don't render twice. Falls back to a
  plain label/value/sub row (down as value, up as sub, no charts) under
  two ring-buffer samples, so the rate stays visible before there's enough
  history to draw a line.
- #58 (2026-08-23): follow-up to #57 -- the dash-prefixed sub line (CPU
  temp/freq, MEM used/total, BAT time-remaining, NET upload rate) moved
  from a third `.sys-grid` column beside the value onto its own line
  directly under it (`.sys-sub{grid-column:2}`), matching how this data
  read before #57. `sysRow()` now omits the sub row entirely (not an
  empty one) when there's nothing to show, e.g. Storage.
- #57 (2026-08-23): System (CPU/MEM/BAT, Storage, Network) standardized
  onto the same conventions Weather's `.kv-grid` settled on (#50-#52):
  a new `.sys-grid` (label | value+status-bar, sub on its own line
  under the value -- #58) replaces the old `<table class="sys-table">`
  icon|label|value|tail row shape. No icons (the cpu/mem/disk/battery/net
  glyphs and the network up/down arrow glyphs are gone -- `sysIcon()`
  shrank to just its one remaining live case, sunrise/sunset); one font
  size for every cell. A row's value gets Weather's `.alert-badge`
  treatment past its existing hot threshold (CPU/MEM/Storage > 88%,
  Battery <= 35% unplugged), replacing the retired icon-tone signal.
  Explicitly kept as-is: the Utilization Bars/mini bar/step chart status
  visualization itself, now attached to the value cell instead of a
  separate tail column.
- #56 (2026-08-23): the Devices panel (#27 and its follow-ups -- remembered-
  per-network registry, ipv6-first identification, self-pinning) was
  removed entirely (user: "remove devices. its basically just clutter"),
  not just hidden. `lib/devices.py`, `scripts/nmap.py` (a standalone
  on-demand scan script that also depended on it, `pnpm run nmap`),
  `bin/dashd-serve`'s background nmap-sweep thread and `devices`/
  `devices_gateway`/`devices_scanning` state keys, `config.json`'s
  `devices` block, and `bin/dashd-control`/`web/control/control.js`'s
  devices fields are all gone -- not just the frontend panel. `statusDot()`
  and the `.spinner`/`--color-offline-dot` CSS were also removed since
  Devices was their only consumer. `data/devices.json` is no longer
  written; existing copies on disk are harmless leftovers (gitignored,
  nothing reads them anymore).
- #26 (2026-08-20): new Music component -- MPRIS now-playing state via
  `playerctl` (`lib/sysinfo.now_playing()`), source-agnostic (picks up any
  local player or browser tab registered over MPRIS, no per-service
  integration). Merged into `/api/state` as `music` (null when nothing is
  playing/paused). Renders as a fourth System sub-section below Network,
  same icon|label|value|tail row shape as CPU/MEM/Storage/Network, and
  stays hidden entirely rather than showing an empty row when idle.
  `playerctl` is a system package (`libplayerctl2`/`playerctl`, already
  installed on this machine) -- if it's ever missing, `now_playing()`
  degrades to always returning None rather than erroring. Functionally
  verified (unit tests against a mocked `playerctl` subprocess, smoke
  stage, live curl against `/api/state` with nothing playing); not
  visually verified with an actual track playing -- no player had an
  active track during this session, so eyeball the populated state on the
  live desktop next time something is playing.
- #23 (2026-08-20, **REMOVED 2026-08-21 at 583229d** -- deleted "for
  now", along with Tasks/#25; the `extra.schedule` passthrough it read
  survives untouched, since `extra` is merged verbatim and isn't
  schedule-specific. History kept for the design rationale only -- there
  is no Schedule panel, see #63): new Schedule panel, section below
  System/Forecast
  (`accent-crimson`, completing the teal/gold/red triad). Distinct from a
  full calendar grid -- a compact today's-agenda list (`renderSchedule()` in
  `web/app.js`), reading `state.extra.schedule` (an array of `{time,
  title}` rows) through the existing `data/extra.json` passthrough -- no
  server change needed. Source-agnostic per the issue ("data source TBD
  during implementation"); renders an empty-state row until something
  populates `extra.json`. Verified functionally (smoke stage) and visually
  via a static headless-Chrome render; not checked against the live desktop.
- #27: added `lib/devices.py` (nmap -sn ping sweep) and a Devices row in the
  new Monitor panel. Scanning runs off the request thread -- a `/24` sweep
  can take real seconds, especially if scan_target isn't actually reachable
  from this host, so dashd-serve kicks it off in a background thread and
  serves the last-known cache (`data/devices.json`, TTL
  devices.scan_interval_seconds) rather than blocking /api/state on it.
- #28: added `lib/logsrc.py` (journalctl unit or file tail, filtered
  against config.logs.patterns) and a Log row in the Monitor panel --
  matched/highlighted lines only, not a full tail. Verified end-to-end
  against a real running unit (NetworkManager) and a temp file source in
  this session; the shipped default (`journalctl_unit: "ssh"`) is empty on
  this dev machine since sshd isn't running here, which is correct
  behavior, not a bug.
- #30: added `bin/dashd-control`, a LAN-reachable mobile config surface
  (units/display/logs, whitelisted -- devices was whitelisted too until
  it was removed at #56) paired via a QR code
  (`qrencode`, already packaged -- no new dependency). Token-gated past
  `/api/health`; QR rendering and token rotation are further loopback-only.
  Its systemd unit exists but is **not enabled** (Gatekeeper Protocol).
  Verified end-to-end with curl (health/auth/config GET+POST/qr.svg); QR
  scannability itself relies on `qrencode`'s own correctness rather than an
  in-session phone scan -- flagging that as unverified-by-a-human, not
  unverified-at-all.
- All three original units (`serve`/`collect`/`host`) **enabled and active** against
  `graphical-session.target`; the dashboard survives logout.
  `desktop-dashboard-collect` (#14) was linked, approved, and
  `enable --now`'d in-session — Gatekeeper Protocol satisfied, same as the
  original two units (#1).
- `display.transparent` is **false**. The transparent path was verified to
  work — cosmic-comp does composite an RGBA layer-shell surface over
  `cosmic-bg`'s wallpaper, backdrop blur included — but it reads poorly
  against the current black-hole wallpaper, so it was reverted. One-key flip
  in `config.json` plus a host restart if revisited against calmer imagery.
- #25 (2026-08-20, **FRONTEND REMOVED 2026-08-21 at 583229d** -- deleted
  "for now", along with Schedule/#23. The backend half is deliberately
  still live: `load_tasks()` keeps merging `tasks` into `/api/state` and
  `scripts/smoke.py` keeps asserting it, with nothing on the page
  consuming it. History kept for the design rationale only -- there is no
  Tasks panel, see #63): new Tasks component, a third stacked panel below
  System/Forecast. Source-agnostic per the issue -- `bin/dashd-serve` reads
  `data/tasks.json` (`{"items": [{"text","done","due"}]}`) verbatim and
  merges it into `/api/state` as `tasks`, same read-only passthrough
  pattern as `extra.json`; no task source/API was wired up, so the panel
  shows "No tasks" until something (a script, cron, a future integration)
  writes that file. Reuses System's teal accent rather than adding a
  fourth color, per DESIGN.md's three-accent palette. Functionally
  verified (smoke stage against an isolated worktree-local server, port
  temporarily reassigned) but not visually — same "yours to eyeball"
  convention as recent System/Forecast work.
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
- Open: #3 — `outputs.<name>.layout` reaches
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

## Appendix: the pre-split CLAUDE.md preamble (2026-09-21)

Kept verbatim. The project description below was condensed into the identity
lines and `## Scope` of `CLAUDE.md` when the file was split at #75.

# Desktop Dashboard

Live HTML rendered as the desktop wallpaper on COSMIC/Wayland — weather,
forecast, sun/moon, system stats (with SQLite-backed history for cpu/mem
trends, CPU and Memory each rendering a compact three-tier step-line graph
row; battery has no graphs, #12, #17), and an MPRIS now-playing music panel
(#26). Not a
wallpaper image on a timer: a real WebKit view with CSS animation and JS,
one surface per monitor, sitting below the windows. No digital clock,
date, or location readout -- the user's own system already shows all
three (#5, #9). Two halves: System (left) | Forecast — current
conditions, hourly, week, sun/moon, all one panel (right, #10) — with a
Log panel (#28) below. The Tasks (#25) and Schedule (#23) panels that
used to stack under those two are **gone from the frontend** (removed at
583229d, 2026-08-21); their server-side data sources are still live and
unrendered — see *Layout* and *Current state*.

## Superseded decisions

### `display.transparent` = false (2026-08-20, superseded 2026-09-21)

- Over: the RGBA + backdrop-blur path, which was verified to work
- Why: reads poorly against the current wallpaper. One-key flip if that changes

Recorded in the pre-split `CLAUDE.md` as *"`display.transparent` is **false**. The
transparent path was verified to work ... but it reads poorly against the current
black-hole wallpaper, so it was reverted."* The tracked `config.json` never carried
it: `git log -S'"transparent"' -- config.json` matches only the initial commit
`fdd85e2`, and every sampled commit back to 2026-08-20 has `true`. The revert either
never happened or happened outside the repo, and the claim was lifted into the
approved contract on 2026-09-21 before being caught the same day. Replaced by the
`true` entry plus the time-of-day slate tint (#78).
