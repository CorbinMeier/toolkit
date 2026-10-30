---
paths:
  - "web/index.html"
  - "web/app.js"
---

# Frontend CSS and SVG rules (`web/`)

- **The vendored Tailwind v4 browser build only emits an `@theme` token onto
  `:root` when its scanner sees the token's name inside an HTML class
  attribute** — a plain utility class (`text-warm`), or the bare `var(...)`
  name spelled out inside `[...]` arbitrary-value syntax (`border-[var(--x)]`
  counts; a `var()` reference buried in a separate `<style>` tag or in
  `app.js` does not, since the scanner never parses those). `--color-warm`
  was for a long time emitted only as a side effect of the offline banner
  carrying `text-warm`; recoloring that banner (2026-08-20, cyberpunk
  restyle) silently broke every *other* `var(--color-warm)` consumer
  app-wide (sun icon, hot-threshold bars, sparkline gradient, sun arc) until
  caught by a live `getComputedStyle` check. Fix/pattern: any color token
  consumed only via plain `var()` — in this stylesheet or in `app.js` — must
  also be restated in the plain (non-`text/tailwindcss`) `<style>` block's
  own `:root{}` rule, which the browser parses unconditionally; `@theme`
  keeps its own copy purely so the Tailwind utility classes that do exist in
  the markup keep compiling. Both blocks in `web/index.html` now carry the
  full palette for exactly this reason — don't de-duplicate them.
- **Historical (both resolved by #57's move off `<table>`, kept for the
  general CSS mechanism they document):**
  - A `td`-level `pl-[...]`/`pr-[...]` Tailwind utility class used to
    silently do nothing in the System tables: `.sys-table td{padding:0.32vmin
    0}` (a class+element selector) has *higher* specificity than a
    single-class Tailwind utility like `.pl-\[0\.9em\]{padding-left:0.9em}`,
    so the shorthand's `0` left/right padding always won regardless of
    source order. General lesson: a compound class+element selector beats a
    single-class utility on specificity regardless of source order — set
    `element.style.paddingLeft` directly when that matters, don't fight it
    with another class.
  - A `<table>` that is a *direct* child of a `flex flex-col` container
    inherits `align-items: stretch` and gets forced to the container's full
    cross-axis width even with no `w-full` class anywhere — this used to
    undo #17's "compact, sized to its own content" sizing for CPU/MEM/BAT's
    `<table class="sys-table">` specifically (Storage/Network's tables sat
    one level deeper inside a plain wrapper `<div>`, so the div stretched
    instead of the table). `.sys-grid{width:fit-content}` (#57) carries the
    same fix forward for the CSS Grid that replaced the table — the
    principle applies to any block-level container that's a flex item.
- **`animation-fill-mode: both` + a stagger delay is a blank-wallpaper
  hazard.** It parks each panel at `opacity:0` until the animation runs, so
  anything throttling or disabling animation leaves the desktop empty forever.
  `apply()` always strips the class back off after 1.6s. Visibility must never
  depend on an animation completing.
- **`preserveAspectRatio="none"` distorts SVG text and markers**, not just
  the path — labels stretch and dots become ellipses. The weather hourly
  sparkline that used to warrant this note is gone (see #8, `renderHourly`
  replaced it with a plain list), but `app.js`'s `miniSpark()` deliberately
  uses `preserveAspectRatio="none"` anyway: it draws a bare path with no
  text/marker children, so there's nothing for the distortion to hit, and
  stretching it to exactly fill a fixed small box is the point for a
  compact inline trend line. Any *new* SVG with text or markers should
  default to measuring its real pixel box and setting `viewBox` from that
  instead (1 unit == 1 px), same as the removed sparkline did, re-triggered
  from the `resize` listener in `apply()`.
