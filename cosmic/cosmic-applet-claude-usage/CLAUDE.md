# cosmic-applet-claude-usage
COSMIC panel applet showing Claude plan usage as two vertical bars with a pace marker.
App ID `net.corbinmeier.CosmicAppletClaudeUsage`, binary `cosmic-applet-claude-usage`.
Sibling projects: `../cosmic-applet-timer` (skeleton this copies), `../cosmic-notification-center`.

## Build / verify
`scripts/check.sh` (fmt, clippy -D warnings, tests, release build; last line `CHECK PASS`).
`scripts/install.sh` / `uninstall.sh` (user-local: `~/.local/bin` + `~/.local/share/applications`). @scripts/README.md

## Scope
- Left bar: 5-hour session limit. Right bar: the current day part of the week budget (week cut into 7 x 24 h parts; the bar is that part's 1/7 share of the week allowance). Per-model weeks only in the hover text.
- Two vertical bars side by side, session on the left, daily on the right, same on every panel orientation. Each fills from the bottom upward with percent used (fill capped at 100, text shows the raw number).
- A horizontal marker line, 2 px (white on a dark COSMIC theme, near-black on a light one; read from the theme on every draw) on each bar at the share of the window already gone, measured from the
  bottom: `clamp((now - start) / window, 0, 1)`; session `start = resets_at - 5 h`, daily `start = W + d * 24 h`,
  window 24 h (see Decisions; the marker is always drawn when the day part resolves). Week with
  no reset time: `W` starts at the most recent Thursday 06:00 local. Session with no reset time:
  no marker. The marker moves on a local 60 s tick, independent of polls.
- Fill uses the COSMIC theme accent; the session bar switches to the theme's destructive color at 95% or more, the daily bar when the week percent is 95% or more.
- Hover shows the limits with percent > 0, blocks separated by a blank line. `5 Hour` first, with its own `Resets`:
  `5 Hour : 8% / 62%` (used / expected, the pace marker's value, `—` when unknown) and `Resets : 7:19 PM` (local;
  `Thu 6:00 AM` when not today; omitted when unknown). Then one group: `Daily`, `Weekly`, one line per per-model week
  (bare model name), no blank lines inside, ending in a single `Resets` line (the week's; the first per-model week's if
  the week has none). `Daily : 12% / 50%` shows whenever Weekly does (even at 0%); the daily part's own boundary is not
  shown. Labels are padded so the colons line up. `No usage yet` when every block is hidden, `Reading usage…` before the
  first reading, and after a failed run a trailing `Stale - last read 2:14 PM` (no error words).
- Out of scope: system stats, a full-screen overlay, notifications, per-model bars.

## Decisions
- Data comes from `claude -p /usage --output-format json --no-session-persistence` (the same command
  an earlier TypeScript tool ran): the CLI's own login,
  no token handling, no HTTP. The JSON has no structured limits; the `result` text is parsed with
  line regexes ported from that tool. Reset text has no year and carries an IANA zone in parentheses.
- `claude` is spawned by absolute path (`$HOME/.local/bin/claude`, PATH fallback): the panel's PATH
  may lack `~/.local/bin`. 60 s timeout. `--no-session-persistence` is mandatory (no transcript per poll).
- Primary data is the Claude Code status-line feed: the statusLine command
  `cosmic-applet-claude-usage --statusline` (setting `"statusLine": {"type": "command", "command": "<abs bin path> --statusline"}`, abs bin = `~/.local/bin/cosmic-applet-claude-usage`)
  receives `rate_limits.five_hour|seven_day {used_percentage, resets_at (epoch s)}` on stdin after every reply and writes
  `$XDG_RUNTIME_DIR/claude-usage/rate_limits.json` (fallback `$HOME/.cache/claude-usage/`, created only when `XDG_RUNTIME_DIR` is unset; dir 0700, atomic rename, no stdout,
  errors ignored, no GUI init). The applet reads it on its 60 s tick; a feed written within 15 min is the reading
  (a null window keeps the current reading's value while its `resets_at` is still ahead, as the first write of a session can omit one; otherwise 0%, no reset). Per-model weeks exist only in `/usage` output.
- `claude /usage` (~16 s and ~0.5 GB per run) is only the fallback: when the feed is stale or missing, at most once
  every 15 min, never two runs at once; newest reading wins. A failed run, or `result` text matching
  `/rate.?limited/i`, keeps the last good reading and marks it stale.
- libcosmic pinned by `rev` `430f3d48a97d8779f9c1985473912e594ccca1ae` (cosmic-applets 328d142, COSMIC
  1.11.0). Re-pin on COSMIC upgrades per `../cosmic-applet-timer/CLAUDE.md`.
- Never edit the user's panel config; the applet is added by hand in Settings > Desktop > Panel > Applets.
- Tests and dev runs never open windows on the live desktop: run with `env -u WAYLAND_DISPLAY -u DISPLAY`.

- Daily bar is stateless from the week reading: week start `W = resets_at - 168 h` (else last Thursday 06:00), day `d = clamp(floor((now - W) / 24 h), 0, 6)`,
  `daily = (week% - d * 100/7) / (100/7) * 100`, fill capped at 100, raw in the hover, floored at 0. Overage carries forward: a heavy day keeps the bar
  pegged until the linear budget catches up; under budget shows 0. Marker = share of the 24 h part gone.
- Daily bar is destructive when the week percent is >= `HOT_PERCENT` (95), not the daily number: the week is the real lockout and no week bar remains; passing a day's share is routine.
- Hover text uses `applet.applet_tooltip` (libcosmic wayland tooltip, same call as cosmic-applets
  panel-button/app-list at 328d142), `src/app.rs` `view`. It takes a `'static` string, so the text is rebuilt each view.
- Shared code lives in `../claude-usage-core` (path dependency, feature `usage-text`; also used by `../vitals`): `reading` (`Limit`, `Reading`, `HOT_PERCENT`), `pace` (ported from `pace.ts`, plus `day_part`/`daily_percent` day-part math), `feed` (`--statusline` writer, feed file reader), `words` (stale line), `readout` (hover text: `text`/`lines`), `parse` (ported from `parse.ts`). Edit those there, not here; their tests live there.
- Module map (this crate): `poller.rs` (run
  `claude`, `Status` keeps last good reading, `Schedule` = no overlap, 15 min), `bars.rs` (canvas, bottom-anchored fill, horizontal marker line; right bar = daily), `app.rs` (60 s tick: read feed, else maybe `/usage`).
  Bar height = panel icon size, width icon/3, gap icon/6 (`BAR_WIDTH_DIV`, `BAR_GAP_DIV`).
- Tests use `tests/fixtures/usage-output.txt` (copy of an earlier tool's fixture; the core crate keeps its own copy) and injected outcomes; the real CLI is never run.

## Open questions
- Live-panel look (track color `neutral_5` at 50%, bar sizes, tooltip) not yet verified.
- Server alternative: a usage server at `GET http://127.0.0.1:4317/api/usage` can serve the same reading; the applet
  runs its own poll so it works without one. Revisit if `/usage` starts rate limiting.
