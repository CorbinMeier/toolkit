# Cosmic

My COSMIC desktop tooling (Pop!_OS 24.04, COSMIC 1.11.0, Wayland). The general, non-COSMIC tools are in `../local-ai/`.

| Directory | What it is | Installs to |
|---|---|---|
| `cosmic-notification-center/` | Replaces COSMIC's notification service: CSS-driven toasts, rules, sounds, history, a shade, a panel applet. Python + GTK/WebKit, plus a Rust applet. | `~/.local/bin`, `~/.config` and `~/.local/share` (`net.corbinmeier.notifications`), app launcher, icons |
| `cosmic-applet-timer/` | Panel applet: one countdown timer. Rust + libcosmic, pinned to COSMIC 1.11.0. | `~/.local/bin`, `~/.local/share/applications` |
| `cosmic-applet-claude-usage/` | Panel applet: Claude plan usage as two bars with a pace marker. Fed by Claude Code's status line. | `~/.local/bin`, `~/.local/share/applications`, plus a hand edit to `~/.claude/settings.json` |
| `claude-usage-core/` | Rust library: usage types, pace math, status-line feed. Not installed. | nothing |
| `vitals/` | btop-style terminal dashboard (CPU, memory, disk, network, Claude usage, tokens) and a web mode. **Not a COSMIC tool**; it lives here only because it depends on `claude-usage-core`. | `~/.local/bin/vitals`, optional `vitals-web.service` |

## Prerequisites
libcosmic, COSMIC itself, Claude Code and Rust come from their own repos, not from here. Links, and why libcosmic is pinned: [`../PREREQUISITES.md`](../PREREQUISITES.md).

## Things to know before moving, building or uninstalling
- **Build products are not tracked.** The binaries in `~/.local/bin` are release builds from `scripts/install.sh`, not files in this repo. `target/`, `.venv/` and caches are gitignored.
- **`claude-usage-core` must stay a sibling** of `cosmic-applet-claude-usage` and `vitals` (`path = "../claude-usage-core"`). Splitting `vitals` out means publishing or vendoring that crate first.
- **One thing shadows system software:** the `cosmic-notifications` shim in `~/.local/bin` wins over `/usr/bin/cosmic-notifications` because `~/.local/bin` is first on the session `PATH`. A COSMIC update that starts the daemon by absolute path would silently bypass it.
- **Installed copies remember where the repo was.** The `~/.local/bin/cosmic-notifications` shim and the Notification Center `.desktop` entries hardcode the checkout path at install time. If you move the checkout, re-run `scripts/install.sh` and `scripts/install-launcher.sh`, or leave a symlink at the old path. See that tool's README.
- **Each tool logs what it changes outside the repo** in its own `docs/system-changes.md` where present, with the revert command; every tool has `scripts/uninstall.sh`.
- `cargo`/`libcosmic` builds download and compile a lot; `target/` directories are large.

## Verify
Each tool has `scripts/check.sh` (last line `CHECK PASS`). Run it inside the tool's directory; headless (`env -u WAYLAND_DISPLAY -u DISPLAY`) so no test opens windows on the desktop.
