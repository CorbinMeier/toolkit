# vitals

btop-style terminal dashboard: CPU, memory, disk and network timelines, Claude Code token usage over time, and the Claude plan-usage row. Rust, ratatui, Linux only. `vitals --serve` runs the same data as a web dashboard. Spec (binding): `docs/spec.md`; contract: `CLAUDE.md`.

```
scripts/install.sh              # builds and installs ~/.local/bin/vitals
scripts/install.sh --service    # also installs and enables vitals-web.service
scripts/uninstall.sh
scripts/check.sh                # last line CHECK PASS
```

## Outside this repo
- **Not a COSMIC tool.** It lives in `apt/` (Linux only; reads `/proc`). It depends on [`../../libs/claude-usage-core`](../../libs/claude-usage-core/README.md) by relative path (`path = "../../libs/claude-usage-core"`); keep the repo layout intact or fix `Cargo.toml`.
- `scripts/install.sh` writes `~/.local/bin/vitals` (release build, not tracked here). With `--service` it also writes `~/.config/systemd/user/vitals-web.service` (`ExecStart=%h/.local/bin/vitals --serve`) and enables it. Uninstalling the binary without disabling the unit leaves a failing service.
- Config: `$XDG_CONFIG_HOME/vitals/config.toml`, saved on every UI change. Cache: `~/.cache/vitals`.
- Web mode has no key by default and relies on a private-network guard (`[web] auth = "none"`); set `auth = "key"` before exposing it.
- System numbers use conventional /proc definitions.
- The Claude row reads the feed that `cosmic-applet-claude-usage --statusline` writes (via the `~/.claude/settings.json` `statusLine` entry).
