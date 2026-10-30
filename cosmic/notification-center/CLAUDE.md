# cosmic-notification-center

Replaces COSMIC's notification service: a CSS-driven daemon (toasts, rules, sounds, history),
a reading/customization window, and a panel applet. Full spec: `docs/spec.md`.
Every change outside the repo is logged in `docs/system-changes.md` with its revert command.

## Layout
Module map and config-dir behavior: `.claude/rules/layout.md` (loads with the code).

## Scope
- Owns `org.freedesktop.Notifications` (spec 1.2) and is launched through a
  restart-loop shim named `cosmic-notifications` in `~/.local/bin` (installed by `scripts/install.sh`).
- Opening the shade (bell, `ShowShade`, `--toggle-shade`) closes every toast on screen, sticky ones too, as EXPIRED; they stay in the shade, unread state untouched (`[shade] clear_toasts_on_open`, default true, live).
- App launcher: `scripts/install-launcher.sh` adds "Notification Center" (desktop entry + icons from `resources/`) to the COSMIC app library; it runs `bin/notifyd --show-center`.
- Records every notification to SQLite, including blocked ones and ones that arrive during DND.
  History is kept forever by default.
- Rules in `rules.toml`: match on app, summary/body regex, urgency, category, or any hint. They
  can block, set a sound, set a timeout or `never`, override urgency or icon, add CSS classes,
  highlight words, add tags, or store only. A save applies immediately.
- Sounds: a file path is played as-is (mp3/wav/ogg/flac); with none set, the system theme's
  `message-new-instant` plays.
- Default toast timeouts (per urgency, app timeout respected) and the hover hold are configurable
  in `config.toml` `[toasts]` (`hover_leave_grace_ms`, `hover_max_hold_ms` safety release, default
  120000, 0 = off); a toast never closes while hovered. Hover truth is GTK enter/leave-notify on the
  toast window (WebKit does not report leave on a layer-shell surface); JS is secondary.
- Look: `default.css` (`--nc-*` variables), then `theme.css` from the Appearance panel,
  then the hand-written `style.css`.
  Rule-button and run-log hooks (documented at the top of each css): `.toast-buttons/.toast-button`,
  `.shade-buttons/.shade-button`, `.nc-button`, `.nc-runlog`/`.nc-run`/`.nc-run-output`.
- Window: history list, search, read, actions, plus a customization panel covering appearance
  presets, behavior, sounds and a rules editor (phase 6: `web/rules.js`, the Settings "Rules" tab),
  with live preview and a test-notification button. The panel edits `rules.toml` block by block and
  keeps its comments and every rule it did not change; `docs/rules.md` "Editing in the panel".
- `bin/notifyd --show-center` / `--show-notification ID` call the running daemon and exit (a shortcut command).
- `bin/notifyd --toggle-shade` calls `ToggleShade` the same way (bind it to a key).
- Workflow (3b, daemon side): rule `run` goes to the `Runner` (`arrive` for every recorded
  notification, `shown` only when displayed; none again on a replace of an open one). Button presses
  from toast, shade, center and bus all go through `NotificationServer.press_button(id, button_id)`
  (run, built-ins, `close`). Snooze keeps the row open, hides it from the shade (`store.shade_items`)
  and `check_snoozes` re-shows it at startup and every 30 s. Flags: `--no-run` (no Runner; scripts
  never run), `--press-button ID BUTTON` (bus client). Bus: `PressButton(u, s)`, `GetRuns(u) -> s`
  (JSON run log). The Runner is shut down on exit.
- Reply actions: a sender's `x-nc-actions` hint (choices and/or free text plus one HTTP request) makes the daemon send it, only to URLs in the user's `http-actions.toml` allowlist (empty by default, so inert); `docs/reply-actions.md`. Bus `ReplyChoice`/`ReplyText`, CLI `--reply-choice`/`--reply-text`.
- Image previews: a notification's `image-path` hint, or a body that is one image path (COSMIC Screenshot), shows a thumbnail on toasts, shade rows and center rows, a large preview in the center detail, and Copy image / Copy path buttons (`[images] preview`, live). Served to the pages only through the `nc-image://` WebKit scheme, never `file://`; `docs/spec.md` "Image previews".
- Applet: bell, "new" badge (`UnseenCount`, since the shade/center was last opened), toggles the shade (bus `ToggleShade`; `ShowShade` also exists), DND.
- Out of scope: sounds an app plays through its own audio engine.

## Decisions
- Restart the daemon with `bin/notifyd --restart` (kills only the Python child; the shim restarts it in ~1 s). `killall cosmic-notifications` restarts daemon + panel through cosmic-session, whose per-process exponential backoff grows with every restart.
- Shutdown (`shutdown_daemon`, after `loop.run()`): config monitor, runner, `teardown()` on center,
  shade and toasts (script handler unregistered, `try_close`, window destroyed), a GTK event pump
  (<= 1 s), unown names + unregister, `store.close`, log exit. A failing step is logged and never
  skips later ones. No `os._exit`. Hardening only: the once-reported "corrupted double-linked
  list" abort never reproduced. `scripts/exit-stress.sh` is the manual stress.
- App IDs use `net.corbinmeier.*`: `Notifications` (daemon; control bus name
  `net.corbinmeier.Notifications1`), `NotificationCenter` (window), `CosmicAppletNotifications`.
- The daemon and window run on system `/usr/bin/python3` with GTK3 + `GtkLayerShell-0.1` +
  `WebKit2-4.1`, plain HTML/CSS/JS and no build step. This is the stack proven by
  `~/Code/Dashboards/desktop`. A PlatformIO venv sits ahead of it on PATH, so always invoke
  `/usr/bin/python3` explicitly.
- No runtime dependencies beyond the system typelibs and stdlib: `Gio` for D-Bus, `sqlite3`,
  `tomllib`, `Gio.FileMonitor` for hot reload.
- The daemon and window share one process; the applet calls `ToggleCenter` over the bus.
- Toasts live on one OVERLAY layer surface with one WebView. Keyboard mode is NONE, and the
  surface is resized to fit the stack so it never blocks clicks outside the toasts.
- Notification content is untrusted:
  - Python reduces body markup to an allow-list (`b i u a`) of runs.
  - JS builds DOM from text nodes only, never `innerHTML`.
  - A CSP blocks network access.
  - Links open via `xdg-open`.
- Sound: files play through `pw-play`, falling back to `paplay` (libsndfile 1.2.2). Theme names
  play through `canberra-gtk-play`. A missing file falls back to the default and logs the error.
- The applet is Rust + libcosmic pinned by `rev` `430f3d48a97d8779f9c1985473912e594ccca1ae`,
  the rev in cosmic-applets `328d142` (COSMIC 1.11.0). Re-pin on COSMIC upgrades, following
  `cosmic-applet-timer`'s CLAUDE.md.
- The shim restart-loops `bin/notifyd` and ignores `DAEMON_NOTIFICATIONS_FD`. If it exited,
  cosmic-session would restart the whole panel.
- The shim dies by the signal it received (never exit 0): cosmic-session respawns daemon + panel only after a death by signal.
- Tests and dev runs never claim the bus name on the live session: they use a private bus
  (`dbus-run-session`). Only `scripts/install.sh`, run with the user present, touches the live
  session.
- Never edit the user's panel config; swapping applets is manual in Settings.
- Bell -> shade: left click = `ToggleShade` (falls back to `ToggleCenter` when the daemon has no such
  method). Clicking the bell while the shade is open lands on the click-outside catcher and closes
  it. The popup's "Open notification center" still calls `ShowCenter`. The desktop file has no
  `X-CosmicHoverPopup`, so hovering never opens the popup.
- Shade wiring: `on_dismiss` gets open and EXPIRED-closed ids; open -> `server.dismiss` (signal),
  expired -> `store.mark_dismissed_quiet` (no signal). The shade refreshes via server observers
  (added / changed: mark_read, delete, clear, any close / DND). DND set in the shade goes through
  `server.set_dnd`, so `PropertiesChanged` fires.
- Tests never play audio: `--no-sound` (implied by `--no-display`) uses `NullPlayer`. The applet's
  private-bus test passes both.
- Applet clicks (popup part): right or middle = the popup (header "N unread", last 5
  from `GetHistory("",0,5)` with unread bold, DND toggle on the `DoNotDisturb` property, "Open
  notification center"; a row calls `ShowNotification`, falling back to `ShowCenter`). Offline
  (bus name unowned): button greyed, no click handlers, tooltip "Notification daemon not running".
- Applet badge = `UnseenCount` (received after the `seen.json` watermark, shown/dnd, not dismissed),
  exact to 999 then "999+"; opening the shade or center, `MarkSeen`, or the applet popup resets it
  to 0 (`UnreadCount` stays for bold rows). The reset wrappers live in `build_daemon`
  (`_mark_seen_on_open`); `--state-dir` (default: the db's dir when `--db` is custom).
- Applet icons (`resources/icons/`, installed by `install-applet.sh` into hicolor, shared with the launcher): `net.corbinmeier.NotificationCenter-symbolic` (plain bell), `-new-symbolic` (dot, unseen > 0), `-dnd-symbolic` (slash, DND wins); `IconFallback::Names` falls back to stock `notification-symbolic` / `notification-disabled-symbolic`. The applet desktop `Icon=` is the plain one. Uninstalling the applet keeps the full-color + plain icons while the launcher is installed (and vice versa)
  (both in the Cosmic and Pop themes). Badge beside the bell, hidden at 0 (see the badge decision above).
- Applet live state has no polling: one `Status` refetch per PropertiesChanged,
  NotificationAdded, or NameOwnerChanged (sender-less match rules, so daemon restarts are
  survived). zbus is libcosmic's own 5.x with the `tokio` feature. `NOTIFYD_BUS_ADDRESS`
  points the applet at a private bus (dev and the ignored test only).
- Applet re-pin: same procedure as `cosmic-applet-timer`'s CLAUDE.md ("Re-pin on COSMIC upgrade"):
  take the commit suffix of `dpkg-query -W cosmic-applets`, read libcosmic's rev from
  `git show <commit>:Cargo.lock` in a pop-os/cosmic-applets clone, update `applet/Cargo.toml`,
  `cargo update -p libcosmic`, run `scripts/check.sh`. Start from the timer's `Cargo.lock`.
- Applet private-bus proof: `dbus-run-session -- cargo test --manifest-path applet/Cargo.toml -- --ignored --nocapture`.
- `scripts/check.sh`: ruff (project-local `.venv`, ruff only), `/usr/bin/python3 -m unittest`,
  and in phase 4 the applet's cargo fmt / clippy `-D warnings` / test / build. Last line
  `CHECK PASS`.

## Phases
0. Re-pin `cosmic-applet-timer` to the 1.11 libcosmic rev (that repo).
1. Daemon core: bus server, store, default-theme toasts, shim, install/uninstall.
2. Rules engine, sounds, user CSS, hot reload.
0–2 done and live (2026-10-25).
3. Center window + details page. 3b. Rule `run` scripts, rule `buttons`, snooze
   (`docs/spec.md` § Workflow). 4. Applet. 5. Customization panel. 6. Rules editor (in the
   Settings window's Rules tab; `docs/rules.md` "Editing in the panel").

## Open questions
- None.

@scripts/README.md
