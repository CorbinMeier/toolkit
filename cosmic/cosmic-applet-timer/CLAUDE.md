# cosmic-applet-timer
Single-countdown COSMIC panel applet. App ID `com.corbinmeier.CosmicAppletTimer`, binary `cosmic-applet-timer`. User-facing behavior and install: `README.md`.

## Build / verify
`scripts/check.sh` (fmt, clippy -D warnings, tests, release build; last line `CHECK PASS`). `scripts/install.sh` / `uninstall.sh`. No `just`. @scripts/README.md

## Scope
- One countdown; idle clock icon, running remaining time, ringing until clicked.
- Out of scope: pause/resume, multiple timers, persisting the last duration across restarts.

## Decisions
- libcosmic pinned by `rev` to `430f3d48a97d8779f9c1985473912e594ccca1ae`: the rev in cosmic-applets 328d142 Cargo.lock, matching installed COSMIC 1.11.0 (prior pin 03c8f93, epoch 1.9.0; no API changes were needed). A mismatched popup protocol builds but does nothing.
- Re-pin on COSMIC upgrade: take the commit suffix of `dpkg-query -W cosmic-applets`, read libcosmic's rev from `git show <commit>:Cargo.lock` in a pop-os/cosmic-applets clone, update `Cargo.toml`, `cargo update -p libcosmic`, run `scripts/check.sh`.
- Popup opened via `cosmic::surface::action::app_popup` (same as stock time applet at that rev), not the template's `get_popup`.
- State `Idle | Running{deadline} | Ringing` (`src/timer.rs`); remaining always derived from the deadline, rounded up. 1 s tick only while Running, 500 ms flash tick only while Ringing. Click while Ringing dismisses, never opens the popup.
- Sound: spawned child process in a std thread (`src/sound.rs`), looped, killed on dismiss. canberra-gtk-play, then pw-play, then paplay. No audio crate.
- Applets are enumerated via `freedesktop_desktop_entry::default_paths()` = XDG_DATA_HOME + XDG_DATA_DIRS `/applications`, so the user-local desktop file works. Desktop `Exec=` is an absolute path, filled from `@BIN@` by `install.sh`.
- Never edit the user's panel config; adding the applet is manual in Settings.

## Open questions
- Runtime behavior (popup, tick, sound, flash, dismiss) not yet verified in the live panel.
- `desktop-file-validate` rejects `Categories=COSMIC;`; kept to match stock applets.
