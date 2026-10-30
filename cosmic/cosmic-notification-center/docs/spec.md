# cosmic-notification-center: spec (approved 2026-10-24)

Replaces COSMIC's notification service with our own CSS-driven daemon, a reading window,
and a panel applet.
Contract summary: `CLAUDE.md`. This file holds the full detail it points to.

## Why
- The stock bell applet panics (`Downcast on stateless state`, pop-os/cosmic-applets#1536), and
  after that the daemon keeps a dead connection (pop-os/cosmic-notifications#116). Neither is
  fixed in COSMIC 1.11.0.
- No sounds today. The stock daemon advertises the `sound` capability, so apps leave sound to
  it, but it plays nothing by default and has no setting for it.
- The user wants a window to read through notifications, a full rules engine, and a look
  controlled by CSS.

## Scope
- **Daemon** (`notifyd`): owns `org.freedesktop.Notifications` (spec 1.2: Notify,
  CloseNotification, GetCapabilities, GetServerInformation; signals NotificationClosed,
  ActionInvoked, ActivationToken). It also:
  - runs every notification through the rules engine
  - records each one to SQLite
  - draws the toasts as HTML/CSS
  - plays the sounds
- **Center window**: a normal window showing:
  - a list grouped by day, then app (or by app only, a toolbar choice), searchable with SQLite FTS5
  - every day and app group collapses and expands (header click, Enter/Space, h/l or arrows,
    "Collapse all" / "Expand all"); the state is kept in `~/.local/state/net.corbinmeier.notifications/center.json`
    (defaults: today open, older days closed, groups open); a search or filter opens every group
    that holds a match without changing the saved state
  - filters by app, tag and state
  - the full body of each notification, with its action buttons
  - mark read, delete one, clear all
  - a rules-file status: last reload, and any error with its line number
- **Customization panel** (✔ user ask: "adjust everything"): a Settings area in the window.
  Every change previews live on a sample toast, and a "Send test notification" button sends a
  real one through the full pipeline.
  - **Appearance:** controls for every theme variable:
    - colors (background, text, accent, per-urgency)
    - font family and size
    - corner radius, border, shadow, opacity
    - toast width and spacing
    - enter/exit animation
    - urgency, highlight, and per-app accent colors

    Presets to start from: Default, CRT Green (lock screen), Night Ops HUD (dashboard).
    An "Advanced" tab edits the raw `style.css`.
  - **Behavior:**
    - anchor corner and margins
    - max visible toasts
    - default timeouts per urgency
    - DND, plus which rules bypass it
    - history retention
  - **Sounds:** default sound (system default, or a file path with a file picker), master
    volume, and a "play" button beside every sound field.
  - **Rules** (phase 6, `notifyd/rules_edit.py`, `web/rules.js`): a list editor that writes
    `rules.toml`. Hand-editing the file still works, and the editor keeps everything it does not
    change, comments included (the file is edited block by block; the rest stays byte for byte).
    - List: drag handle (arrow keys too), on/off switch, name, one-line summary, Edit,
      Duplicate, Delete (confirmed in the page), Add rule.
    - Editor form: name and enabled; match mode, app, app/summary/body regexes with a live
      "regex valid" mark (checked by the daemon's own validator), urgency, category, hint
      table, hint_present; block / store_only, sound with play, volume, timeout, set_urgency,
      icon, classes, highlights, tags, dnd_bypass, stop; run (shell string or argv list),
      run_when, cooldown, run_timeout with the NC_* environment warning; up to 6 buttons.
      A help tooltip on every field, errors inline per field, Save / Revert, and an
      unsaved-changes guard.
    - "Test this rule": a sample notification (prefilled from the newest in history) run
      through the unsaved draft, alone and inside the full ruleset, as a dry run (no commands,
      sounds or writes), with a toast preview that uses the real classes.
    - "Edit as text": the raw file, validated on save with the error's line.
    - Writes go through `SettingsService.rules_*` and the daemon's `RuleSet.from_toml`; the
      existing file monitor hot-reloads the result.
- **Applet**: a bell in the panel with a "new" badge (`UnseenCount`: notifications that arrived
  since the shade or center was last opened; exact up to 999, then "999+"). Left-click toggles the
  shade; a menu toggles DND. It is the only part not drawn with CSS.
- **Rules** (`rules.toml`): an ordered list. Every matching rule applies, a later rule
  overrides an earlier one field by field, and `stop = true` ends evaluation.
  - Match on any of:
    - `app` (exact or regex)
    - `summary` / `body` regex
    - `urgency`, `category`
    - any hint key or value (`hint.<key>`)
  - Actions:
    - `block`: not shown and silent, but still recorded
    - `sound` (theme name, file, or `"none"`) with `volume`
    - `timeout` (ms, or `"never"` so it doesn't auto-dismiss)
    - `urgency` override
    - `class` (extra CSS classes)
    - `highlight` (regex → `<mark class="…">` around the matched text)
    - `tags` (your own metadata: stored, searchable, exposed to CSS)
    - `icon` override
    - `store_only` (record silently, no toast)
  - Saving the file (or the Rules panel) applies it right away. A new *kind* of action is a
    code change, a new rule is not.
- **Styling** is a CSS contract, built from three layers that each re-apply when saved:
  1. `default.css`, shipped: layout, plus every look expressed through `--nc-*` custom properties
  2. `theme.css`, written by the Appearance panel: `--nc-*` values only
  3. `style.css`, hand-written: anything else

  Each element has stable hooks: Every element has stable hooks:
  - `.toast`
  - `[data-app]`, `[data-urgency]`, `[data-category]`
  - `.tag-<name>`, plus rule `class` values
  - `mark.<class>`
  The center window uses the same contract under `.center`.
- **History**: every notification is written to the database, including blocked ones and
  ones that arrive during DND, each with its state flag. Stored fields:
  - app name, summary, body, urgency, category
  - all hints as JSON
  - actions
  - matched rule ids, tags
  - state: `shown | blocked | dnd | store_only`, with dismissed/expired/read timestamps
  Default retention is forever, configurable in days. Raw `image-data` bytes are not stored;
  icon name or path is.
- Out of scope: sounds an app plays through its own audio engine.

## Example rules (from the user's asks)
```toml
[[rule]]                     # an app sends: notify-send --app-name="Example App" --urgency=…
name = "example sound"
app = "Example App"
sound = "~/Music/sounds/example.mp3"          # a path to .mp3/.wav/.ogg/.flac is played as-is;
                                          # leave out for the system default sound

[[rule]]
name = "URGENT stays and glows red"
summary = '(?i)\bURGENT\b'
body = '(?i)\bURGENT\b'                   # any of summary/body: see match_mode
match_mode = "any"
highlight = { pattern = '(?i)\bURGENT\b', class = "urgent" }
timeout = "never"

[[rule]]
name = "mute noise"
body = 'regex-to-block'
block = true
```
```css
mark.urgent { color: #ff3b3b; font-weight: 700; background: none; }
.toast[data-app="Example App"] { border-left: 3px solid var(--accent); }
```

## Decisions (proposed; ✔ = taken from the user's answers)
- ✔ App IDs use the `net.corbinmeier.*` prefix:
  - `net.corbinmeier.Notifications`: daemon, and the control bus name
    `net.corbinmeier.Notifications1`
  - `net.corbinmeier.NotificationCenter`: the window
  - `net.corbinmeier.CosmicAppletNotifications`: the applet
- ✔ CSS-driven, so the daemon and window use the stack the desktop dashboard already proves
  on this machine (`~/Code/Dashboards/desktop`):
  - system `/usr/bin/python3`
  - GTK3 + `GtkLayerShell-0.1` + `WebKit2-4.1`
  - plain HTML/CSS/JS, no build step
  This **replaces** the rev-1 idea of forking cosmic-notifications, because iced/libcosmic has
  no CSS.
- ✔ The applet is Rust + libcosmic, like `cosmic-applet-timer`. cosmic-panel only hosts
  libcosmic applets. libcosmic is pinned by `rev` to `430f3d48a97d8779f9c1985473912e594ccca1ae`,
  the rev in cosmic-applets `328d142` / COSMIC 1.11.0. It talks only to
  `net.corbinmeier.Notifications1` (`ToggleCenter`, `UnreadCount` property + signal, DND
  property).
- ✔ One process for the daemon and the window. The applet calls `ToggleCenter`, and the window
  is a WebView in the same process, so they share the DB connection and rule state.
- Toasts are drawn on one layer-shell OVERLAY surface holding one WebView with the whole stack:
  - Keyboard mode is NONE, so toasts never steal focus.
  - The surface resizes to fit the stack (JS reports its height), so it never blocks clicks
    outside the toasts.
  - Anchor corner and margins are set in `config.toml`.
- Notification content is untrusted. Text goes in only as DOM text nodes, and the spec's markup
  subset (`b i u a`) is rebuilt from an allow-list. Highlights wrap text nodes; nothing is
  inserted with `innerHTML`. A CSP blocks all network access, and links open through
  `xdg-open`, never inside the WebView.
- D-Bus uses `Gio.DBusConnection` / `own_name`, with no extra dependencies. SQLite uses the
  stdlib. TOML uses `tomllib`. Hot reload uses `Gio.FileMonitor`.
- ✔ Sounds are set by file path, and the file is played as-is.
  - A rule or the default may give a path (`~` expanded) or a theme name. Left unset, the
    system default plays: `message-new-instant` from the system sound theme, currently `Yaru`.
  - Files play through `pw-play`, falling back to `paplay`. Both use libsndfile 1.2.2, which
    reads MP3, WAV, OGG and FLAC.
  - Theme names play through `canberra-gtk-play`, which follows the system sound theme.
  - A missing or unreadable file falls back to the system default, logs the error, and shows
    it in the window. `suppress-sound`, `sound-file` and
  `sound-name` hints are honored unless a rule says otherwise. DND silences everything except
  rules marked `dnd_bypass = true`.
- Config lives in `~/.config/net.corbinmeier.notifications/`: `config.toml`, `rules.toml`,
  `style.css`. Data lives in `~/.local/share/net.corbinmeier.notifications/history.db`.
- Install adds a `~/.local/bin/cosmic-notifications` shim (the same trick as `cosmic-crt-lock`'s
  `cosmic-greeter` shim):
  - The shim is a restart loop around `notifyd`. If it exited, cosmic-session would restart
    the whole panel.
  - It ignores `DAEMON_NOTIFICATIONS_FD`.
  - Uninstall deletes the shim, then `killall cosmic-notifications` brings back the stock daemon (a swap, not a restart: cosmic-session restarts the panel too, with a per-process exponential backoff that grows with every restart; to restart only the daemon use `bin/notifyd --restart`).
  - Every change outside the repo is logged in `docs/system-changes.md` with a revert command.
- Swapping the stock bell for ours stays manual in Settings → Panel → Applets: never edit the
  panel config.
- `scripts/check.sh` runs ruff (project-local `.venv`, ruff only), `/usr/bin/python3 -m unittest` (rules engine, sanitizer, DB, D-Bus spec conformance on
  a private bus), and cargo fmt / clippy `-D warnings` / test / release build for the applet.
  Its last line is `CHECK PASS`.

## Toast timing and clicks (✔ user decisions 2026-10-25)
- **Hover hold:** the expiry timer keeps running while the pointer is over a toast, but a toast
  never closes while hovered. If its timer ran out during the hover, it closes when the pointer
  leaves, after `hover_leave_grace_ms` (config, default 1000). Hovering anywhere on the toast
  stack holds every toast in it.
- **Configurable defaults:** `[toasts]` in `config.toml`, hot-reloaded:
  - `timeout_low`, `timeout_normal`, `timeout_critical`: ms or `"never"`. Defaults are 5000,
    10000 and `"never"`.
  - `respect_app_timeout`: default true. When true, an app's explicit `expire_timeout` > 0
    wins over the defaults. 0 always means never.
  - `hover_leave_grace_ms`: default 1000.
  Special cases are rules (`timeout = …`), which always win over defaults and the app.
- **Click on the toast body (option c):** runs the app's `default` action when it sent one;
  otherwise opens the details page. The close (×) button only dismisses.

## Dashboard shade (✔ user decision 2026-10-25: option B, CSS shade)
- Clicking the applet bell toggles a **shade**: a drop-down panel drawn by the daemon in
  HTML/CSS on a layer-shell surface, styled with the same `--nc-*` variables and `style.css`.
  It is modeled on Android's notification shade.
  - Anchored by `[shade] anchor` (default `top-right`) and `margin` in `config.toml`,
    with `width` defaulting to 420.
  - Keyboard mode is ON_DEMAND: Esc closes it.
  - Clicking outside closes it, via a transparent full-screen catcher surface on the layer
    below. The bell (`ToggleShade` over the bus) toggles it.
  - Opening it closes every toast on screen at once, sticky and hovered ones included, as
    EXPIRED (`NotificationClosed` reason 1): they stay in the shade, not dismissed, not marked
    read. `[shade] clear_toasts_on_open = false` turns it off; it applies on save.
  - Opening it (and showing the center) moves the seen watermark to now (`seen.json` in
    `~/.local/state/net.corbinmeier.notifications/`), so the applet's `UnseenCount` badge drops to 0;
    `MarkSeen` does the same over the bus. The popup header and tooltip read "N new · M unread".
- **Contents:**
  - **Top bar:** DND switch, unread count, "Clear all", and "Open notification center".
  - **Groups**, one per app, newest group first:
    - the header shows icon, app name, count, and the newest summary
    - it expands or collapses on click, and expand state is remembered per app in
      `~/.local/state/net.corbinmeier.notifications/shade.json`
    - group actions: dismiss all, mark all read, and ⚙ for that app's settings
  - **Items:** summary, body (2 lines collapsed, full when expanded), time and the app's
    action buttons. Clicking an item opens the details page; × dismisses it.
- **Which notifications show:**
  - state `shown` or `dnd`, not closed, or closed only by EXPIRY: an expired toast stays in
    the shade until you dismiss it, as on Android
  - dismissing from the shade or "Clear all" marks the notification dismissed
  - blocked and store_only notifications appear only in the center window
- **Per-app settings (⚙)** are saved to `app-settings.toml`, a UI-managed file in the config
  dir. It is applied *before* the hand-written `rules.toml`, so the user's rules always win,
  and the per-app UI never writes `rules.toml` (only the Rules tab does). Settings per app:
  - mute (store_only)
  - silent (sound "none")
  - its own sound file, with a volume
  - sticky (timeout never)
  - block
  - DND bypass
  The center window's details page links to the same panel.

## Workflow: scripts, buttons, details page (added 2026-10-25, user ask)
- **Details page**: the center window opens on one notification at `#/n/<id>`, through the new
  control method `ShowNotification(u id)`. It shows:
  - the full sanitized body, app and icon, and when it was received, read and closed
  - urgency and category
  - every hint, shown as a key/value table
  - tags, matched rules, and the sound that played (or the sound error)
  - the run log
  - the app's own actions (shown as unavailable once the sender is gone)
  - rule buttons, plus "copy body" and "open link" for each link in the body
- **Rule `run`** runs a command when a rule matches:
  - `run = ["~/bin/app-log", "--quiet"]` runs the program directly (argv, no shell).
    `run = "cmd …"` runs through `/bin/sh -c`.
  - Notification data reaches the command **only through environment variables**:
    `NC_ID`, `NC_APP`, `NC_SUMMARY`, `NC_BODY` (plain text), `NC_URGENCY`, `NC_CATEGORY`,
    `NC_TAGS`, `NC_HINTS_JSON`, `NC_RULE`. It is never interpolated into the command string, so a
    crafted notification can't inject shell syntax.
  - Each run has a timeout (default 30 s, then TERM, then KILL).
  - A per-rule `cooldown` (seconds) and a global cap of 4 concurrent runs stop a spamming app
    from fork-bombing.
  - Exit code, duration and the last 4 KB of stdout/stderr are stored in a new `runs` table
    (schema v2 migration) and shown on the details page.
  - `run_when = "arrive"` (default) runs on every match, including blocked and store_only.
    `"shown"` runs only for displayed notifications.
- **Rule `buttons`** add buttons to the toast and the details page, as a list of tables with
  `label` plus one of:
  - `run`: same forms and environment as above
  - `builtin`, one of:
    - `details`: open the details page
    - `copy-body`, `copy-summary`
    - `open-link`: the first link in the body
    - `mark-read`, `dismiss`
    - `snooze`: needs `minutes`; re-shows the notification later and survives a daemon restart
      via the DB
  - optional `close = true|false`: whether pressing the button closes the toast
- **Copy** uses the Wayland clipboard from GTK (no `wl-copy` needed). **Links** open through
  `xdg-open`, http/https/mailto only, as for body links.
- **App side (optional):** an app can attach `--hint=string:x-nc-url:<issue url>` and
  `--action=open=Open`. Rules can match `hint.x-nc-url`, and an "Open" button can use it through
  `$NC_HINTS_JSON`. Things can add buttons to the notification system this way; the change belongs to the sending app and is not part of this project.

## Window open latency (added 2026-10-27, user ask: "a pause, then it shows")
- Every open of the center or the shade logs one INFO line, ms measured from the show request:
  `center open: built=no present=3ms tail=1ms painted=212ms` and
  `shade open: built=yes present=41ms tail=2ms rows=95ms painted=240ms resized=310ms`.
  - `built`: the WebView had to be created for this open.
  - `present`: map/`present()` returned. `tail`: the open handlers after it returned (toast clear,
    seen watermark). `rows`: the shade page rendered the refreshed rows. `painted`: the page
    reported a frame after becoming visible (`web/paint.js`: two animation frames, then a `perf`
    message). `resized`: the last surface resize after the rows (shade only). `-` = never arrived.
  - Only numbers and the built flag are logged, never notification text. The shade line waits
    400 ms after rows and painted for a late resize; a line still pending is written when the next
    open starts.
- **Prewarm** (`[windows] prewarm`, default true, needs a restart): 3 s after both bus names are
  acquired the center view is built hidden, 1 s later the shade view, so the first open skips the
  web process spawn and the page load. Prewarm maps nothing: the window and the layer-shell
  surfaces (shade and click-outside catcher) are created but never shown, so nothing appears and
  nothing grabs input. Cost: two idle WebKit web processes held for the daemon's lifetime. A
  prewarmed shade page lays out in an unmapped view, so its size reports are ignored until the
  surface has been shown once. `prewarm = false` restores the lazy build on first open.

## Image previews (added 2026-10-27, user ask: screenshots shown in the panel and in the toast)
- **Which image:** `notifyd/images.py`, pure. The `image-path` / `image_path` hint (plain absolute
  path or `file://` URI), else a body that is, once stripped, exactly one absolute path or
  `file://` URI. COSMIC Screenshot sends only the second form (`body` = the saved PNG's path, no
  image hint). A candidate must, after `realpath`, be a regular file with extension png, jpg,
  jpeg, gif, webp or bmp, whose magic bytes say the same format, and at most 25 MiB. Otherwise
  there is no preview and no error (a file deleted later behaves the same). SVG is never
  accepted. Image bytes (`image-data`) are still dropped and never stored; the DB holds no image.
- **Serving:** the pages never get `file://` access to the image. `notifyd/image_ui.py`
  registers the URI scheme `nc-image:` once on the WebKit default context (all three WebViews use
  it); the pages' CSP gains `img-src ... nc-image:` only. A page URL is
  `nc-image://img/<notification id>/<version>`; the handler parses it strictly (id and version
  only), looks the notification up, resolves ITS image again with the same checks, reads it from
  one descriptor (no symlink follow, size and magic re-verified on those bytes) and answers with
  the verified MIME type, or an error. A page cannot name a path, and the payload carries only
  `{uri, mime, name}`.
- **Where:** toasts (thumbnail, `--nc-image-toast-height`), the shade (thumbnail on the row,
  `--nc-image-shade-height`), the center (list-row thumbnail `--nc-image-list-size`; detail view
  large preview `--nc-image-detail-height`, a click opens the file with
  `Gio.AppInfo.launch_default_for_uri`). Toast, shade and center detail also get **Copy image**
  and **Copy path** buttons, which stop the click (no dismiss, no default action) and answer with
  a brief "Copied" / "Failed" on the button.
- **Copy image:** `wl-copy --type <mime>` with the file as stdin (an argv list, no shell, at most
  3 s); when `wl-copy` is missing or fails, the GTK clipboard gets the image (`set_image`). **Copy
  path** goes through the daemon's existing text clipboard and copies the path as the sender gave
  it.
- **Messages** (all three pages; `id` must be a notification that has an image now, else dropped):
  `image_copy`, `image_copy_path`, `image_open`; the outcome returns as
  `window.nc.imageResult({id, kind, ok})`. A toast accepts them for toasts on screen only.
- **Config:** `[images] preview = true` (live; off hides every preview and refuses requests).
  Shown in the Settings panel's Behavior tab. Theme variables: the `Image` group in
  `docs/theming.md`.
- **Known limits:** the page scales the original file (no thumbnail cache); a toast grows when
  the picture finishes loading. An `image-path` that is a small avatar also previews.

## Phases (each ends at CHECK PASS and a live check)
0. Re-pin `cosmic-applet-timer` to `430f3d4` and verify its popup in the live panel.
   This proves the rev works before the applet depends on it.
1. Daemon core: spec D-Bus server, DB recording, default-theme toasts, shim,
   install/uninstall, system-changes log. Fixes "it never worked".
2. Rules engine, sounds, user `style.css` and hot reload, plus the three example rules.
3. Center window: list, search, filters, read, actions, delete and clear, plus the details
   page (`#/n/<id>`, `ShowNotification`).
3b. Workflow: rule `run`, rule `buttons` + built-ins, the `runs` table (schema v2), snooze.
4. Applet: badge, toggle, DND.
4b. Dashboard shade + per-app settings (`app-settings.toml`); the bell toggles the shade.
5. Customization panel: Appearance (with presets), Behavior, Sounds, test-notification button.
6. Rules panel: list editor that writes `rules.toml` (`notifyd/rules_edit.py`, the `rules_*`
   bridge messages, the Rules tab in `web/rules.js`).

## Open questions
- None blocking. The look is chosen in the Appearance panel. The neutral Default preset ships
  first; the CRT Green and Night Ops HUD presets follow in phase 5.
