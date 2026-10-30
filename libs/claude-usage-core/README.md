# claude-usage-core

Rust library shared by `cosmic-applet-claude-usage` and `vitals`: Claude plan-usage reading types, pace math, the Claude Code status-line feed, and the words used to describe them. Contract and module map: `CLAUDE.md`.

## Verify
`scripts/check.sh` (fmt, clippy, tests with and without the `usage-text` feature, release build; last line `CHECK PASS`).

## Outside this repo
- **Installs nothing.** No binary, no desktop entry, no config. It exists only to be depended on.
- **Consumers find it by relative path** (`path = "../claude-usage-core"`): `cosmic-applet-claude-usage` (with `usage-text`) and `vitals` (without). Keep all three as siblings or those builds break.
- `usage-text` is off by default because it pulls `regex` and `chrono-tz`; `vitals` never needs them.
- Part of the `cosmic/` folder (`../README.md`), though nothing in it is COSMIC-specific.
