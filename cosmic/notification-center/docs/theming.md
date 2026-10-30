# Theming

The look of toasts, the shade and the center is built from three CSS layers, loaded in this
order. Each re-applies when its file is saved.

1. `web/default.css`: shipped. Layout, plus every look expressed through `--nc-*` custom properties.
2. `theme.css`: written by the Appearance panel (`notifyd/theme.py`). `--nc-*` values only. Lives
   at `~/.config/net.corbinmeier.notifications/theme.css`. Do not hand-edit: the panel rewrites it.
3. `style.css`: hand-written, anything else (selectors, `@keyframes`, per-app looks). Loads last,
   so it wins.

Toasts, the shade and the center all receive `theme.css` followed by `style.css` as one user
stylesheet, after `default.css`. Saving either file (from the panel or by hand) re-applies it
everywhere within about 300 ms. In the panel, Appearance writes `theme.css` (validated, atomic) and
the Advanced tab writes `style.css` (any text up to 256 KiB, never validated). The `--nc-center-*`
variables are declared on `.center` in `center.css`, so `theme.css` writes their overrides in a
`.center { }` rule rather than `:root`.

`theme.css` holds overrides only. A variable that is absent keeps its `default.css` value, so
"reset" means deleting the line.

## Variables

Generated from `notifyd.theme.VARIABLES` (a test fails when this table or the list drifts from
`web/default.css`, the `:root` of `web/shade.css` and the `.center` rule of `web/center.css`;
regenerate with `notifyd.theme.variables_table()`).

| Variable | Group | Type | Default | Controls |
|---|---|---|---|---|
| `--nc-bg` | Colors | color | `#1f2125` | Background |
| `--nc-fg` | Colors | color | `#e8eaed` | Text |
| `--nc-fg-muted` | Colors | color | `#9aa0a6` | Muted text |
| `--nc-accent` | Colors | color | `#8ab4f8` | Accent (links, actions) |
| `--nc-border-color` | Colors | color | `#3a3d44` | Border |
| `--nc-mark-bg` | Colors | color | `rgba(138, 180, 248, 0.25)` | Highlight background |
| `--nc-mark-fg` | Colors | color | `inherit` | Highlight text |
| `--nc-button-bg` | Colors | color | `rgba(255, 255, 255, 0.08)` | Button background |
| `--nc-button-bg-hover` | Colors | color | `rgba(255, 255, 255, 0.16)` | Button background (hover) |
| `--nc-avatar-saturation` | Colors | length | `45%` | Letter avatar saturation |
| `--nc-avatar-lightness` | Colors | length | `42%` | Letter avatar lightness |
| `--nc-avatar-fg` | Colors | color | `#ffffff` | Letter avatar text |
| `--nc-urgency-low` | Urgency | color | `#6b7280` | Low urgency stripe |
| `--nc-urgency-normal` | Urgency | color | `#8ab4f8` | Normal urgency stripe |
| `--nc-urgency-critical` | Urgency | color | `#f28b82` | Critical urgency stripe |
| `--nc-urgency-critical-bg` | Urgency | color | `#2b1f21` | Critical background |
| `--nc-font-family` | Typography | font | `"Inter", "Noto Sans", system-ui, sans-serif` | Font family |
| `--nc-font-size` | Typography | length | `14px` | Font size |
| `--nc-border-width` | Shape | length | `1px` | Border width |
| `--nc-radius` | Shape | length | `12px` | Corner radius |
| `--nc-shadow` | Shape | shadow | `0 6px 20px rgba(0, 0, 0, 0.45)` | Shadow |
| `--nc-opacity` | Shape | number | `0.97` | Opacity |
| `--nc-anim-duration` | Motion | duration | `220ms` | Animation duration |
| `--nc-enter-animation` | Motion | animation | `nc-enter var(--nc-anim-duration) ease-out both` | Enter animation |
| `--nc-exit-animation` | Motion | animation | `nc-exit var(--nc-anim-duration) ease-in both` | Exit animation |
| `--nc-width` | Layout | length | `380px` | Toast width |
| `--nc-gap` | Layout | length | `10px` | Gap between toasts |
| `--nc-padding` | Layout | length | `12px` | Padding |
| `--nc-icon-size` | Layout | length | `40px` | Icon size |
| `--nc-shadow-room` | Layout | length | `20px` | Shadow room (transparent margin so shadows are not clipped) |
| `--nc-image-toast-height` | Image | length | `140px` | Toast image height (max) |
| `--nc-image-shade-height` | Image | length | `96px` | Shade image height (max) |
| `--nc-image-detail-height` | Image | length | `360px` | Center image height (max) |
| `--nc-image-list-size` | Image | length | `36px` | Center list thumbnail size |
| `--nc-image-radius` | Image | length | `6px` | Image corner radius |
| `--nc-shade-width` | Shade | length | `420px` | Shade width |
| `--nc-shade-gap` | Shade | length | `8px` | Gap between shade rows |
| `--nc-shade-surface` | Shade | color | `rgba(255, 255, 255, 0.05)` | Row background |
| `--nc-shade-surface-hover` | Shade | color | `rgba(255, 255, 255, 0.09)` | Row background (hover) |
| `--nc-shade-divider` | Shade | color | `var(--nc-border-color)` | Divider |
| `--nc-shade-unread` | Shade | color | `var(--nc-accent)` | Unread marker |
| `--nc-shade-badge-bg` | Shade | color | `var(--nc-button-bg-hover)` | Badge background |
| `--nc-shade-badge-fg` | Shade | color | `var(--nc-fg)` | Badge text |
| `--nc-shade-anim-duration` | Shade | duration | `180ms` | Shade open/close duration |
| `--nc-danger` | Urgency | color | `var(--nc-urgency-critical)` | Danger (destructive actions) |
| `--nc-center-row-hover` | Center | color | `rgba(255, 255, 255, 0.045)` | Row (hover) |
| `--nc-center-row-selected` | Center | color | `rgba(138, 180, 248, 0.14)` | Row (selected) |
| `--nc-center-bar-bg` | Center | color | `rgba(255, 255, 255, 0.03)` | Toolbar background |
| `--nc-center-title-bg` | Center | color | `var(--nc-bg)` | Title bar background |
| `--nc-center-danger` | Center | color | `var(--nc-urgency-critical)` | Danger |
| `--nc-center-radius` | Center | length | `min(var(--nc-radius), 8px)` | Corner radius |
| `--nc-center-font-size` | Center | length | `13px` | Font size |
| `--nc-center-anim-duration` | Center | duration | `160ms` | Open/close duration |

### Letter avatar

When no icon resolves, every UI shows `span.nc-avatar[data-app]` in the icon box: a circle with the
app's first letter (uppercase, first grapheme). Its hue is a stable hash of the app name, set by
the page as the inline custom property `--nc-avatar-hue` (0-359); saturation, lightness and text
color come from `--nc-avatar-saturation`, `--nc-avatar-lightness` and `--nc-avatar-fg`. Override
one app in `style.css` with `.nc-avatar[data-app="CI"] { background: #345; }`.

Value rules (`theme.validate`): colors are hex, `rgb()/rgba()/hsl()/hsla()` or a named color;
lengths are `px`, `rem`, `em`, `%` (or `0`); durations `ms` or `s`; fonts a comma list of family
names (quoted or bare); shadows `none` or layers of `[inset] x y [blur [spread]] color`;
animations `none` or `nc-enter|nc-exit [duration] [easing] [fill]` (only the two shipped
keyframes). Anything containing `;`, braces, `<`, `>`, a backslash, a newline, `/*`, `url(`,
`expression`, `@import` or `!important` is rejected.

## Presets

Starting points; each is a values dict in `theme.PRESETS` and a reference stylesheet in
`web/presets/<name>.css`. Applying one replaces the overrides in `theme.css` with its values.

| Preset | Look |
|---|---|
| `default` | No overrides: the neutral dark look of `default.css`. |
| `crt-green` | Green phosphor on near-black, VT323, 1px green border, soft glow, square corners. After the lock screen. |
| `night-ops-hud` | Deep console panel, teal signal, 2px dim-teal border, Fira Mono, no rounding, gold highlights. After the desktop dashboard (its chamfered notch needs `style.css`). |

## Editing `config.toml`

`notifyd/config_edit.py` changes single values in `config.toml` without losing comments:
`set_values(text, {"toasts": {"timeout_normal": 12000}})`, `remove_keys`, and the atomic,
tomllib-validated `update_file(path, updates, removals)`. A commented default (`# key = ...`)
becomes a real line right under the comment; missing keys go at the end of their table; missing
tables are appended. It refuses array-of-tables targets and keys that currently hold arrays or
inline tables.

## CSS hooks

Stable selectors for `style.css` (the full lists are in the header comments of `web/default.css`,
`web/center.css`, `web/shade.css` and `web/settings.css`).

- Toasts: `.toast[data-app][data-urgency][data-category]`, `.tag-<name>` and rule classes,
  `.toast-icon .toast-content .toast-app .toast-summary .toast-body .toast-close`,
  `.toast-actions > .toast-action`, `.toast-buttons > .toast-button[data-kind]`, `mark.<class>`.
- Reply actions (all three pages, `web/reply.js`; absent when the notification has none): `.nc-reply[data-state]`
  with `.nc-reply-choices > button.nc-reply-choice[data-choice]`, `textarea.nc-reply-text`,
  `.nc-reply-count(.over)`, `button.nc-reply-send`, `.nc-reply-confirm`, `.nc-reply-status[data-kind]`
  and `.nc-reply-host`. Styled with existing `--nc-*` variables only.
- Letter avatar: `.nc-avatar[data-app]` (hue in the inline `--nc-avatar-hue`).
- Image previews (all three pages, `web/image.js`; absent when the notification has no image or
  `[images] preview = false`): `.nc-image[data-host="toast|shade|center"]` holding
  `img.nc-image-preview` (`[data-open]` in the center, where a click opens the file) and
  `.nc-image-actions > button.nc-image-button[data-kind="copy|path"]` (`[data-state="done|failed"]`
  for about 1.5 s). Center list rows get `.nc-image-thumb > img.nc-image-thumb-img`. Sizes are the
  `Image` group above: `--nc-image-toast-height`, `--nc-image-shade-height`,
  `--nc-image-detail-height` (maximum heights, the picture keeps its aspect), `--nc-image-list-size`
  (square thumbnail) and `--nc-image-radius`. There is no transition, so nothing to reduce.
- Shade: `.shade`, `.shade-group[data-app][data-urgency]`, `.shade-item[data-urgency][data-state]`,
  `.shade-badge`, `.shade-button`.
- Center: everything under `.center`; rows are `.row[data-app][data-urgency][data-state]`.
- Settings preview: the Appearance preview uses the real `.toast` and `.shade-group` markup under
  `.sp-preview`, with the edited `--nc-*` values set on that element only, so `style.css` rules
  for those hooks show up in it too.
