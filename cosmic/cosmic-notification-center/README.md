# cosmic-notification-center

Replaces COSMIC's notification service: a CSS-driven daemon (toasts, rules, sounds, history), a reading/customization window, and a panel applet. Python 3 (GTK, WebKit) with a Rust applet in `applet/`. Contract: `CLAUDE.md`; full spec: `docs/spec.md`; rules: `docs/rules.md`; scripts: `scripts/README.md`.

## Outside this repo (this tool touches the most)
Everything below is logged with its revert command in `docs/system-changes.md`.
- **It replaces a live system service.** `scripts/install.sh` renders `~/.local/bin/cosmic-notifications`, a restart-loop shim. `~/.local/bin` comes first on cosmic-session's `PATH`, so the shim shadows `/usr/bin/cosmic-notifications`. If a COSMIC update starts the daemon by absolute path, the shim is silently bypassed.
- **Absolute repo path baked in.** The shim (`repo='...'`) and `~/.local/share/applications/net.corbinmeier.NotificationCenter.desktop` (`Exec=.../bin/notifyd`) hardcode where this repo lives. Moving or renaming the directory breaks the running daemon on its next restart. Moving means re-running `scripts/install.sh` and `scripts/install-launcher.sh` from the new location, and `scripts/install-applet.sh` for the applet.
- Panel applet: `~/.local/bin/cosmic-applet-notification-center` (release build from `applet/`, not tracked) plus `net.corbinmeier.CosmicAppletNotifications.desktop`. Adding it to the panel is manual.
- Launcher entry and four icons under `~/.local/share/icons/hicolor/`.
- User data: config and rules in `~/.config/net.corbinmeier.notifications/` (`config.toml`, `rules.toml`, `style.css`, never overwritten by install), history in `~/.local/share/net.corbinmeier.notifications/history.db`. `scripts/uninstall.sh --purge` deletes these.
- Kill switch: create `~/.config/net.corbinmeier.notifications/disable` and the shim execs the stock daemon.
- Restart with `bin/notifyd --restart`, not `killall cosmic-notifications` (that restarts the panel too and feeds cosmic-session's growing restart backoff).
- `.venv/` here is untracked dev tooling with absolute paths; the daemon itself runs `/usr/bin/python3 -m notifyd`. Recreate the venv after moving.
- Install and demo scripts show windows or replace a live service: run them with the user present, and run `scripts/check.sh` headless.
- Part of the `cosmic/` folder (`../README.md`).
