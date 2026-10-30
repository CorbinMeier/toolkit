---
paths:
  - "notifyd/**"
  - "web/**"
  - "applet/**"
  - "scripts/**"
  - "bin/**"
  - "tests/**"
---
# Code layout

- `notifyd/`: Python package for the daemon and the window host
  - `__main__.py`: entry point; `Runtime` holds the live RuleSet, SoundPlayer and user CSS, and
    watches the config dir
  - `config.py`: `Config`, `load_config` and `FIELDS`, the single per-key validator table for
    `config.toml` (the loader and the panel share it; `restart` marks keys the daemon does not
    apply live)
  - `settings.py`: `SettingsService(runtime, server, config_dir)`, the customization panel's
    backend (theme.css, style.css, config.toml, sound preview, test notifications); assigned as
    `center.settings` by `build_daemon`; raises `SettingsError` with per-key messages
  - `model.py`: the notification record and its JSON for the web layer
  - `store.py`: SQLite history
  - `dbus_server.py`: both bus interfaces; `process` (rules) and `sound` hooks, DND bypass
  - `rules.py`: `rules.toml` parser and engine (`RuleSet`)
  - `rules_edit.py`: the Rules tab's editor of `rules.toml`: block-level and comment-preserving
    (`list_rules`, `update_rule`, `add_rule`, `delete_rule`, `move_rule`, `set_enabled`), every
    result checked by `RuleSet.from_toml`; `RulesEditError({field path: message})`. Reached through
    `SettingsService.rules_*` (also `rules_test`, a side-effect-free dry run) and the bridge's
    `rules_*` messages
  - `app_settings.py`: per-app prefs (`AppPrefs`, `AppSettings`) <-> `app-settings.toml`; builds a
    `RuleSet` that runs before `rules.toml`. `Runtime.get/set_app_prefs` is the UI's API
  - `sound.py`: `choose` (which sound), `SoundPlayer` (pw-play, paplay, canberra) and `NullPlayer`
    (same API, spawns nothing; used by `--no-sound` and `--no-display`)
  - `shade.py`: dashboard shade (`ShadeWindow`, display-free `ShadeBridge`); wired in `__main__`
    by `shade_factory(server, runtime)` + `shade_callbacks`, `NullShade` under `--no-display`
  - `opentrace.py`: `OpenTrace`, the one-INFO-line open-latency log of the center and the shade
    (`center open: built=no present=3ms tail=1ms painted=212ms`); the page side is `web/paint.js`
    (`perf` message, validated by both bridges) and the shade's `rows` report
  - `toasts.py`: layer-shell + WebKit toast host; `teardown_webview` is the shared step list behind
    each window host's idempotent `teardown()` (`ToastHost.close(id)` only removes one toast)
  - `http_actions.py`: sender-defined reply actions (`x-nc-actions` hint -> one HTTP request): `parse_hint`, `normalize_url`, the `http-actions.toml` allowlist, `HttpActions` (`plan`, `send` in a worker thread); the server side is `NotificationServer.reply_*` in `dbus_server.py`; `docs/reply-actions.md`
  - `images.py`: image previews, pure: `resolve(hints, body)` (the `image-path` hint, else a body
    that is one path/`file://` URI; realpath, regular file, extension + magic, <= 25 MiB),
    `read_image`, `copy_image` (`wl-copy --type <mime>`, file on stdin, no shell)
  - `image_ui.py`: `ImageService` (one per daemon, an `images` attribute on the toast host and the
    shade/center bridges; `info` payload, `serve`, `handle` for `image_copy`/`image_copy_path`/
    `image_open`), `register_scheme` (the `nc-image://img/<id>/<ver>` handler on the WebKit default
    context); `web/image.js` renders it on all three pages; `docs/spec.md` "Image previews"
  - `sanitize.py`: body-markup allow-list
  - `reply_ui.py`: sender-defined reply actions, UI side: `clean_reply` (allow-list of the `reply` the pages
    get; never URL, body or token), `ReplyRouter` (validates `reply_choice/text/confirm/cancel`, calls the
    hosts' `on_reply_*`, answers with `window.nc.replyResult`); `web/reply.js` renders it on all three pages
  - `center.py`: center window host (`CenterWindow`) and its display-free `CenterBridge`
  - `interfaces/*.xml`, `schema.sql`
- Config dir `~/.config/net.corbinmeier.notifications/` (the parent of `--config`), watched by a
  `Gio.FileMonitor` on the directory with a 300 ms debounce; each save is logged at INFO:
  - `rules.toml`: swapped in on success (RulesReloaded true); on error the old rules stay and
    RulesReloaded(false, msg) is emitted
  - `app-settings.toml`: UI-managed per-app prefs, applied before `rules.toml` (user rules win);
    same error handling; created on first save, so install.sh does not write it
  - `theme.css` and `style.css`: sent as `theme.css + "\n" + style.css` to the toasts, shade and
    center (`set_user_css`); either missing means empty; both watched
  - `config.toml`: `[sound]`, `[shade]` (`anchor`, `margin`, `width`, `clear_toasts_on_open`, each validated alone, via
    `shade.configure`), `[images] preview` (read live by the `ImageService`) and the `[toasts]` timeouts/grace/hover_max_hold_ms apply live; `[toasts]`
    `anchor`, `margin`, `[history]` and `[windows] prewarm` (build the center and shade views
    hidden after startup, default true) need `bin/notifyd --restart`
  - `http-actions.toml`: the reply-actions allowlist (hand-edited, never written by the daemon); missing = empty; reloaded on save, a bad file leaves it empty (fails closed) and signals RulesReloaded(false, msg)
  - `scripts/install.sh` writes defaults for all three if absent and never overwrites them
- `web/`: plain HTML/CSS/JS served to the WebViews (`toasts.*`, `center.*`, `default.css`)
- `applet/`: Rust + libcosmic panel applet (phase 4), binary `cosmic-applet-notification-center`
  - `src/model.rs`: pure state (badge, icon, tooltip, popup rows) with the unit tests
  - `src/bus.rs`: zbus client of `net.corbinmeier.Notifications1`; `updates` streams `Status`; an
    `#[ignore]`d test drives a real `bin/notifyd` on a private bus
  - `src/app.rs`: the libcosmic `Application` (panel button, popup)
  - `resources/net.corbinmeier.CosmicAppletNotifications.desktop`
- `bin/notifyd`: launcher. `scripts/`: check, install, uninstall, install-applet, uninstall-applet
- `tests/`: stdlib `unittest`
