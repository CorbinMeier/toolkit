---
paths:
  - "web/index.html"
  - "web/app.js"
  - "web/control/**"
---

# Frontend panel rules (`web/`)

```
web/index.html      panel structure (Tailwind utility classes); System
                    renders as three compact .sys-grid CSS grids (not
                    w-full -- sized to content), one each for CPU/MEM/BAT,
                    Storage, Network: label | value (+ Utilization Bars/
                    mini bar/step chart), no icons, one font size (#57).
                    An optional sub line (CPU temp/freq, MEM used/total,
                    BAT time-remaining, NET upload rate) sits dash-prefixed
                    directly under the value, pinned there via
                    grid-column:2 rather than beside it as a third column
                    (#58). A row's value takes Weather's alert-badge
                    treatment (#49) past its hot threshold, replacing the
                    retired icon-tone signal. Music (#26) is a fourth
                    System sub-section below Network with its own bespoke
                    layout (cover art + title/artist/progress, not a row
                    grid) plus transport controls (#65). It does NOT hide
                    when idle -- user report: "it should not disappear just
                    because no media is playing, should just sit idle" --
                    it shows a placeholder and disables its controls.
                    A Log panel (#28) follows System/Forecast -- same
                    notched-panel treatment. Three panels are gone and
                    stay gone: Devices (#27) removed entirely at #56
                    ("basically just clutter"), and Tasks (#25) +
                    Schedule (#23) deleted "for now" at 583229d --
                    no markup, no renderTasks()/renderSchedule(), even
                    though the server still serves both feeds (#63)
web/app.js          all rendering; pure function of last good state.
                    Two independent trend sources feed the step charts: an
                    in-memory ring buffer (this session's own /api/state
                    polls, for the 30s CPU/Memory tier and the Network
                    chart) and trendHistory (/api/history rows, for every
                    tier >= 5 minutes). refreshAutoCycles() (called every
                    apply()) drives any class="auto-cycle" element through a
                    passive scroll-dwell-reset loop -- the only way to reveal
                    overflowing panel content on a surface with no pointer
                    reach (#29); Sessions/Log (#91, bounded by .sess-scroll/
                    .log-scroll's max-height) are its shipped consumers now.
                    placePanels() (#91, follow-up to #3) moves each panel
                    <section> into #colLeft/#colCenter/#colRight per
                    data-layout, resolved from outputs.<name>.layout by
                    measured aspect ratio when it's 'auto'. Since #94 the
                    enabled panels come from outputs.<name>.cards (ordered
                    ids, CARDS in app.js) and are dealt round-robin across
                    the layout's columns; unset cards fall back to LAYOUTS
                    flattened row by row, which deals back to the same
                    columns. Cards rows show an L/C/R
                    tag (index % columns); ←/→ swap with the neighbour
                    slot (same row, adjacent column), ↑/↓ with index±cols
                    (same column) (#101). Cards has a "Layout: auto/narrow/wide"
                    submenu (#100) writing outputs.<name>.layout; profiles
                    snapshot it beside `cards` (older profiles without it
                    leave layout alone). The context menu (Profiles/Cards/Settings) is
                    patchConfig()-driven: optimistic local merge, then POST
                    /api/config. Profiles live in data/config.local.json
                    under `profiles`; a null value deletes one. Likewise
                    `outputs.<key>.cards: null` resets a monitor to its
                    LAYOUTS default (Cards > Reset to default, #98).
                    patchConfig records an inverse patch (20 deep, in-page,
                    not location) for the root "Undo" row (#99). A profile may carry
                    `when: day|night` (#102): scheduleProfiles() in apply() applies
                    it once per sunrise/sunset change, first output only
web/vendor/         Tailwind v4 browser build, vendored (no network at runtime)
web/control/        standalone mobile control UI served by bin/dashd-control
                    (plain HTML/JS, deliberately not styled to DESIGN.md's
                    Night Ops HUD -- it's a phone-viewed form, not the
                    wallpaper surface)
```

## Gotchas

- **A 30-second step-line chart cannot be built from `/api/history`** —
  samples land there roughly every `metrics_sample_seconds` (30s default),
  so a 30-second window is 0-1 points. `web/app.js` keeps a separate
  client-side ring buffer (`ring`, fed from every `/api/state` poll, ~5s)
  for exactly that tier, plus the Network chart (throughput bursts are
  brief enough that even 30s DB resolution would flatten them). Anything
  5 minutes or longer reads from `trendHistory` (`/api/history`) instead,
  since by then the DB's resolution is fine *and* it survives a page
  reload, which the in-memory ring buffer does not.

