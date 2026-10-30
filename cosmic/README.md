# Cosmic

Tools that need the **COSMIC desktop** (COSMIC 1.11.0 on Pop!_OS 24.04, Wayland). May depend on `../libs/`; never on `../apt/` or `../archive/`. General, non-COSMIC tools live in `../apt/`.

| Directory | What it is | Installs to |
|---|---|---|
| `notification-center/` | Replaces COSMIC's notification service: CSS-driven toasts, rules, sounds, history, a shade, a panel applet. Python + GTK/WebKit, plus a Rust applet. | `~/.local/bin`, `~/.config` and `~/.local/share` (`net.corbinmeier.notifications`), app launcher, icons |
| `applet-timer/` | Panel applet: one countdown timer. Rust + libcosmic, pinned to COSMIC 1.11.0. | `~/.local/bin`, `~/.local/share/applications` |
| `applet-claude-usage/` | Panel applet: Claude plan usage as two bars with a pace marker. Fed by Claude Code's status line. | `~/.local/bin`, `~/.local/share/applications`, plus a hand edit to `~/.claude/settings.json` |

The shared Rust library behind the usage applet is [`../libs/claude-usage-core`](../libs/claude-usage-core/README.md); the terminal dashboard that reads the same feed is [`../apt/vitals`](../apt/vitals/README.md).

## Prerequisites
libcosmic, COSMIC itself, Claude Code and Rust come from their own repos, not from here. Links, and why libcosmic is pinned: [`../PREREQUISITES.md`](../PREREQUISITES.md).

## Things to know before moving, building or uninstalling
- **Build products are not tracked.** The binaries in `~/.local/bin` are release builds from `scripts/install.sh`, not files in this repo. `target/`, `.venv/` and caches are gitignored.
- **`applet-claude-usage` builds against `../../libs/claude-usage-core`** by relative path (`path = "../../libs/claude-usage-core"`). Keep the repo layout intact or fix that `Cargo.toml`.
- **One thing shadows system software:** the `cosmic-notifications` shim in `~/.local/bin` wins over `/usr/bin/cosmic-notifications` because `~/.local/bin` is first on the session `PATH`. A COSMIC update that starts the daemon by absolute path would silently bypass it.
- **Installed copies remember where the repo was.** The `~/.local/bin/cosmic-notifications` shim and the Notification Center `.desktop` entries hardcode the checkout path at install time. If you move the checkout, re-run `scripts/install.sh` and `scripts/install-launcher.sh`, or leave a symlink at the old path. See that tool's README.
- **Each tool logs what it changes outside the repo** in its own `docs/system-changes.md` where present, with the revert command; every tool has `scripts/uninstall.sh`.
- `cargo`/`libcosmic` builds download and compile a lot; `target/` directories are large.

## Verify
Each tool has `scripts/check.sh` (last line `CHECK PASS`). Run it inside the tool's directory; headless (`env -u WAYLAND_DISPLAY -u DISPLAY`) so no test opens windows on the desktop.
