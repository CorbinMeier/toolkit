# claude-usage-core
Rust lib shared by the COSMIC claude-usage applet and `vitals`: Claude plan-usage reading types, pace math, status-line feed, words.
Dependents (path `../claude-usage-core`): `../cosmic-applet-claude-usage` (with `usage-text`) and `../vitals` (without).

## Build / verify
`scripts/check.sh` (fmt, clippy -D warnings and tests with and without `usage-text`, release build; last line `CHECK PASS`). @scripts/README.md
Run cargo headless: `env -u WAYLAND_DISPLAY -u DISPLAY cargo ...`.

## Scope
- `reading`: `Limit`, `Reading`, `HOT_PERCENT` (95). No regex, no chrono-tz.
- `pace`: pace/expected-percent math and day-part math (port of an earlier TypeScript implementation).
- `feed`: the Claude Code status-line feed: stdin parser, `run_statusline` writer, file reader, `FRESH_SECS`, `feed_dir`.
- `readout`: the hover/TUI usage readout. `lines(reading, read_at_ms, stale, now_ms) -> Vec<Line>` (`Line` = Row{label,used,expected} | Reset | Blank | Note) is the one implementation; `text(...)` joins it with aligned colons (moved from the applet's `readout.rs`, byte-identical output).
- `words`: `stale_words`, `reset_short` (port of an earlier TypeScript implementation).
- `parse` (feature `usage-text`, off by default): parser for `claude -p /usage` text; pulls regex and chrono-tz.
- Out of scope: running `claude`, UI, polling, HTTP, anything GUI.

## Decisions
- Extracted from the applet on 2026-10-28 (Corbin approved) so `vitals` shares one definition. Behavior is unchanged; each module moved with its tests.
- Only `parse` needs regex and chrono-tz, so it sits behind `usage-text`; `vitals` never pulls them.
- The feed file contract (`$XDG_RUNTIME_DIR/claude-usage/rate_limits.json`, 15 min fresh) is defined in `feed`; the applet owns the `claude /usage` fallback, vitals never runs `claude`.
- Tests use `tests/fixtures/usage-output.txt` (copy of an earlier tool's fixture); the real CLI is never run.
- Edition 2024, MIT, like the applet and the rest of the repo.

- `readout` moved here 2026-10-29 (Corbin approved) so the applet and `vitals` show identical text; the applet's `Status` is not in the crate, callers pass `reading`, `read_at`, `stale()`.

## Open questions
- None.
