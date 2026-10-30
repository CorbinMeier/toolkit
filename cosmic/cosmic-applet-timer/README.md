# cosmic-applet-timer

COSMIC panel applet: one countdown timer (Rust + libcosmic).

## Behavior
- Idle: `clock-symbolic` icon. Running: remaining time (`M:SS`, `H:MM:SS` from an hour).
- Click (idle or running): popup with H/M/S spin buttons (hours 0–99, minutes/seconds 0–59; default 5:00, last-used kept in memory only) and Start. While running it shows the time left and Cancel. The popup closes after Start or Cancel; Start at 0:00 does nothing.
- At zero (Ringing): desktop notification, alarm sound loops, panel button flashes "0:00" on red/yellow every 0.5 s. Click the panel button to dismiss: sound and flashing stop, popup does not open.

## Install
1. `scripts/install.sh` (builds release; installs `~/.local/bin/cosmic-applet-timer` and `~/.local/share/applications/com.corbinmeier.CosmicAppletTimer.desktop`; no sudo).
2. Settings > Desktop > Panel > Applets > Add applet > "Timer". Not listed: close and reopen Settings.
3. Drag it where you like, like any other applet.

Uninstall: remove it from the panel, then `scripts/uninstall.sh`.

Updating: rerun `scripts/install.sh`. A running applet keeps the old binary until the panel restarts it: remove and re-add the applet, or log out and back in.

## Runtime dependency (sound)
`canberra-gtk-play` (event `alarm-clock-elapsed` from the sound theme), else `pw-play` or `paplay` with `/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga`. If none exist the alarm is silent; notification and flashing still work.

## After a COSMIC upgrade
libcosmic is pinned to the installed COSMIC release (see `CLAUDE.md`). If the popup stops opening after an upgrade, re-pin libcosmic to the new release and reinstall.

## Known issues
- `desktop-file-validate` flags `Categories=COSMIC;` (unregistered category). The stock COSMIC applets use the same value; left as is.

## Outside this repo
- Installs only two files, both from `scripts/install.sh` (no sudo): `~/.local/bin/cosmic-applet-timer` (a release build, not tracked here) and `~/.local/share/applications/com.corbinmeier.CosmicAppletTimer.desktop`. `scripts/uninstall.sh` removes both; remove the applet from the panel first.
- Panel layout is COSMIC's own config (`~/.config/cosmic/...`); this repo never edits it.
- The build is pinned to libcosmic from COSMIC 1.11.0 (cosmic-applets `328d142`) in `Cargo.toml`. It must be re-pinned after a COSMIC upgrade; see "After a COSMIC upgrade" above.
- Part of the `cosmic/` folder (`../README.md`). Standalone, it has no sibling dependencies.
