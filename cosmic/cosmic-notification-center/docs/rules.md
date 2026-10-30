# Rules reference

Rules live in `~/.config/net.corbinmeier.notifications/rules.toml`. Saving the file applies it
at once. If the file has an error, the previous good rules stay active and the error (with the
rule and line) is logged: `journalctl --user -t cosmic-session | grep notifyd`, and the Rules tab
shows it. A missing file means no rules. Examples: `examples/rules.toml`.

Every rule is a `[[rule]]` table. Rules run top to bottom; **every enabled rule that matches
applies**. A later rule overrides an earlier one for single-value actions and adds to list
actions. `stop = true` ends the run after that rule.

```toml
[[rule]]
name = "example sound"          # match fields, then actions
app = "Example App"
sound = "bell"
```

A rule with no match fields matches every notification. A key the engine does not know is an
error (so `sumary = ...` is caught instead of silently ignored).

## General keys

| Key | Type | Meaning |
|---|---|---|
| `name` | string, required | Non-empty and unique. Shown in history as the matched rule. |
| `enabled` | bool, default `true` | `false` skips the rule (it is still checked for errors). |
| `match_mode` | `"all"` (default) or `"any"` | Whether every match field must match, or any one. Applies only to the match fields the rule sets. |

## Match fields

| Key | Type | Matches |
|---|---|---|
| `app` | string | The app name, exact, case-insensitive. |
| `app_regex` | regex | Searched in the app name. |
| `summary`, `body` | regex | Searched (anywhere) in the summary or the raw body. |
| `urgency` | `"low"` `"normal"` `"critical"`, or a list | The notification's urgency, case-insensitive. |
| `category` | string or list | The `category` hint, exact and case-sensitive. |
| `[rule.hint]` | table of `key = "regex"` | Each named hint is matched against `str(value)`. A missing hint is no match; every listed key must match. A boolean is `"True"`/`"False"`. |
| `hint_present` | list of keys | All of these hints exist, whatever their value. |

Regexes are Python `re` syntax and are *searched*, not anchored: use `^`/`$` to anchor, and
`(?i)` at the start for case-insensitive. Put single quotes around them (`'\bURGENT\b'`) so
backslashes are not TOML escapes. Only the first 10,000 characters of a field are searched.
Avoid patterns with nested repeats such as `(a+)+`; they can be slow.

```toml
[[rule]]
name = "build bot failures"
app_regex = '(?i)^(jenkins|ci)'
urgency = ["normal", "critical"]
[rule.hint]
"x-status" = '^fail'
```

## Actions

| Key | Type | Effect |
|---|---|---|
| `block` | bool | Not shown and silent, but still recorded (state `blocked`). |
| `store_only` | bool | Recorded silently, no toast (state `store_only`). If one rule sets both, `block` wins; across rules the later rule wins. |
| `sound` | string | A file path (`~` is expanded; mp3/wav/ogg/flac), a sound-theme name, or `"none"` for silence. Overrides the sender's sound hints. |
| `volume` | integer 0-100 | Sound volume. Works with or without `sound`. |
| `timeout` | integer ms > 0, or `"never"` | Toast lifetime. `"never"` keeps it until dismissed. Overrides the app's timeout and the `[toasts]` defaults in `config.toml` (`timeout_low` 5000, `timeout_normal` 10000, `timeout_critical` `"never"`). A toast never closes while the pointer is over the stack. |
| `set_urgency` | `"low"` `"normal"` `"critical"` | Overrides the urgency (so `data-urgency` and later rules see it). The timeout is **not** recomputed; set `timeout` too if you want one. Note `urgency` is the *match* key. |
| `icon` | string | Icon name, file path or `~` path replacing the app's icon. It also outranks an `image-path` hint (the rule drops that hint). |
| `class` | identifier or list | Extra CSS classes on the toast. |
| `highlight` | `{ pattern, class }` or a list of them | Wraps text matching `pattern` in `<mark class="...">`. |
| `tags` | list of identifiers | Your own metadata: stored, searchable, and exposed to CSS as `.tag-<name>`. |
| `dnd_bypass` | bool | Show and sound this notification even in Do Not Disturb. |
| `stop` | bool | If this rule matched, skip all later rules. |

`false` for a flag (`block = false`) does nothing; it never undoes an earlier rule's `true`.

Classes, tags and highlight classes must be CSS-safe identifiers: letters, digits, `_` and
`-`, not starting with a digit or `-` (`urgent`, `build_ok`, `tag-2` are fine; `2fast`, `a b`,
`x.y` are errors).

### Icon resolution

Every UI (toast, shade, center) resolves an app's icon the same way, first hit that is an existing
file wins (`notifyd/icons.py`):

1. the `image-path` hint
2. `app_icon`: a path, `file://` URI, `~` path or themed name (an `icon` rule or the per-app icon
   setting writes here and also removes the `image-path` hint, so it outranks everything below)
3. the `desktop-entry` hint: that .desktop file's `Icon=`
4. a .desktop file in the XDG data dirs (`applications/`) whose `Name=`, `StartupWMClass=` or
   file id equals the app name, case-insensitively (rescanned at most every 60 s)
5. nothing: the page draws a letter avatar (see `docs/theming.md`)

Themed names go through the GTK icon theme at the UI's size (toast 48, shade 32, center 32 and 64
in the details page). The per-app icon (the shade's gear card, field "Icon", with a live preview
and a Clear button; `icon = "..."` in `app-settings.toml`) is checked when saved: a path must exist
or the name must resolve, otherwise the error shows inline. Files without the key still load, and
an icon file deleted later just falls back to the chain.

### Order of effects

Scalar actions (`sound`, `volume`, `timeout`, `set_urgency`, `icon`, state) take the value from
the **last** matching rule that sets them. List actions (`class`, `tags`, `highlight`) append in
rule order with duplicates dropped. The names of all matching rules are recorded, in order.

```toml
[[rule]]
name = "all CI"
app = "CI"
tags = ["ci"]
sound = "bell"

[[rule]]
name = "CI failed"
app = "CI"
summary = '(?i)fail'
tags = ["failed"]        # the toast ends up with tags ["ci", "failed"]
sound = "dialog-error"   # and this sound, replacing "bell"
stop = true
```

## Highlight patterns run in JavaScript

`pattern` is validated with Python at load, but the toast applies it with a JavaScript
`RegExp`. Only a **leading** `(?i)`, `(?s)` or `(?m)` (any combination, such as `(?is)`) is
translated. So for highlights:

- No other inline flags: not `(?x)`, not `a(?i)b`, not scoped `(?i:...)`.
- No Python named groups `(?P<name>...)` (JS uses `(?<name>...)`); use plain groups.
- No `\A` or `\Z`; use `^` and `$`.
- Lookahead `(?=...)` is fine; lookbehind `(?<=...)` works in current WebKit but has quirks,
  so prefer to avoid it.

At load, the daemon logs a warning when a highlight pattern contains syntax it can detect
cheaply (`(?P<`, mid-pattern inline flags, `\A`, `\Z`). The rule still loads. Match fields
(`summary`, `body`, ...) are Python only and have no such restriction.

## Scripts and buttons

A rule can run a command (`run`) and add buttons (`buttons`) to the toast and the details page.

### `run`

| Key | Type | Meaning |
|---|---|---|
| `run` | non-empty string, or non-empty list of non-empty strings | The command. A list is run directly as argv (no shell): `["~/bin/app-log", "--quiet"]`. A string runs through `/bin/sh -c`. |
| `run_when` | `"arrive"` (default) or `"shown"` | `arrive` runs on every match, including blocked and store_only notifications. `shown` runs only for notifications that are displayed. |
| `cooldown` | integer seconds >= 0, default 0 | Minimum gap between runs of this rule, so a chatty app cannot spam it. |
| `run_timeout` | integer seconds 1-3600, default 30 | After this the process is sent TERM, then KILL. |

`run_when`, `cooldown` and `run_timeout` are errors without `run`. Each matching rule with a
`run` adds one run (the rule name is recorded with it). Exit code, duration and the last 4 KB of
output of every run are kept and shown in the **run log** on the notification's details page.

**Notification data arrives only in environment variables:** `NC_ID`, `NC_APP`, `NC_SUMMARY`,
`NC_BODY` (plain text), `NC_URGENCY`, `NC_CATEGORY`, `NC_TAGS`, `NC_HINTS_JSON`, `NC_RULE`, and
`NC_BUTTON` (the button label, for button runs). **Never build a command from notification text**:
anyone who can send a notification controls it, and pasting it into a shell string is command
injection. Write a fixed script and read the variables inside it, quoted (`"$NC_SUMMARY"`).

```toml
[[rule]]
name = "log example"
app = "Example App"
run = ["sh", "-c", "printf '%s\\t%s\\n' \"$NC_SUMMARY\" \"$NC_BODY\" >> ~/app.log"]
```

### `buttons`

A list of tables (`[[rule.buttons]]`), each with a `label` (1-40 characters) and exactly one of
`run` (same forms and environment as above) or `builtin`. Unknown keys are errors; messages name
the rule and the button index (`buttons[1]`).

| `builtin` | Does | `close` default |
|---|---|---|
| `details` | Opens the details page. | false |
| `copy-body` | Copies the body text to the clipboard. | false |
| `copy-summary` | Copies the summary. | false |
| `open-link` | Opens the first link in the body (`xdg-open`; http, https, mailto). | false |
| `mark-read` | Marks the notification read. | true |
| `dismiss` | Closes and dismisses it. | true |
| `snooze` | Shows it again later; needs `minutes` (1-10080). | true |

`minutes` is required for `snooze` and an error otherwise. `close = true|false` overrides the
default (whether pressing the button closes the toast).

```toml
[[rule]]
name = "example open button"
app = "Example App"
hint_present = ["x-nc-url"]
[[rule.buttons]]
label = "Open"
run = ["sh", "-c", "xdg-open \"$(python3 -c 'import json,os; print(json.loads(os.environ[\"NC_HINTS_JSON\"])[\"x-nc-url\"])')\""]

[[rule]]
name = "snooze button"
[[rule.buttons]]
label = "Snooze 15 min"
builtin = "snooze"
minutes = 15
```

Across matching rules `run` entries and buttons are appended in rule order. Duplicate buttons
(same label, run, builtin and minutes) are dropped, keeping the first. A notification gets at
most **6** buttons: extras are dropped and a warning naming their rules is logged. Button ids
are assigned in final order, `b0`, `b1`, ... The web layer sees a button's id, label, kind and
`close`, never its command.

Per-app settings (`app-settings.toml`) do not support `run` or `buttons`.

## Editing in the panel

Settings, **Rules** tab (`#/settings/rules`) edits `rules.toml` without hand-writing it. Hand edits
still work; the panel and your editor can be used on the same file.

- **List:** one row per rule in file order, with an on/off switch, the name and a one-line summary
  (`app = Example App -> sound, 2 buttons`). Drag the handle, or focus it and press the arrow
  keys (Home and End jump), to reorder. **Edit**, **Duplicate** (named `<name> copy`) and **Delete**
  (asks inline first); **Add rule** starts a new one.
- **Editor:** name and enabled; the match fields (regex fields show a live "regex valid" mark
  checked by the daemon's own Python `re`, so it is the answer the engine will give); the actions;
  the script (shell string or argument list, `run_when`, cooldown, time limit, with the
  `NC_*` warning); up to 6 buttons. Every field has a `?` with the wording of this page. **Save**
  writes the file (the daemon hot-reloads it at once), **Revert** drops the draft, and leaving with
  unsaved changes asks first. Errors show under the field they belong to.
- **Test this rule:** a sample notification (prefilled from the newest notification in history)
  is run through your *unsaved draft*, alone and inside the full ruleset (the draft takes the
  place of the saved rule; app settings run first, as in the daemon). It shows whether the rule
  matched and a toast preview with the real classes, highlights and buttons. It is a dry run: no
  command runs, no sound plays, nothing is saved, and a command never appears in the result.
- **Edit as text:** the whole file in a text box. A save that fails validation writes nothing and
  shows the daemon's message with the line (a rule error names its `[[rule]]` line and the page
  jumps to the offending key).

What the panel never touches: a rule you do not change is written back byte for byte, and so are
the comments around it. The file is split into blocks: one per `[[rule]]` (its `[[rule.buttons]]`
and `[rule.hint]` tables included), with the comment lines directly above the header as its lead
and the blank lines or free-floating comments after it as its tail. Moving or deleting a rule takes
its lead and tail with it. Editing a rule keeps its lead and tail, and in the rule itself keeps the
line, order and inline comment of every key whose value did not change; a changed key gets a new
value on the same line, a removed key loses its line, a new key goes in after the last key that
comes before it in the usual order (`name`, `enabled`, `match_mode`, match fields, actions, script).
A changed `[rule.hint]` or `[[rule.buttons]]` is rewritten under its header; unchanged ones stay
verbatim. A rule the panel cannot patch safely (dotted keys, odd layouts) is rewritten cleanly, its
lead and tail kept. Values the panel writes: `false` flags and default values (`enabled = true`,
`match_mode = "all"`, `run_when = "arrive"`, `cooldown = 0`, `run_timeout = 30`) are left out unless
the rule already had that key; regexes use single quotes when they contain a backslash.

Every write is validated by the same loader the daemon uses (a bad edit writes nothing). If the
file cannot be listed (a `rule = [...]` array, a table other than `[[rule]]`, a TOML syntax error)
the tab shows the problem and offers **Edit as text**.

## Errors

A failed load reports the rule number and name, the line of its `[[rule]]` header, and what is
wrong, for example:

```
rule #2 ('typo'), line 4: unknown key(s): sumary (check for a typo; see docs/rules.md)
```

TOML syntax errors keep the parser's line and column. Unknown keys, wrong types, duplicate or
empty names, bad regexes, out-of-range numbers and non-identifier classes or tags are all errors.

## Styling

`class` values land on the `.toast`; tags become `.tag-<name>`; highlights become
`mark.<class>`:

```css
mark.urgent { color: #ff3b3b; font-weight: 700; background: none; }
.toast.pinned { border-left: 3px solid var(--accent); }
```

## Runtime-only fields

`dnd_bypass` and `volume` become `Notification.dnd_bypass` and `Notification.sound_volume`;
`run` becomes `Notification.runs`. They exist only while the notification is processed and are not stored in history.

## Per-app settings (`app-settings.toml`)

The notification center's per-app settings (the gear on each app) are saved to
`app-settings.toml` in the same config dir. The UI manages it: hand edits are allowed and apply
live, but comments are not preserved when the UI saves. A missing file means no settings; it is
created on the first save. A bad edit is rejected (previous settings stay, error with the line is
logged and signalled as RulesReloaded(false, "app-settings.toml: ...")).

**Evaluation order:** app settings run first, then `rules.toml`. So your own rules always win:
an app set to sticky plus a rule with `timeout = 5000` ends at 5000. The per-app UI never writes
`rules.toml` (only the Rules tab does, see "Editing in the panel"). Matched app entries show up in history as the rule `app: <App>`.

Each `[[app]]` has `name` (matched exactly, case-insensitive; the same app twice is an error)
and only the non-default prefs:

| Pref | Becomes |
|---|---|
| `muted = true` | `store_only` (history and badge, no toast or sound) |
| `silent = true` | `sound = "none"` |
| `sound = "<file or theme name>"` | `sound` (ignored when `silent`) |
| `volume = 0..100` | `volume` |
| `sticky = true` | `timeout = "never"` |
| `blocked = true` | `block` (wins over `muted`) |
| `dnd_bypass = true` | `dnd_bypass` |

```toml
[[app]]
name = "Slack"
sticky = true
sound = "/home/me/sounds/ping.oga"
volume = 60
```
