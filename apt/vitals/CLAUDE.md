# vitals
btop-style terminal dashboard: CPU, memory, disk, network timelines, Claude Code token usage over time,
and the Claude usage row. Binary `vitals`, Rust + ratatui + crossterm, Linux only.
Full spec: `docs/spec.md` (binding; change it in the same commit as any behavior change).
Siblings: `../../libs/claude-usage-core` (shared Claude usage crate), `../../cosmic/applet-claude-usage` (writes the feed).

## Build / verify
`scripts/check.sh` (fmt, clippy -D warnings, tests, release build, size check; last line `CHECK PASS`).
`scripts/bench.sh` checks the §8 performance budget. @scripts/README.md
UI snapshots: `INSTA_UPDATE=always cargo test --test ui` accepts new ones (tests pin TZ=UTC and a fixed clock).
`vitals --once --size WxH` prints one frame as text; `vitals --all` (alias `--dump`) prints every raw number.

## Scope
- Sections: 1 CPU, 2 Memory, 3 Disk, 4 Network, 5 Claude, 6 Tokens, placed by config rows. Each stacks
  compact timeline -> bars (the focus) -> summary, each toggleable; settings + help popups top right (spec §4).
- CLI readouts: `vitals --cpu --json` etc. (spec §9).
- Every mouse action has a key twin (spec §6). Config `$XDG_CONFIG_HOME/vitals/config.toml`, saved at once on every UI change.
- Web dashboard: `vitals --serve` (headless, spec §14): the page in `web/`, server in `src/web/`, TLS files you supply, no key by default (`[web] auth = "none"`, private-network guard; `auth = "key"` is opt-in).
- Out of scope: process list, GPU, battery, remote hosts, persisted system history, non-Linux.

## Decisions
- 2026-10-28 (Corbin): name `vitals`; Claude code shared via path crate `../../libs/claude-usage-core`; disk shows
  both fullness (space per mount) and busy %; no GPU; default theme `default` (btop style);
  Claude row and Tokens panel use Claude orange `#D97757`; token usage over time is panel 6.
- System numbers use conventional /proc definitions.
- Claude usage: feed file `$XDG_RUNTIME_DIR/claude-usage/rate_limits.json`, then the optional usage server `GET 127.0.0.1:4317/api/usage`.
  Never spawn `claude` (the applet owns the `/usage` fallback).
- Tokens: read `~/.claude/projects/**/*.jsonl` (or `$CLAUDE_CONFIG_DIR/projects`) usage fields only, dedupe by
  `message.id`, index at `$XDG_CACHE_HOME/vitals/tokens-v1.bin`; never store message content.
- `/proc` and `/sys` read directly (no `sysinfo`); no async runtime; zero allocations per tick in steady state.
- Tests are headless (`TestBackend`, fixtures); never open windows or touch the live desktop; never run `claude`.

- 2026-10-29 (Corbin): web dashboard `vitals --serve` for a tablet PWA: one port 4318, TLS sniffed from the first byte,
  the supplied `server.crt`/`server.key` reused (never new certificates, never `ca.key`), `rustls` + `ring`, no async runtime.
- 2026-10-29 (Corbin): binary budget raised to 3.25 MB for `--serve` (one binary; no Cargo feature).

## Open questions
- None yet.
