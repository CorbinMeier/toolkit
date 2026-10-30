# claude-usage-core

Rust library shared by `cosmic-applet-claude-usage` and `vitals`: Claude plan-usage reading types, pace math, the Claude Code status-line feed, and the words used to describe them. Contract and module map: `CLAUDE.md`.

## Verify
`scripts/check.sh` (fmt, clippy, tests with and without the `usage-text` feature, release build; last line `CHECK PASS`).

## Outside this repo
- **Installs nothing.** No binary, no desktop entry, no config. It exists only to be depended on.
- **Consumers find it by relative path:** `cosmic/applet-claude-usage` (with `usage-text`) and `apt/vitals` (without), both with `path = "../../libs/claude-usage-core"`. Move this folder and both `Cargo.toml` files need editing.
- `usage-text` is off by default because it pulls `regex` and `chrono-tz`; `vitals` never needs them.
- Lives in `libs/` because it needs only Rust; see the root README for the folder rules.
