/* Desktop dashboard renderer.
 *
 * No standalone date/clock/location readout -- the user's own system
 * already shows all three (see #5, #9). The Calendar component
 * (#22) is not that: a month grid with today highlighted is context the
 * taskbar doesn't give, not a redundant single-line date.
 * Everything redraws from /api/state on a poll. Render is a pure function
 * of the last good state, so a failed poll just keeps the previous frame
 * up and raises the offline banner instead of blanking the wallpaper.
 *
 *  #34: the CPU/Memory trend graphs (and the /api/history poll
 * that fed them) were removed from this file -- not wanted on the
 * wallpaper. Collection is untouched (dashd-collect still writes
 * data/metrics.db every config.metrics_sample_seconds, and dashd-serve
 * still exposes /api/history) in case a future view wants them back.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
const el = (n, a = {}) => Object.entries(a).reduce(
  (e, [k, v]) => (e.setAttribute(k, v), e), document.createElementNS(NS, n));

const params = new URLSearchParams(location.search);
const OUTPUT = params.get('output') || 'default';
// ?static=1 suppresses entrance animation -- used by headless snapshot renders,
// where virtual time never advances a CSS animation to completion.
const STATIC = params.get('static') === '1';
// ?forcetime=HH:MM (#92): a real debug parameter, not the throwaway shim
// #90's implementer built and reverted -- pins the clock sunTint() reads
// so a time-lapse can be rendered deterministically instead of whatever
// wall-clock time check.sh/a manual render happens to run at. Gated
// behind STATIC so it can never reach the live wallpaper: dashd-host never
// passes ?static=1, so FORCE_TIME is always null there regardless of what
// a URL might claim. Documented in .claude/rules/verification.md.
const FORCE_TIME = STATIC ? params.get('forcetime') : null;
function tintNow() {
  if (FORCE_TIME && /^([01]\d|2[0-3]):[0-5]\d$/.test(FORCE_TIME)) {
    const [hh, mm] = FORCE_TIME.split(':').map(Number);
    const d = new Date();
    d.setHours(hh, mm, 0, 0);
    return d;
  }
  return new Date();
}

let state = null;
let failures = 0;

// A window's worth of *this browser session's* /api/state polls (default
// every 5s). Backs the Network chart, which wants finer resolution than
// the server-side history DB offers (throughput bursts are brief; 30s-
// resolution DB samples would flatten them). Capped by wall-clock age, not
// point count, so it self-adjusts to whatever the configured poll interval
// actually is. Disk I/O (#82) rides the same ring for the same reason --
// it separately persists to metrics.db (lib/metrics.py's COLUMNS) for a
// possible future longer-tier view, but the live chart reads straight off
// this session's own polls, same as Network's.
const RING_WINDOW_MS = 5 * 60 * 1000;
let ring = { t: [], down: [], up: [], diskRead: [], diskWrite: [] };

// Surface tint (#78; made the opaque background itself, #89; recolored to
// actually read as time-of-day, #90): a full-viewport slate gradient
// painted by the surface -- this IS the background, at alpha 1, not a
// translucent layer riding cosmic-bg's real wallpaper. Four phases, each a
// real, recognisable colour rather than a near-neutral variant: sunrise
// (warm orange/rose over a still-dark sky), noon (open daylight blue),
// sunset (deeper and redder than sunrise -- the two must never be
// interchangeable), midnight (a genuinely dark blue-violet, not a purple
// wash). #78/#88's "subset of slate gray, barely present" bar governed
// this palette for three rounds and was explicitly withdrawn by #90: the
// user's complaint was that midnight "doesn't really feel like midnight",
// i.e. the old near-neutral spread was the bug, not a target to protect.
//
// TINT_NEUTRAL is still the flat, unshifted slate (Off) -- unchanged from
// #89, since nobody has complained about the Off state and it still needs
// to look like a deliberate flat background, not a broken one.
// TINT_PHASES holds each phase's Medium-strength target color; strength
// (#88, re-scoped to colour departure by #89) blends from TINT_NEUTRAL
// toward that target -- 0 (Off) is the flat neutral with no time shift at
// all, 1 (Medium) is the tuned target, 1.6 (Strong, the user's own live
// pick) extrapolates further past it for a bolder cast. Panel content
// stays readable at every phase regardless of how far this departs from
// neutral: `.panel`'s background is 88% opaque (`--color-panel`), so only
// ~12% of any tint value ever bleeds into a panel's own fill. Measured
// (WCAG contrast ratio, panel fill blended with each phase's average
// top/bottom tint, at Strength 1.6 -- the widest spread shipped, worst
// case for all four phases): --color-ink >= 14.88:1, --color-muted >=
// 6.29:1, --color-faint >= 3.05:1, the accent triad (cyan/warm/crimson)
// all >= 5.02:1 -- every figure within a point of the Off baseline
// (ink 16.25:1, muted 6.87:1, faint 3.33:1). No raw text ever sits
// directly on #suntint (the boot screen and offline banner both have
// their own opaque layers above it), so the panel's own opacity is what
// keeps content readable, not restraint on how saturated the background
// is allowed to be -- the bar this issue sets.
//
// Phase centers follow the real sun rather than the clock: sunrise/sunset
// come straight from weather.sun; afternoon (noon) centers on solar noon
// (their midpoint) and midnight on the midpoint of the night span, so all
// four drift with the season the same way, not just the two that are
// given directly -- #90 keeps this exactly as #78 built it, unchanged.
// Theme-independent (see DESIGN.md's Surface Tint entry): this is a
// daylight cue on the background layer, not HUD chrome, so it renders
// identically under night_ops/retro_terminal/retro_orange (the CRT
// themes' own opaque body backgrounds sit underneath and are now fully
// covered -- see DESIGN.md).
//
// #92: the gradient's GEOMETRY moves now too, not just its palette --
// "like a skybox", per the issue. TINT_PHASES stays exactly what #90
// tuned (this issue moves the four hues, it does not re-pick them); what's
// new is solarAltitude()/tintIntensity()/tintGlowPos() below, which turn
// the phase colour into a moving three-stop gradient: sky (darkens toward
// the base near sunrise/sunset -- "a still-dark sky", the issue's own
// words), a warm glow stop whose POSITION climbs from near the bottom
// edge (the horizon) at sunrise/sunset toward the top at solar noon and
// back down into sunset, and the unchanged deep base. See sunTint()'s own
// comment for the exact shape.
const TINT_NEUTRAL = { top: [34, 36, 44], bottom: [20, 21, 27] };
const TINT_PHASES = {
  sunrise:   { top: [104, 62, 48], bottom: [32, 21, 20] },
  afternoon: { top: [60, 100, 155], bottom: [36, 55, 85] },
  sunset:    { top: [135, 45, 38], bottom: [58, 22, 20] },
  midnight:  { top: [24, 18, 46], bottom: [7, 6, 16] },
};

// Named strength levels for the context menu's Tint submenu (#88). Since
// #89 this is a colour-departure multiplier, not an alpha multiplier --
// Off is exactly 0 (TINT_NEUTRAL, no time shift, but always an opaque
// surface -- never transparent). Medium (1) reproduces TINT_PHASES above
// as tuned; Subtle/Strong scale the departure down/up. display.tint_strength
// stores the raw number (persisted via #74's selection mechanism, same as
// display.theme), so an old or hand-edited config with an arbitrary value
// still works -- the menu just won't show any entry checked. The values
// are unchanged from #88 -- the user's live selection (1.6, Strong) still
// resolves to Strong under the new meaning.
const TINT_STRENGTHS = { off: 0, subtle: 0.6, medium: 1, strong: 1.6 };
const clamp255 = (v) => Math.max(0, Math.min(255, v));

function minutesOf(d) { return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60; }

function tintAnchors(sun) {
  // Graceful fallback (no weather yet, or a stale cache missing sun data)
  // -- a plausible fixed day so the tint still cycles instead of freezing
  // on one phase; degrades the same way sysinfo/ghstats degrade elsewhere.
  const rise = sun && sun.sunrise ? minutesOf(new Date(sun.sunrise)) : 390;
  const set = sun && sun.sunset ? minutesOf(new Date(sun.sunset)) : 1110;
  return {
    sunrise: rise,
    afternoon: (rise + set) / 2,
    sunset: set,
    midnight: ((set + rise + 1440) / 2) % 1440,
  };
}

function lerp(a, b, t) { return a + (b - a) * t; }
function lerpTriple(a, b, t) { return a.map((v, i) => lerp(v, b[i], t)); }
function rgbaStr(rgb, a) {
  return `rgba(${Math.round(rgb[0])}, ${Math.round(rgb[1])}, ${Math.round(rgb[2])}, ${a.toFixed(3)})`;
}

// Continuous solar altitude (#92): 0 at sunrise/sunset, +1 at solar noon,
// -1 at true midnight (the midpoint of the night span, same anchor
// tintAnchors() already computes) -- NOT a real solar-elevation formula,
// the issue says one is welcome but not required, just a smooth two-piece
// sine built on the same real weather.sun times #78 already anchors the
// four phases to, so the seasonal drift #78 built keeps working unchanged.
// The day and night pieces are both exactly 0 at their shared boundary
// (sunrise/sunset), so the curve is continuous everywhere -- there's a
// slope change there (day and night spans are rarely equal length), not a
// jump, which is why a time-lapse never shows a step.
function solarAltitude(sun, now) {
  const { sunrise: rise, sunset: set } = tintAnchors(sun);
  const nowMin = minutesOf(now);
  if (nowMin >= rise && nowMin < set) {
    return Math.sin(Math.PI * ((nowMin - rise) / (set - rise)));
  }
  const nightSpan = rise + 1440 - set;
  return -Math.sin(Math.PI * (((nowMin - set + 1440) % 1440) / nightSpan));
}

// Intensity (#92, part 3 of the issue): strongest near the horizon at dawn
// and dusk (altitude ~0), receding at noon AND at true night -- "recedes"
// rather than "vanishes" during the day, per the issue's own "washes the
// whole field at noon" -- a hard floor at altitude>=0 keeps a faint wash
// visible at noon instead of the glow stop disappearing into the plain
// gradient for the middle third of the day; night gets no such floor, so
// it genuinely reaches 0 approaching true midnight ("gone at night").
function tintIntensity(altitude) {
  return altitude >= 0
    ? Math.max(0.15, 1 - altitude)
    : Math.max(0, 1 - Math.abs(altitude));
}

// Glow stop position, 0 (top) - 100 (bottom) (#92): low near the bottom
// edge at sunrise/sunset (altitude 0 -> 82, close to the horizon but short
// of the 100 the deep base itself owns), climbing toward the top through
// the morning (altitude 1 -> 15), and symmetrically back down into sunset.
// Past sunset the same line keeps going toward the bottom (altitude -1 ->
// 96) for continuity, though it's moot there since tintIntensity() has
// already floored the glow's amount to 0 by then.
function tintGlowPos(altitude) {
  return altitude >= 0 ? 82 - 67 * altitude : 82 - 14 * altitude;
}

// Returns { top, glow, glowPos, bottom } -- top/glow/bottom are rgba()
// strings (always alpha 1 -- this paints the opaque background itself,
// #89), glowPos a percentage string for the moving middle stop. Phase
// colour blending (a/b/t below) is the unchanged #78/#89/#90 continuous
// lerp between the two nearest phase centers -- no discrete step anywhere,
// which is what keeps a phase change from ever reading as the sudden cut
// #78 explicitly calls out. `strength` (#88, colour-departure semantics
// since #89) is applied once to get `top`/`bottom`, same as before k=0
// collapses to flat neutral (Off), k=1 lands on the tuned phase blend
// (Medium), k=1.6 (Strong) extrapolates a little past it.
//
// #92's new geometry sits on top of that unchanged palette:
// - `sky` (the 0% stop) darkens from `top` toward `bottom` as altitude
//   approaches 0 -- "a still-dark sky" under the sunrise/sunset glow, the
//   issue's own phrase -- and holds exactly `top` at noon/midnight
//   (altitude +-1), matching #90's accepted hue there untouched. The
//   darken amount is also gated by min(strength,1), so Off (k=0) collapses
//   `sky` to exactly `top` at every altitude -- no motion at all, which is
//   what "Off stays flat neutral" requires.
// - `glow` (the moving middle stop) lerps from the plain two-stop colour
//   the gradient would have at that position anyway (`natural`) toward
//   `top` (the undarkened phase colour), by intensity * min(strength,1).
//   At Off or at night this collapses to exactly `natural`, so the third
//   stop is invisible -- indistinguishable from the old two-stop gradient
//   -- rather than a moving knee in a flat background.
// - `bottom` (the 100% stop, the deep base) is untouched by any of this.
function sunTint(sun, now, strength) {
  const anchors = tintAnchors(sun);
  const order = Object.keys(anchors)
    .map((key) => ({ key, at: anchors[key] }))
    .sort((a, b) => a.at - b.at);
  const nowMin = minutesOf(now);
  let i = order.findIndex((o) => o.at > nowMin);
  if (i === -1) i = 0; // past the last anchor of the day -> wrap to the first
  const b = order[i];
  const a = order[(i - 1 + order.length) % order.length];
  const span = ((b.at - a.at + 1440) % 1440) || 1440;
  const elapsed = (nowMin - a.at + 1440) % 1440;
  const t = elapsed / span;
  const pa = TINT_PHASES[a.key], pb = TINT_PHASES[b.key];
  const k = strength ?? TINT_STRENGTHS.medium;
  const blendTop = lerpTriple(pa.top, pb.top, t);
  const blendBottom = lerpTriple(pa.bottom, pb.bottom, t);
  const top = lerpTriple(TINT_NEUTRAL.top, blendTop, k).map(clamp255);
  const bottom = lerpTriple(TINT_NEUTRAL.bottom, blendBottom, k).map(clamp255);

  const altitude = solarAltitude(sun, now);
  const kAmt = Math.min(k, 1); // clamp even Strong's 1.6 to 1 for the new
                                // geometry -- see the comment above.
  const darken = Math.max(0, Math.min(0.85, 0.5 * (1 - Math.abs(altitude)) * kAmt));
  const sky = lerpTriple(top, bottom, darken).map(clamp255);
  const pos = tintGlowPos(altitude);
  const natural = lerpTriple(sky, bottom, pos / 100);
  const glowAmt = tintIntensity(altitude) * kAmt;
  const glow = lerpTriple(natural, top, glowAmt).map(clamp255);

  return {
    top: rgbaStr(sky, 1),
    glow: rgbaStr(glow, 1),
    glowPos: `${pos.toFixed(1)}%`,
    bottom: rgbaStr(bottom, 1),
  };
}
function pushRing(s) {
  const now = Date.now();
  ring.t.push(now); ring.down.push(s.net.down); ring.up.push(s.net.up);
  ring.diskRead.push(s.disk_io.read); ring.diskWrite.push(s.disk_io.write);
  const cutoff = now - RING_WINDOW_MS;
  while (ring.t.length && ring.t[0] < cutoff) {
    ring.t.shift(); ring.down.shift(); ring.up.shift();
    ring.diskRead.shift(); ring.diskWrite.shift();
  }
}
function ringSince(key, ms) {
  const cutoff = Date.now() - ms;
  const i = ring.t.findIndex((t) => t >= cutoff);
  return i === -1 ? [] : ring[key].slice(i);
}

/* ----------------------------------------------------------------- format */
const bytes = (b) => {
  const u = ['B', 'K', 'M', 'G', 'T']; let i = 0; b = b || 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b < 10 && i > 0 ? b.toFixed(1) : Math.round(b)}${u[i]}`;
};
const dur = (s) => {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
};
// Zero-padded HH:MM:SS, not dur()'s variable-width "1h 2m" -- Music's
// elapsed/total readout needs a fixed digit count so it doesn't visibly
// jump around every second (paired with .num's tabular-nums).
const clockTime = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return [h, m, sec].map((n) => String(n).padStart(2, '0')).join(':');
};
const hhmm = (iso) => (iso || '').slice(11, 16);
const POINTS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
  'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
const compass = (deg) => POINTS[Math.round(deg / 22.5) % 16];

/* ------------------------------------------------------------ sys panel */
/* Hand-rolled icons, zero external asset dependency. stroke/fill are set
 * once on the root <svg> and inherit down through SVG's normal property
 * cascade. sunrise/sunset (Sun & Moon's timeChip()) are the only live
 * consumer now -- System (#57), Music, and Weather's per-stat rows (#41)
 * have all dropped icons entirely; their cases were removed here rather
 * than left as dead code. */
function sysIcon(kind, tone = 'currentColor') {
  const g = el('svg', { viewBox: '0 0 24 24', class: 'sysicon',
    fill: 'none', stroke: tone, 'stroke-width': 1.6,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round' });
  switch (kind) {
    case 'sunrise':
      g.appendChild(el('line', { x1: 2, y1: 20, x2: 22, y2: 20 }));
      g.appendChild(el('line', { x1: 12, y1: 8, x2: 12, y2: 16 }));
      g.appendChild(el('polyline', { points: '8,13 12,8 16,13' }));
      break;
    case 'sunset':
      g.appendChild(el('line', { x1: 2, y1: 20, x2: 22, y2: 20 }));
      g.appendChild(el('line', { x1: 12, y1: 6, x2: 12, y2: 16 }));
      g.appendChild(el('polyline', { points: '8,11 12,16 16,11' }));
      break;
    default: break;
  }
  return g;
}


// Three-stop hex interpolation used to color each segment of utilBars() --
// a spectrum across the bar's full length, revealed proportionally by how
// many segments are filled, the same effect as the reference racing-stat
// graphic (#31). Stops come from CSS custom properties (spectrumStops()),
// not hardcoded, so a theme can override them (#39).
function lerpHex(a, b, t) {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  const c = pa.map((v, i) => Math.round(v + (pb[i] - v) * t));
  return `#${c.map((v) => v.toString(16).padStart(2, '0')).join('')}`;
}
// Read once per utilBars() call, not per segment -- getComputedStyle is
// the theme-aware source (#39), a custom property's raw author value.
function spectrumStops() {
  const cs = getComputedStyle(document.documentElement);
  return {
    lo: cs.getPropertyValue('--spectrum-lo').trim(),
    mid: cs.getPropertyValue('--spectrum-mid').trim(),
    hi: cs.getPropertyValue('--spectrum-hi').trim(),
  };
}
function spectrumColor(t, stops) {
  return t < 0.5 ? lerpHex(stops.lo, stops.mid, t / 0.5)
    : lerpHex(stops.mid, stops.hi, (t - 0.5) / 0.5);
}

/* Angled utilization bar (#31, replacing the status LED -- user
 * feedback: "not working out"). A row of slanted parallelogram segments,
 * same idea as a car-stat readout: segments up to the filled count light up
 * in a theme-driven spectrum (red -> gold -> green by default, #39) across
 * the bar's position (not tied to the metric's own percentage bands), the
 * rest sit dim/unlit as the empty track. Battery has no percentage bands
 * to speak of either -- same spectrum, just fewer segments lit at low
 * charge. */
function utilBars(pct, segments = 10) {
  const clamped = Math.max(0, Math.min(100, pct ?? 0));
  const filled = Math.round((clamped / 100) * segments);
  const svg = el('svg', { viewBox: '0 0 100 20', preserveAspectRatio: 'none',
    class: 'util-bars w-[clamp(3.2rem,8vmin,5.6rem)] h-[clamp(0.7rem,1.6vmin,0.95rem)] shrink-0 ml-auto' });
  const w = 5, gap = 2.2, skew = 2.6, h = 16, top = 2, left = 3;
  const stops = spectrumStops();
  for (let i = 0; i < segments; i++) {
    const x = left + i * (w + gap);
    const points = [
      [x + skew, top], [x + skew + w, top], [x + w, top + h], [x, top + h],
    ].map((p) => p.join(',')).join(' ');
    const lit = i < filled;
    svg.appendChild(el('polygon', {
      points,
      fill: lit ? spectrumColor(i / (segments - 1), stops) : 'oklch(0.32 0.02 260 / .45)',
      opacity: lit ? '0.95' : '1',
    }));
  }
  return svg;
}

/* Step line chart -- compact enough to sit on a single line next to text
 * (a tier label, in sysTierRow() below). "Step" rather than a diagonal
 * line: each sample holds its value until the next one arrives (a
 * staircase), which reads truer for a polled metric than interpolating
 * between two readings that were never actually in between. Accepts one
 * or more {values, tone, fill} series sharing one x/y scale, so the
 * Network chart can plot download and upload as two lines without them
 * fighting over independent scales. `fill: true` additionally fills the
 * area under the step line with a gradient fading to transparent -- more
 * visual presence at wallpaper viewing distance than a bare stroke, without
 * adding any more information than the line already carries. Deliberately
 * preserveAspectRatio="none": these are bare paths with no text/marker
 * children to distort (see CLAUDE.md's gotcha on that attribute), so
 * stretching to exactly fill a fixed small box is exactly what's wanted
 * here.
 *
 * `domain` ([lo, hi]) overrides the scale this chart would derive from its
 * own series. Two charts drawn side by side are two SVGs and so cannot share
 * a scale implicitly the way two series in one box do -- Network (#59) passes
 * the same domain to both so its down and up boxes stay directly comparable,
 * which is the property the single dual-line box had by construction. */
let gradientSeq = 0;
function stepChart(series, cls, domain = null) {
  const svg = el('svg', { viewBox: '0 0 100 30', preserveAspectRatio: 'none',
    class: cls || 'w-[clamp(3.4rem,9vmin,6.4rem)] h-[clamp(0.8rem,1.8vmin,1.05rem)] shrink-0 ml-auto' });
  const all = series.flatMap((sr) => sr.values);
  if (all.length < 2) return svg;
  const [lo, hi] = domain || [Math.min(...all), Math.max(...all)];
  const span = Math.max(hi - lo, 1);
  const pad = 2;
  const baseline = 30;
  const y = (v) => (30 - pad) - ((v - lo) / span) * (30 - pad * 2);
  series.forEach(({ values, tone, fill }) => {
    if (values.length < 2) return;
    const x = (i) => (i / (values.length - 1)) * 100;
    let d = `M ${x(0).toFixed(1)} ${y(values[0]).toFixed(1)}`;
    for (let i = 1; i < values.length; i++) {
      // hold the previous value flat to this sample's x, then step to it
      d += ` L ${x(i).toFixed(1)} ${y(values[i - 1]).toFixed(1)}`;
      d += ` L ${x(i).toFixed(1)} ${y(values[i]).toFixed(1)}`;
    }
    if (fill) {
      // Gradient id is page-unique (not per-metric) since the whole box
      // is torn down and rebuilt every render -- a counter avoids ever
      // reusing an id an old, still-fading-out node might reference.
      const id = `graph-fill-${gradientSeq++}`;
      const defs = el('defs');
      const grad = el('linearGradient', { id, x1: '0', y1: '0', x2: '0', y2: '1' });
      grad.appendChild(el('stop', { offset: '0%', 'stop-color': tone, 'stop-opacity': '0.35' }));
      grad.appendChild(el('stop', { offset: '100%', 'stop-color': tone, 'stop-opacity': '0' }));
      defs.appendChild(grad);
      svg.appendChild(defs);
      const last = x(values.length - 1).toFixed(1);
      const areaD = `${d} L ${last} ${baseline} L ${x(0).toFixed(1)} ${baseline} Z`;
      svg.appendChild(el('path', { d: areaD, fill: `url(#${id})`, stroke: 'none' }));
    }
    svg.appendChild(el('path', { d, fill: 'none', stroke: tone,
      'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      'vector-effect': 'non-scaling-stroke', opacity: '.85' }));
  });
  return svg;
}

/* Fill bar, e.g. a disk row's small fixed-width swatch (default, sized to
 * match utilBars()'s CPU/MEM/BAT scale -- #42) or Music's full-width
 * progress bar (widthClass/heightClass override, user request: "make the
 * progress bar span the entire component"). ml-auto is part of the
 * default only -- it pushes a narrow bar to the right edge of a table's
 * tail column; a w-full bar has no room for that to matter. radiusClass
 * defaults to the pill shape every other caller (disk rows) still wants;
 * Music passes 'rounded-none' (user request: "update the progress bar to
 * be square") without affecting those other callers. */
function miniBar(pct, tone = 'var(--color-accent)',
    widthClass = 'w-[clamp(3.2rem,8vmin,5.6rem)] ml-auto',
    radiusClass = 'rounded-full',
    heightClass = 'h-[clamp(0.7rem,1.6vmin,0.95rem)] min-h-[3px]') {
  const wrap = document.createElement('div');
  wrap.className = `${widthClass} ${radiusClass} ${heightClass} overflow-hidden`;
  wrap.style.background = 'oklch(0.5 0.02 260/.22)';
  const fill = document.createElement('div');
  // transform:scaleX, not width -- animating width/height/padding/margin
  // triggers layout on every frame; scaleX is compositor-only.
  fill.style.cssText = `background:${tone};width:100%;height:100%;` +
    `border-radius:inherit;transform-origin:left;` +
    `transform:scaleX(${Math.min(100, pct ?? 0) / 100});transition:transform .6s ease`;
  wrap.appendChild(fill);
  return wrap;
}

// One .sys-grid row: label | value (+ status bar/chart), with an optional
// sub line directly underneath the value (#57 standardized System onto
// Weather's .kv-grid conventions -- no icons, one font size everywhere;
// #58 moved the dash-prefixed sub info back under the row instead of
// beside it as a third column). `hot` swaps the value to the same
// alert-badge treatment Weather uses (#49) instead of the retired
// icon-tone signal; the bar/chart itself is unrelated and keeps its own
// independent coloring (DESIGN.md's Utilization Bars component is a
// fixed position-based spectrum, not a threshold). `sub` is the old
// second-line info (CPU temp/freq, MEM used/total, BAT time-remaining,
// NET upload rate) -- omitted entirely (not an empty row) when there's
// nothing to show, e.g. Storage.
function sysRow({ label, value = '', hot = false, bar = null, sub = '' }) {
  const key = document.createElement('div');
  key.className = 'sys-key text-faint tracking-[0.1em] uppercase truncate';
  key.textContent = label;

  const val = document.createElement('div');
  val.className = 'sys-val num';
  const valText = document.createElement('span');
  valText.className = hot ? 'alert-badge' : 'text-ink';
  valText.textContent = value;
  val.appendChild(valText);
  if (bar) val.appendChild(bar);

  if (!sub) return [key, val];

  const subEl = document.createElement('div');
  subEl.className = 'sys-sub num text-faint';
  subEl.textContent = `- ${sub}`;

  return [key, val, subEl];
}

// CPU/Memory/Battery value rows. Trend graphs used to render here (#17,
// #33) but were removed (#34) -- not wanted on the wallpaper. Metrics
// collection is untouched (dashd-collect, metrics.db, /api/history all still
// live) for possible later use. Each row's status bar is the angled
// Utilization Bars graphic (#31, replacing the retired status LED).
function renderCpuMemBat(s) {
  const box = $('sys');
  box.replaceChildren();
  const isHot = (p) => p > 88;
  const pad2 = (n) => String(Math.round(n)).padStart(2, '0');

  box.append(...sysRow({
    label: 'CPU', value: `${pad2(s.cpu)}%`, hot: isHot(s.cpu),
    bar: utilBars(s.cpu),
    sub: [s.temp_c ? `${s.temp_c}°C` : null,
      s.cpu_freq ? `${(s.cpu_freq / 1000).toFixed(1)}GHz` : null].filter(Boolean).join(' · '),
  }));

  box.append(...sysRow({
    label: 'MEM', value: `${pad2(s.mem.pct)}%`, hot: isHot(s.mem.pct),
    bar: utilBars(s.mem.pct),
    sub: `${bytes(s.mem.used)} / ${bytes(s.mem.total)}`,
  }));

  if (s.battery) {
    const battHot = s.battery.pct <= 35 && !s.battery.plugged;
    box.append(...sysRow({
      label: 'BAT', value: `${pad2(s.battery.pct)}%`, hot: battHot,
      bar: utilBars(s.battery.pct),
      sub: !s.battery.plugged && s.battery.secs_left ? dur(s.battery.secs_left) : '',
    }));
  }

  // LOAD (#81): sys.load/sys.uptime were already in /api/state and never
  // rendered anywhere but the plain sysfoot text below. Promoted to a real
  // .sys-grid row so it gets the same hot-threshold/bar treatment as
  // CPU/MEM/BAT -- "hot" here is 1-minute load past the core count (the
  // only meaningful threshold for a load average, since 1.0 means
  // "fully busy" on a single core but is nothing on a 16-core box), not a
  // fixed percentage. Uptime rides along as this row's sub line per the
  // issue's own suggestion, rather than spending a row of its own on it.
  const cores = s.cpu_cores.length;
  const load1 = s.load[0];
  box.append(...sysRow({
    label: 'LOAD', value: s.load.map((n) => n.toFixed(2)).join(' '),
    hot: load1 > cores,
    bar: utilBars(Math.min(100, (load1 / cores) * 100)),
    sub: `up ${dur(s.uptime)}`,
  }));

  // sys.procs/sys.host are left off the row grid entirely (#81 -- "probably
  // not worth surface space at wallpaper distance", and neither one is
  // actionable the way a hot load average or a low battery is): process
  // count is noise without a baseline, and hostname doesn't change at
  // runtime, so a static footer line is already exactly right for both.
  $('sysfoot').innerHTML =
    `<div class="flex justify-between gap-[1vmin]">
       <span>${s.procs} procs</span>
       <span>${s.host}</span>
     </div>`;
}

// Pending apt updates (#83): one row, same .sys-grid shape as everything
// else here. The total on its own is chrome -- what earns the alert-badge
// treatment is any pending *security* update, same hot-threshold mechanism
// CPU/MEM/Storage/LOAD already use, called out in the sub line rather than
// a second color (this system commits to exactly one alert color, red,
// see DESIGN.md's Status-Only Glow / One Accent Per Region rules). Omitted
// entirely -- not a zeroed or error row -- when the cache hasn't populated
// yet or the tooling isn't available, same degrade-gracefully posture as
// Projects/Sessions/Music.
function renderUpdates(updates) {
  const box = $('sys');
  if (!updates || !updates.available) return;
  box.append(...sysRow({
    label: 'UPDATES',
    value: `${updates.total}`,
    hot: updates.security > 0,
    sub: updates.security > 0 ? `${updates.security} security` : '',
  }));
}

// Storage: its own dedicated area (#12), not mixed into the
// cpu/mem/bat rows above. Every partition lsblk reports a real utilization
// for, not just psutil's mounted view -- see dashd-serve's disk_tree().
// sys.swap (#81) sits with Storage, not MEM -- it's disk, not RAM -- and
// renders here rather than via disk_tree()/lsblk: a bare swap partition
// carries no usable FSUSE%, since its real usage lives on a crypt child
// disk_tree() deliberately doesn't walk into for a utilization figure (see
// lib/sysinfo.py). psutil.swap_memory() is the number to show instead.
// Skipped entirely when there's no swap configured (total 0) rather than
// rendering a dead 0% row.
function renderDisks(disks, swap) {
  const box = $('disks');
  box.replaceChildren();
  const isHot = (p) => p > 88;
  const barTone = (p) => isHot(p) ? 'var(--color-warm)' : 'var(--color-accent)';

  if (swap && swap.total > 0) {
    box.append(...sysRow({
      label: 'SWAP', value: `${swap.pct.toFixed(0)}%`, hot: isHot(swap.pct),
      bar: miniBar(swap.pct, barTone(swap.pct), undefined, 'rounded-none'),
      sub: `${bytes(swap.used)} / ${bytes(swap.total)}`,
    }));
  }

  disks.forEach((d) => {
    const label = d.mount && d.mount !== '[SWAP]'
      ? (d.mount.split('/').filter(Boolean).pop() || '/') : d.name;
    box.append(...sysRow({
      label: label.toUpperCase(),
      value: d.pct != null ? `${d.pct.toFixed(0)}%` : bytes(d.size),
      hot: isHot(d.pct ?? 0),
      // Square corners (user request); width/height use miniBar()'s
      // default scale, unified with CPU/MEM/BAT's utilBars() (#42).
      bar: miniBar(d.pct, barTone(d.pct ?? 0), undefined, 'rounded-none'),
    }));
  });
}

// One captioned chart in a throughput graph row (Network's DOWN/UP, and
// since #82 Storage's READ/WRITE): caption above, chart, then the actual
// rate directly underneath -- #60 moved Network's down/up values here from
// the NET metric row above, since a caption with no number next to its
// chart forced a look back up at a disconnected row to read the rate.
// The caption itself keeps the same faint uppercase treatment as a
// .sys-key label rather than being tinted to match its line -- position
// already identifies it, and colored text would spend an accent on a
// label (DESIGN.md's one-accent-per-region rule) to say what position
// already says. The rate below is text-ink, same brightness as every
// other value in the system (#55/#57).
function throughputGraph(label, value, values, tone, domain) {
  const cell = document.createElement('div');
  cell.className = 'flex flex-col gap-[0.25vmin]';

  const cap = document.createElement('div');
  cap.className = 'sys-key text-faint tracking-[0.1em] uppercase';
  cap.textContent = label;

  // Explicit width, not w-full: .sys-grid is width:fit-content, so a
  // percentage width would resolve against whatever the text rows happen to
  // measure. Sizing the charts and letting the grid widen to them keeps
  // "every metric grid is content-sized" true (#17) with the graph row in it.
  const chart = stepChart([{ values, tone }],
    'w-[clamp(3.4rem,9vmin,6.4rem)] h-[clamp(1.6rem,3.6vmin,2.4rem)] block', domain);

  const valEl = document.createElement('div');
  valEl.className = 'num text-ink';
  valEl.textContent = value;

  cell.append(cap, chart, valEl);
  return cell;
}

// Network: dedicated area below Storage. The NET label row keeps the
// standard .sys-grid shape (#57) so its columns still line up with
// CPU/MEM/BAT and Storage, but carries no value/sub of its own (#60) --
// down/up now render as captions under their own chart instead, so
// showing them again here would just duplicate the same two numbers with
// no chart next to them. The chart itself sits in a full-width row
// beneath the label row (#59), split into separate download and upload
// boxes side by side instead of two lines sharing one box.
//
// Both boxes are drawn on one explicit domain spanning both series. Two SVGs
// cannot share a scale the way two series in one box did, and letting each
// self-scale would make a 40K/s upload draw the same shape as a 40M/s
// download -- the comparison the shared scale existed to protect.
//
// No long-term storage for this one (see #12) -- it's the ring buffer's fine
// 5s resolution that makes brief throughput bursts visible at all, so a
// DB-backed tier would just flatten them. No hot/alert threshold defined for
// throughput, so the value never gets the badge treatment.
function renderNetwork(s) {
  const box = $('net');
  box.replaceChildren();

  const down = ringSince('down', RING_WINDOW_MS);
  const up = ringSince('up', RING_WINDOW_MS);
  const all = down.concat(up);
  // Under two samples every series is a single point and stepChart draws
  // nothing -- fall back to the plain label/value/sub row (same shape
  // every other System row uses) so the rate is still visible before the
  // ring buffer has enough history for a chart, rather than showing
  // nothing at all.
  if (all.length < 2) {
    box.append(...sysRow({
      label: 'NET',
      value: `down ${bytes(s.net.down)}/s`,
      sub: `up ${bytes(s.net.up)}/s`,
    }));
    return;
  }
  box.append(...sysRow({ label: 'NET' }));

  const domain = [Math.min(...all), Math.max(...all)];
  const graphs = document.createElement('div');
  graphs.className = 'sys-graph flex gap-[0.8vmin]';
  graphs.append(
    // #61: no "down"/"up" word here -- the DOWN/UP caption above each
    // chart already says which is which, so repeating it in the value
    // would just be noise.
    throughputGraph('DOWN', `${bytes(s.net.down)}/s`, down, 'var(--color-accent)', domain),
    throughputGraph('UP', `${bytes(s.net.up)}/s`, up, 'var(--color-warm)', domain),
  );
  box.appendChild(graphs);
}

// Disk I/O (#82): Storage's missing sibling to Network's throughput chart --
// same shape, same reasoning, appended into the same #disks .sys-grid as
// the capacity rows above it (SWAP, then each partition) so READ/WRITE
// reads as one more row of that grid rather than a separate area. Unlike
// Network, disk_read/disk_write DO persist to metrics.db (lib/metrics.py's
// COLUMNS) for a possible future longer tier -- but the chart itself still
// reads off this session's own ring buffer, same fine-resolution reasoning
// as Network's (a brief write burst would flatten at 30s DB resolution).
function renderDiskIO(s) {
  const box = $('disks');

  const read = ringSince('diskRead', RING_WINDOW_MS);
  const write = ringSince('diskWrite', RING_WINDOW_MS);
  const all = read.concat(write);
  if (all.length < 2) {
    box.append(...sysRow({
      label: 'I/O',
      value: `read ${bytes(s.disk_io.read)}/s`,
      sub: `write ${bytes(s.disk_io.write)}/s`,
    }));
    return;
  }
  box.append(...sysRow({ label: 'I/O' }));

  const domain = [Math.min(...all), Math.max(...all)];
  const graphs = document.createElement('div');
  graphs.className = 'sys-graph flex gap-[0.8vmin]';
  graphs.append(
    throughputGraph('READ', `${bytes(s.disk_io.read)}/s`, read, 'var(--color-accent)', domain),
    throughputGraph('WRITE', `${bytes(s.disk_io.write)}/s`, write, 'var(--color-warm)', domain),
  );
  box.appendChild(graphs);
}

// Music: its own top-level component, pinned to the bottom-right corner of
// the viewport (position:fixed in index.html), source-agnostic -- whatever
// dashd-serve's now_playing() picked up from MPRIS, be it a local player
// or a browser tab (#26). Stays visible and sits idle when nothing is
// playing/paused, rather than disappearing (user report: "it should not
// disappear just because no media is playing, should just sit idle" --
// it previously collapsed the whole section, which read as the panel
// itself being broken/missing rather than "no track right now").
//
// Layout per user requests across a couple of rounds: the note icon and
// the PLAYING/PAUSED status label are both dropped from the render
// entirely (the commented-out `status` line below is kept, not deleted,
// in case it's wanted back). Cover art (musicArt, a static <img> in
// index.html) sits left of the title/artist/progress stack (musicBody).
// Row one: title left, artist/album right. Row two: elapsed | progress
// bar | total, HH:MM:SS via clockTime() so the digits don't reflow every
// second. Title gets flex-1 + w-0 + min-w-0 (its absence used to let a
// long song name stretch the whole row -- "it scales with the name of
// the song" -- see the w-0 comment below) plus auto-cycle-x so a clamped
// title scrolls to reveal itself instead of just truncating silently.
function renderMusic(music) {
  const art = $('musicArt');
  const box = $('musicBody');
  box.replaceChildren();

  // Controls (#65) are disabled rather than hidden with no player -- the
  // section is meant to sit idle, not reflow. The toggle's label tracks the
  // live state (playing => show PAUS), since a fixed label can't say which
  // way the toggle is about to go; it falls back to PLAY when there's
  // nothing to describe.
  ['musicPrev', 'musicToggle', 'musicNext'].forEach((id) => {
    const btn = $(id);
    if (btn) btn.disabled = !music;
  });
  const toggle = $('musicToggle');
  if (toggle) {
    toggle.textContent = music && music.playing ? 'PAUS' : 'PLAY';
    toggle.setAttribute('aria-label', music && music.playing ? 'Pause' : 'Play');
  }

  if (!music) {
    art.classList.add('hidden');
    art.removeAttribute('src');
    delete art.dataset.artUrl;
    const idle = document.createElement('div');
    idle.className = 'text-faint text-[clamp(.5rem,1.05vmin,.76rem)]';
    idle.textContent = 'Nothing playing';
    box.appendChild(idle);
    return;
  }

  // const status = music.playing ? 'PLAYING' : 'PAUSED';

  // Native MPRIS art_url (playerctl), proxied/redirected by dashd-serve
  // (see bin/dashd-serve's music_art_response()) -- a file:// URL can't be
  // loaded directly from this http-origin page. Only touch .src when the
  // art actually changed, so a same-track poll doesn't reload/flicker it;
  // onerror hides a stale or unreadable art_url instead of showing a
  // broken-image icon.
  if (music.art_url) {
    if (art.dataset.artUrl !== music.art_url) {
      art.onerror = () => art.classList.add('hidden');
      art.src = `/api/music/art?t=${encodeURIComponent(music.art_url)}`;
      art.dataset.artUrl = music.art_url;
    }
    art.classList.remove('hidden');
  } else {
    art.classList.add('hidden');
    art.removeAttribute('src');
    delete art.dataset.artUrl;
  }

  const row = document.createElement('div');
  row.className = 'flex items-baseline gap-[0.8vmin]';
  const title = document.createElement('div');
  // w-0 (not just min-w-0) is load-bearing: this row's ancestors size
  // themselves shrink-to-fit (Music's panel width tracks the Calendar+
  // Weather row -- see index.html), and a flex item's *intrinsic*
  // contribution to that kind of auto-width ancestor is its max-content
  // size regardless of min-width:0/flex-basis:0%. Only an explicit
  // definite width (0) removes the content from that calculation, which
  // is what let a long title silently stretch the whole panel before --
  // "it scales with the name of the song".
  title.className = 'auto-cycle-x flex-1 w-0 min-w-0 text-ink tracking-wide ' +
    'text-[clamp(.52rem,1.1vmin,.8rem)]';
  title.textContent = music.title || '';
  const artist = document.createElement('div');
  artist.className = 'text-faint tracking-wide shrink-0 text-right ' +
    'text-[clamp(.46rem,.95vmin,.7rem)]';
  artist.textContent = [music.artist, music.album].filter(Boolean).join(' · ');
  row.append(title, artist);
  box.appendChild(row);

  if (music.length_secs) {
    const elapsed = music.position_secs ?? 0;
    const pct = Math.min(100, (elapsed / music.length_secs) * 100);

    const timeRow = document.createElement('div');
    timeRow.className = 'flex items-center gap-[0.6vmin]';
    const elapsedEl = document.createElement('span');
    elapsedEl.className = 'num text-faint shrink-0 text-[clamp(.42rem,.85vmin,.62rem)]';
    elapsedEl.textContent = clockTime(elapsed);
    const totalEl = document.createElement('span');
    totalEl.className = 'num text-faint shrink-0 text-[clamp(.42rem,.85vmin,.62rem)]';
    totalEl.textContent = clockTime(music.length_secs);
    const barWrap = document.createElement('div');
    barWrap.className = 'flex-1';
    barWrap.appendChild(miniBar(pct, 'var(--color-accent)', 'w-full', 'rounded-none',
      'h-[0.5vmin] min-h-[3px]'));
    timeRow.append(elapsedEl, barWrap, totalEl);
    box.appendChild(timeRow);
  }
}

// Log highlighter (#28): filtered/highlighted lines only (see
// lib/logsrc.py) -- not a full tail. Status -> accent color, matching the
// System panel's traffic-light language (critical=crimson, warn=amber,
// ok=online-green, anything else=faint).
const LOG_TONE = {
  critical: 'var(--color-crimson)',
  warn: 'var(--color-warm)',
  ok: 'var(--color-online)',
};
function renderLog(lines) {
  const box = $('logbox');
  box.replaceChildren();
  if (!lines || !lines.length) {
    const row = document.createElement('div');
    row.className = 'text-faint';
    row.textContent = 'no highlighted lines';
    box.appendChild(row);
    return;
  }
  lines.slice(-12).reverse().forEach((line) => {
    const tone = LOG_TONE[line.status] || 'var(--color-faint)';
    const row = document.createElement('div');
    row.style.color = tone;
    row.style.borderLeft = `2px solid ${tone}`;
    row.className = 'pl-[0.6vmin] truncate';
    row.title = line.text;
    row.textContent = `${line.label ? `[${line.label}] ` : ''}${line.text}`;
    box.appendChild(row);
  });
}

/* -------------------------------------------------------------- projects */
/* Per-repo GitHub issue stats (#62). The server sends the full sorted,
 * already-filtered row list -- this renders all of it into a fixed-height
 * box and lets the auto-cycle (#29) reveal anything past the eighth row,
 * rather than slicing to a "top N" here. That split matters: a JS slice
 * would make the visible count a magic number in two places, while the CSS
 * height makes it a single layout fact that adapts if the font scales.
 *
 * Color carries the meaning, so each count keeps its own hue even at zero
 * (dimmed via .is-zero) -- the columns have to stay readable as columns.
 */
// --color-actionable, NOT --color-online: every theme overrides the latter
// (retro_orange makes it amber), which rendered this column the same color
// as the panel chrome and flattened the triad. See index.html's :root.
const PROJ_COLS = [
  ['blocked', 'var(--color-crimson)', 'ai-blocked -- waiting on you'],
  ['actionable', 'var(--color-actionable)', 'actionable open issues'],
  ['closed', 'var(--color-done)', 'closed issues'],
];

function projNote(p) {
  if (p.disabled) return 'disabled in config';
  if (p.pending) return 'fetching…';
  if (!p.available) return `unavailable — ${p.error || 'gh failed'}`;
  return null;
}

/* Page size lives in CSS (.proj-scroll's --proj-page), not here: that one
 * value also computes the box height, so reading it back keeps the visible
 * row count a single layout fact instead of a magic number duplicated on
 * both sides. Falls back to the same 8 the stylesheet ships with. */
function projPageSize() {
  const box = $('projscroll');
  if (!box) return 8;
  const n = parseInt(getComputedStyle(box).getPropertyValue('--proj-page'), 10);
  return Number.isFinite(n) && n > 0 ? n : 8;
}

let projPage = 0;

function renderProjects(p) {
  const grid = $('projgrid');
  const pager = $('projpager');
  if (!grid) return;
  grid.replaceChildren();
  const info = p || { pending: true };

  const note = projNote(info);
  if (note) {
    if (pager) pager.classList.add('hidden');
    const row = document.createElement('div');
    // Span every column -- a status line is about the panel, not one repo.
    row.style.gridColumn = '1 / -1';
    row.className = 'text-faint truncate';
    row.textContent = note;
    grid.appendChild(row);
    return;
  }

  const rows = info.rows || [];

  if (!rows.length) {
    if (pager) pager.classList.add('hidden');
    const row = document.createElement('div');
    row.style.gridColumn = '1 / -1';
    row.className = 'text-faint';
    row.textContent = 'No open issues';
    grid.appendChild(row);
    return;
  }

  // Clamp every render, not just on click: the row list is refetched on a
  // background thread, so repos can disappear (archived, or gone stale past
  // projects.stale_after_days) while the user is parked on the last page.
  const size = projPageSize();
  const pages = Math.max(1, Math.ceil(rows.length / size));
  projPage = Math.min(Math.max(0, projPage), pages - 1);

  if (pager) {
    pager.classList.toggle('hidden', pages === 1);
    $('projpage').textContent = `${projPage + 1}/${pages}`;
    $('projprev').disabled = projPage === 0;
    $('projnext').disabled = projPage >= pages - 1;
  }

  rows.slice(projPage * size, projPage * size + size).forEach((r) => {
    const name = document.createElement('div');
    name.className = 'truncate text-ink';
    // Owner prefix only in the tooltip -- the card shows the repo, which is
    // what distinguishes rows; the org is mostly redundant noise at this
    // width. title still gives the full nameWithOwner on inspection.
    name.textContent = r.short || r.repo;
    name.title = `${r.repo} · ${r.blocked} blocked, ${r.actionable} actionable, `
      + `${r.closed} closed`;
    grid.appendChild(name);

    PROJ_COLS.forEach(([key, color, label]) => {
      const cell = document.createElement('div');
      const n = r[key] || 0;
      cell.className = n ? 'proj-num' : 'proj-num is-zero';
      if (n) cell.style.color = color;
      cell.textContent = String(n);
      cell.title = `${n} ${label}`;
      grid.appendChild(cell);
    });
  });
}

/* -------------------------------------------------------------- sessions */
/* Who logged in since the last boot (#71). The server sends the full sorted,
 * already-filtered row list (active first, then most recent login) and this
 * renders all of it -- the cap lives in config.sessions.max_rows, so there is
 * no second, invisible limit here.
 */

/* Local HH:MM from an epoch seconds value. The date is prefixed only when the
 * login did not happen today: a boot can span days (four-day uptimes are
 * normal on this machine), and a bare "20:37" on a row three days old reads as
 * this evening. Same 24-hour shape hhmm() produces for the weather times. */
function sessClock(epoch) {
  if (!epoch) return '—';
  const d = new Date(epoch * 1000);
  const hm = `${String(d.getHours()).padStart(2, '0')}:`
    + `${String(d.getMinutes()).padStart(2, '0')}`;
  const today = new Date();
  const sameDay = d.getFullYear() === today.getFullYear()
    && d.getMonth() === today.getMonth() && d.getDate() === today.getDate();
  return sameDay ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

/* Where the login came from, in the narrowest honest form. Only live sessions
 * have any of this: logind drops the session object on logout, so an ended row
 * genuinely has no tty/type to show and gets an em dash rather than a guess. */
function sessWhere(r) {
  if (r.remote && r.remote_host) return r.remote_host;
  return r.tty || r.type || (r.active ? 'session' : '—');
}

function sessNote(s) {
  if (s.disabled) return 'disabled in config';
  if (!s.available) return `unavailable — ${s.error || 'journal unreadable'}`;
  return null;
}

function renderSessions(s) {
  const grid = $('sessgrid');
  if (!grid) return;
  grid.replaceChildren();
  const info = s || { available: false };

  const span = (cls, text) => {
    const cell = document.createElement('div');
    // Span every column -- a status line is about the panel, not one session.
    cell.style.gridColumn = '1 / -1';
    cell.className = cls;
    cell.textContent = text;
    grid.appendChild(cell);
  };

  const note = sessNote(info);
  if (note) return span('text-faint truncate', note);

  const rows = info.rows || [];
  if (!rows.length) return span('text-faint', 'No logins this boot');

  rows.forEach((r) => {
    const who = document.createElement('div');
    who.className = r.active ? 'truncate sess-live' : 'truncate text-ink';
    who.textContent = r.user;
    const when = r.login_at
      ? new Date(r.login_at * 1000).toLocaleString()
      : 'login time unknown';
    who.title = `${r.user} · session ${r.session_id} · ${when}`
      + (r.active ? ' · still logged in' : '');
    grid.appendChild(who);

    const where = document.createElement('div');
    where.className = 'truncate text-faint';
    where.textContent = sessWhere(r);
    grid.appendChild(where);

    const login = document.createElement('div');
    login.className = 'sess-num text-faint';
    login.textContent = sessClock(r.login_at);
    grid.appendChild(login);

    const held = document.createElement('div');
    held.className = r.active ? 'sess-num sess-live' : 'sess-num text-faint';
    // A null duration is not zero: the session ended at a moment the journal
    // never recorded (see lib/sessions.py's ended_unknown). Rendering "0m"
    // there would assert something the data cannot support.
    held.textContent = r.duration_s === null || r.duration_s === undefined
      ? '?' : dur(r.duration_s);
    held.title = r.ended_unknown
      ? 'session ended, but no logout was recorded'
      : `${r.active ? 'logged in for' : 'lasted'} ${dur(r.duration_s || 0)}`;
    grid.appendChild(held);
  });

  if (info.truncated) {
    span('text-faint', `+${info.truncated} more this boot`);
  }
  return undefined;
}

/* -------------------------------------------------------- weather panels */
// No icons in this card (user request, #41). Spreadsheet-style key:value
// grid (#50, .kv-grid in index.html) -- Condition/Temp/Feels/High-Low/Wind/
// Humidity/Rain/UV each get one row, key column left, value column right,
// all sharing one CSS grid so the value column lines up top to bottom
// (the old 2x2 table plus a separately-laid-out Wind/Humid/Rain/UV list
// didn't share a column grid, so their value columns started at different
// x positions). Upcoming (renderWeek) is removed from this card (still
// called, targets a hidden node -- #40's pattern for Hourly/Sun & Moon).
function renderWeather(w, configLocation) {
  const descEl = $('wdesc');
  const feelsEl = $('wfeels');
  const tempEl = $('wtemp');
  const hiloEl = $('whilo');
  const windEl = $('wwind');
  const humidEl = $('whumid');
  const rainEl = $('wrain');
  const uvEl = $('wuv');
  // #77: the city this forecast is for, next to the "Weather" heading --
  // renders regardless of unavailable/stale, since it names the location
  // the (possibly stale) numbers below belong to, not a live reading
  // itself. w.location comes from the cached fetch and is missing when
  // there's no cache at all yet (total unavailability); configLocation
  // (state.config.location, always present) covers that gap.
  const locEl = $('wlocation');
  if (locEl) locEl.textContent = w.location || configLocation || '';
  if (w.unavailable) {
    if (descEl) {
      descEl.textContent = 'weather unavailable';
      descEl.classList.remove('alert-badge'); descEl.classList.add('text-ink');
    }
    if (feelsEl) feelsEl.textContent = '—';
    if (tempEl) {
      tempEl.textContent = '—';
      tempEl.classList.remove('alert-badge'); tempEl.classList.add('text-ink');
    }
    if (hiloEl) hiloEl.replaceChildren();
    if (windEl) windEl.textContent = '—';
    if (humidEl) humidEl.textContent = '—';
    if (rainEl) rainEl.textContent = '—';
    if (uvEl) uvEl.textContent = '—';
    return;
  }

  // w.alert.fields is computed server-side (bin/dashd-serve's
  // weather_alert(), #46/#48) against config.weather_alerts -- regex over
  // w.desc plus temp/high/rain% thresholds, same shape as the Log panel's
  // config.logs.patterns. Only the specific out-of-range value gets the
  // alert-badge treatment (white text, filled deep-red background, #49)
  // -- not the whole panel, which stays accent-amber regardless (#48: a
  // single hot value highlighting the whole card was the wrong shape).
  // Every value cell shares one base color, text-ink (#52 -- three
  // different colors used to be scattered across these rows), so it
  // swaps out for alert-badge rather than stacking, avoiding a same-
  // specificity tie between the two.
  const alert = w.alert?.fields || {};
  // #51: 000F, not 000°F -- w.units.temp comes off the wire as "°F"/"°C".
  const tempUnit = w.units.temp.replace('°', '');

  if (descEl) {
    descEl.textContent = w.desc
      + (w.stale ? ` · ${Math.round(w.age_seconds / 60)}m old` : '');
    const hot = Boolean(alert.condition);
    descEl.classList.toggle('alert-badge', hot);
    descEl.classList.toggle('text-ink', !hot);
  }
  if (feelsEl) feelsEl.textContent = `${w.apparent}${tempUnit}`;
  if (tempEl) {
    tempEl.textContent = `${w.temp}${tempUnit}`;
    const hot = Boolean(alert.temp);
    tempEl.classList.toggle('alert-badge', hot);
    tempEl.classList.toggle('text-ink', !hot);
  }
  if (hiloEl) {
    const hi = document.createElement('span');
    hi.textContent = `${w.high}${tempUnit}`;
    if (alert.high) hi.classList.add('alert-badge');
    const lo = document.createElement('span');
    lo.className = 'ml-[0.4vmin]';
    lo.textContent = `/ ${w.low}${tempUnit}`;
    hiloEl.replaceChildren(hi, lo);
  }
  if (windEl) windEl.textContent = `${w.wind}${w.units.wind} ${compass(w.wind_dir)}`;
  if (humidEl) humidEl.textContent = `${w.humidity}%`;
  if (rainEl) {
    rainEl.textContent = `${w.precip_prob ?? 0}%`;
    const hot = Boolean(alert.rain);
    rainEl.classList.toggle('alert-badge', hot);
    rainEl.classList.toggle('text-ink', !hot);
  }
  if (uvEl) uvEl.textContent = w.uv == null ? '—' : `${Math.round(w.uv)}`;

  renderHourly(w);
  renderWeek(w);
  renderSunMoon(w);
}

/* Air quality (#79) -- its own kv-grid row, own data source (state.air_quality,
 * not nested in weather: independent fetch/stale lifecycle server-side).
 * Degrades to hidden -- both the key and value spans -- the same way ghstats/
 * now_playing degrade, rather than rendering an empty/dashed row every poll. */
function renderAirQuality(aq) {
  const keyEl = $('waqi-key');
  const valEl = $('waqi');
  if (!keyEl || !valEl) return;
  const show = Boolean(aq) && !aq.unavailable && aq.aqi != null;
  keyEl.classList.toggle('hidden', !show);
  valEl.classList.toggle('hidden', !show);
  if (!show) return;
  valEl.textContent = `${aq.aqi} ${aq.category}`
    + (aq.stale ? ` · ${Math.round(aq.age_seconds / 60)}m old` : '');
  const hot = Boolean(aq.alert);
  valEl.classList.toggle('alert-badge', hot);
  valEl.classList.toggle('text-ink', !hot);
}

/* Official NWS alert banner (#80) -- state.nws_alerts, kept entirely
 * distinct from weather.alert (renderWeather() above): an active Red Flag
 * Warning and "today's high crossed a configured number" are different
 * questions. Renders the single most severe active alert (already sorted
 * server-side, dashd-serve's shape_nws_alerts()) or nothing at all -- a
 * daily "no alerts" row would train the eye to stop reading it. */
function renderNwsAlerts(nws) {
  const el = $('nwsalert');
  if (!el) return;
  const alerts = (nws && !nws.unavailable) ? (nws.alerts || []) : [];
  if (alerts.length === 0) {
    el.classList.add('hidden');
    el.textContent = '';
    return;
  }
  const a = alerts[0];
  el.textContent = a.event
    + (a.ends ? ` · until ${hhmm(a.ends)}` : '')
    + (alerts.length > 1 ? ` (+${alerts.length - 1} more)` : '');
  el.classList.remove('hidden');
}

/* Compact hourly strip: time + temp, no chart, no icon (#40). Replaces a
 * "Next 24 hours" sparkline that was vague and cost far more vertical
 * space than the info was worth (see #8). */
function renderHourly(w) {
  const pts = w.hourly.filter((h) => h.temp != null).slice(0, 6);
  $('hourly').replaceChildren(...pts.map((p) => {
    const cell = document.createElement('div');
    cell.className = 'flex flex-col items-center gap-[0.25vmin] flex-1 min-w-0';

    const t = document.createElement('div');
    t.className = 'text-faint num text-[clamp(.48rem,1vmin,.68rem)]';
    t.textContent = hhmm(p.time);

    const temp = document.createElement('div');
    temp.className = 'num text-ink/90 text-[clamp(.6rem,1.4vmin,1rem)]';
    temp.textContent = `${p.temp}°`;

    cell.append(t, temp);
    return cell;
  }));
}

function renderWeek(w) {
  // Local date, not toISOString() (UTC) -- same fix renderCalendar()
  // already needed. In the evening Pacific time this bug made
  // *tomorrow* match "today" instead, so today's own row never got the
  // "Today" label/emphasis and just looked like a muted past day (user
  // report: "remove yesterday's weather", when the actual bug was
  // today's row being mislabeled, not an extra past day in the data).
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-` +
    `${String(now.getDate()).padStart(2, '0')}`;
  // Drop anything before today (defensive) and cap at today + a couple
  // more days (user request: "just need today and the next couple of
  // days", not the full week).
  const days = w.daily.filter((d) => d.date >= today).slice(0, 3);
  $('week').replaceChildren(...days.map((d) => {
    const isToday = d.date === today;
    const row = document.createElement('div');
    row.className = 'flex items-center gap-[0.8vmin] py-[0.15vmin] ' +
      (isToday ? 'text-ink' : 'text-muted');
    const name = isToday ? 'Today'
      : new Date(d.date + 'T12:00').toLocaleDateString(undefined, { weekday: 'short' });

    const label = document.createElement('span');
    label.className = 'w-[20%] tracking-wide text-[clamp(.52rem,1.1vmin,.82rem)]' +
      (isToday ? ' font-medium' : '');
    label.textContent = name;

    const range = document.createElement('span');
    range.className = 'num flex-1 text-right text-[clamp(.55rem,1.2vmin,.88rem)]';
    range.innerHTML = `<span class="text-ink/90">${d.hi}°</span>` +
      `<span class="text-faint ml-[0.7vmin]">${d.lo}°</span>`;

    row.append(label, range);
    return row;
  }));
}

/* Calendar (#22): current-month grid, today highlighted. Source-agnostic --
 * built purely from the client clock, with an optional event-dot overlay
 * read from state.extra.calendar.events (a list of "YYYY-MM-DD" strings or
 * {date, label} objects). extra.json is the existing pass-through point
 * (bin/dashd-serve merges data/extra.json into /api/state verbatim), so a
 * future ICS/CalDAV sync script can populate real events without any
 * server or frontend change. No events configured -> just the bare grid. */
function renderCalendar(extra) {
  const host = $('calendar');
  if (!host) return;

  const now = new Date();
  const year = now.getFullYear(), month = now.getMonth(), today = now.getDate();
  const first = new Date(year, month, 1);
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const events = new Set(
    (extra?.calendar?.events || [])
      .map((e) => (typeof e === 'string' ? e : e?.date))
      .filter(Boolean),
  );

  const title = document.createElement('div');
  title.className = 'text-muted tracking-wide text-[clamp(.52rem,1.1vmin,.82rem)] mb-[0.5vmin]';
  title.textContent = first.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });

  // One shared font size on the grid itself (#53), not a per-element
  // text-[...] class on the weekday headers and day numbers separately --
  // those used to be two different sizes (headers smaller than numbers),
  // the same "each piece picked its own size" drift #50 fixed for Weather's
  // kv-grid by putting the size on the parent once and letting every child
  // inherit it.
  const grid = document.createElement('div');
  grid.className = 'grid grid-cols-7 gap-y-[0.3vmin] text-center text-[clamp(.48rem,1vmin,.7rem)]';

  ['S', 'M', 'T', 'W', 'T', 'F', 'S'].forEach((d) => {
    const head = document.createElement('div');
    head.className = 'text-faint uppercase';
    head.textContent = d;
    grid.appendChild(head);
  });

  const isoOf = (y, m, d) => `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;

  // #54: a day cell from ANY month -- adjacent-month spillover (dim=true)
  // shares this with the current month's own days, rather than duplicating
  // the markup, so the today-highlight/event-dot logic can't drift between
  // the two.
  const dayCell = (day, iso, isToday, dim) => {
    const cell = document.createElement('div');
    cell.className = 'flex flex-col items-center justify-center gap-[0.1vmin]';

    // #41/#42: a filled box marks today, not just bolder/brighter text, so
    // it reads at a glance -- but it wraps only the number itself (fixed 2px
    // border, fit-content box), not the whole grid cell, so it stays a
    // small compact square instead of stretching to the column width.
    // #47: filled solid (bg + border), not just outlined -- an unfilled
    // ring read as barely-there against the panel background. Text goes
    // dark (fixed near-black, not a themed color) since every theme's
    // --color-warm is a bright fill the light --color-ink text would wash
    // out on. #53: non-today numbers are text-ink (uniform "data"
    // brightness, matching every other value in the system since #52) --
    // today's filled box is the one deliberate exception, same role as
    // Weather's alert-badge, so the rest don't need to also be dimmed.
    // #54: dim (leading/trailing adjacent-month days) is a third state,
    // text-faint -- the same "de-emphasized" role the weekday headers
    // already use -- so last-July/early-September context reads as
    // present but clearly not this month's data.
    const num = document.createElement('span');
    num.className = 'num inline-flex items-center justify-center leading-none ' +
      'p-1 box-border ' +
      (isToday
        ? 'font-medium border-2 border-[var(--color-warm)] ' +
          'bg-[var(--color-warm)] text-[oklch(0.16_0_0)]'
        : dim
          ? 'text-faint border-2 border-transparent'
          : 'text-ink border-2 border-transparent');
    num.textContent = String(day);
    cell.appendChild(num);

    if (events.has(iso)) {
      const dot = document.createElement('span');
      dot.className = 'w-[0.22em] h-[0.22em] shrink-0';
      dot.style.background = isToday ? 'var(--color-ink)' : 'var(--color-warm)';
      cell.appendChild(dot);
    }

    return cell;
  };

  // Leading days: the tail end of the previous month, filling the grid up
  // to this month's first weekday instead of leaving blank cells.
  const leading = first.getDay();
  const prevMonthDays = new Date(year, month, 0).getDate();
  const prevYear = month === 0 ? year - 1 : year;
  const prevMonth = month === 0 ? 11 : month - 1;
  for (let i = leading - 1; i >= 0; i--) {
    const day = prevMonthDays - i;
    grid.appendChild(dayCell(day, isoOf(prevYear, prevMonth, day), false, true));
  }

  for (let day = 1; day <= daysInMonth; day++) {
    grid.appendChild(dayCell(day, isoOf(year, month, day), day === today, false));
  }

  // Trailing days: the start of the next month, filling the last row out
  // to a full multiple of 7 the same way.
  const trailing = (7 - ((leading + daysInMonth) % 7)) % 7;
  const nextYear = month === 11 ? year + 1 : year;
  const nextMonth = month === 11 ? 0 : month + 1;
  for (let day = 1; day <= trailing; day++) {
    grid.appendChild(dayCell(day, isoOf(nextYear, nextMonth, day), false, true));
  }

  host.replaceChildren(title, grid);
}

function renderSunMoon(w) {
  const svg = $('sunarc');
  svg.replaceChildren();
  const rise = new Date(w.sun.sunrise), set = new Date(w.sun.sunset);
  const now = new Date();
  const t = Math.min(Math.max((now - rise) / (set - rise), 0), 1);

  const W = 300, pad = 16, base = 74;
  const arc = `M ${pad} ${base} Q ${W / 2} ${-14} ${W - pad} ${base}`;
  svg.appendChild(el('path', { d: arc, fill: 'none',
    stroke: 'var(--color-hairline)', 'stroke-width': '2' }));

  const path = el('path', { d: arc, fill: 'none' });
  svg.appendChild(path);
  const len = path.getTotalLength();
  const daytime = now >= rise && now <= set;

  if (daytime) {
    const travelled = el('path', {
      d: arc, fill: 'none', stroke: 'var(--color-warm)', 'stroke-width': '2',
      'stroke-linecap': 'round',
      'stroke-dasharray': `${len * t} ${len}`,
    });
    svg.appendChild(travelled);
  }
  const p = path.getPointAtLength(len * t);
  svg.appendChild(el('line', { x1: pad, y1: base, x2: W - pad, y2: base,
    stroke: 'var(--color-hairline)', 'stroke-width': '1' }));
  svg.appendChild(el('circle', {
    cx: p.x, cy: p.y, r: daytime ? 6 : 4,
    fill: daytime ? 'var(--color-warm)' : 'var(--color-faint)',
  }));

  // Icon-led, not the old plain ↑/↓ unicode glyphs -- same sysIcon
  // technical stroke language as the System panel.
  const timeChip = (icon, time) => {
    const span = document.createElement('span');
    span.className = 'inline-flex items-center gap-[0.35vmin]';
    const ic = sysIcon(icon, 'var(--color-faint)');
    ic.classList.remove('sysicon');
    ic.classList.add('icon-inline');
    span.append(ic, document.createTextNode(time));
    return span;
  };
  $('suntimes').replaceChildren(
    timeChip('sunrise', hhmm(w.sun.sunrise)),
    timeChip('sunset', hhmm(w.sun.sunset)));

  // moon: shade a circle by illuminated fraction
  const m = $('moon');
  m.replaceChildren();
  const ph = w.moon.phase;
  m.appendChild(el('circle', { cx: 50, cy: 50, r: 46,
    fill: 'oklch(0.35 0.02 260)' }));
  const k = Math.cos(2 * Math.PI * ph);      // -1 full .. +1 new
  const waxing = ph < 0.5;
  const lit = el('path', {
    d: `M 50 4 A 46 46 0 ${waxing ? 1 : 0} ${waxing ? 1 : 0} 50 96
        A ${Math.abs(k) * 46} 46 0 ${k > 0 ? 1 : 0} ${waxing ? 0 : 1} 50 4 Z`,
    fill: 'var(--color-ink)', opacity: '.92',
  });
  m.appendChild(lit);
  $('moontext').innerHTML =
    `${w.moon.name}<br><span class="text-muted num">` +
    `${Math.round(w.moon.illumination * 100)}% lit</span>`;
}

/* ------------------------------------------------------------------ poll */
/* ------------------------------------------------------- auto-cycle reveal
 * The desktop surface sits below every window with no pointer/keyboard
 * reach (#29) -- a panel whose content outgrows its box has no scrollbar a
 * user could ever grab. Any element marked class="auto-cycle" (with a CSS
 * height/max-height so it can actually overflow) opts into a passive
 * scroll-dwell-reset loop that reveals the rest of its content over time.
 * class="auto-cycle-x" is the same idea on the horizontal axis (scrollLeft
 * instead of scrollTop) -- Music's song-title marquee. refreshAutoCycles()
 * is called every apply() and is the only integration point a future panel
 * needs -- it doesn't touch anything that already fits its box.
 */
const autoCycles = new WeakMap();

function stopAutoCycle(el) {
  const c = autoCycles.get(el);
  if (!c) return;
  clearTimeout(c.timer);
  autoCycles.delete(el);
  if (c.axis === 'x') el.scrollLeft = 0; else el.scrollTop = 0;
}

function driveAutoCycle(el, overflow, axis, { stepPx = 1, stepMs = 45, dwellMs = 2600 } = {}) {
  const c = { overflow, axis, timer: null };
  autoCycles.set(el, c);
  const scrollProp = axis === 'x' ? 'scrollLeft' : 'scrollTop';
  const step = () => {
    const max = axis === 'x' ? el.scrollWidth - el.clientWidth : el.scrollHeight - el.clientHeight;
    if (el[scrollProp] >= max - 0.5) {
      c.timer = setTimeout(() => {
        el[scrollProp] = 0;
        c.timer = setTimeout(step, dwellMs);
      }, dwellMs);
      return;
    }
    el[scrollProp] = Math.min(max, el[scrollProp] + stepPx);
    c.timer = setTimeout(step, stepMs);
  };
  c.timer = setTimeout(step, dwellMs);
}

function refreshAutoCycles() {
  // Static snapshot renders (headless screenshot, ?static=1) and
  // prefers-reduced-motion both want a single settled frame, not a
  // perpetually running timer chain.
  const skipMotion = STATIC || matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.querySelectorAll('.auto-cycle, .auto-cycle-x').forEach((box) => {
    const axis = box.classList.contains('auto-cycle-x') ? 'x' : 'y';
    const overflow = axis === 'x'
      ? box.scrollWidth - box.clientWidth
      : box.scrollHeight - box.clientHeight;
    const running = autoCycles.get(box);
    if (overflow <= 2 || skipMotion) {
      if (running) stopAutoCycle(box);
      return;
    }
    // Same shape as the in-flight cycle -- let it keep going instead of
    // yanking back to the top on every ~5s poll.
    if (running && running.axis === axis && Math.abs(running.overflow - overflow) < 4) return;
    if (running) stopAutoCycle(box);
    driveAutoCycle(box, overflow, axis);
  });
}

/* ---------------------------------------------------------- panel layout
 * #91 (follow-up to #3): outputs.<name>.layout reaches this far but nothing
 * used to key off it. #root is a 2- or 3-track CSS Grid of #colLeft/
 * #colCenter/#colRight (see index.html's [data-layout] rules); this is
 * what decides which of those columns each top-level panel <section>
 * belongs in, and physically moves it there.
 *
 * A CSS grid-template-areas switch can't do this: each column stacks
 * panels of genuinely different natural heights (System/Projects/Sessions
 * vs. Forecast/Music/Log), and grid-template-areas requires every named
 * area to form a rectangle, which independent per-column stacks don't --
 * so the columns are real DOM containers and the panels move between them.
 */
const LAYOUTS = {
  narrow: {
    colLeft: ['secSystem', 'secProjects', 'secSessions'],
    colCenter: [],
    colRight: ['secForecast', 'musicSection', 'secLog'],
  },
  // Sessions moves under Forecast in its own center column -- this is what
  // actually fills the wide output's empty middle third, rather than just
  // stretching the two flanking columns wider with nothing new in them.
  wide: {
    colLeft: ['secSystem', 'secProjects'],
    colCenter: ['secForecast', 'secSessions'],
    colRight: ['musicSection', 'secLog'],
  },
};

// 'auto' (the default, unset in config.json) resolves by measured aspect
// ratio rather than a fixed pixel breakpoint -- the two real outputs here
// are 1.6 (1440x900) and 2.37 (2560x1080), and >=2 is "has room for a
// third column." An explicit 'wide'/'narrow' in config.json always wins.
// w/h default to this window; the profile snapshot passes another monitor's.
function resolveLayout(raw, w = innerWidth, h = innerHeight) {
  if (raw === 'wide' || raw === 'narrow') return raw;
  return (w / Math.max(h, 1)) >= 2 ? 'wide' : 'narrow';
}

// The panels the Cards menu (#94) can place, in their default display order.
const CARDS = [
  ['secSystem', 'System'], ['secProjects', 'Projects'],
  ['secSessions', 'Sessions'], ['secForecast', 'Forecast'],
  ['musicSection', 'Music'], ['secLog', 'Log'],
];
const CARD_LABEL = Object.fromEntries(CARDS);
const LAYOUT_COLS = {
  narrow: ['colLeft', 'colRight'],
  wide: ['colLeft', 'colCenter', 'colRight'],
};

// The enabled cards for an output: its explicit outputs.<name>.cards, else
// LAYOUTS' hand-tuned placement flattened row by row -- which the
// round-robin in placePanels() deals back into exactly those columns, so an
// output with no `cards` looks the same as before #94.
function outputCards(outCfg, layout) {
  if (Array.isArray(outCfg.cards)) return outCfg.cards.filter((id) => CARD_LABEL[id]);
  const cols = LAYOUT_COLS[layout].map((c) => LAYOUTS[layout][c]);
  const out = [];
  for (let r = 0; r < Math.max(...cols.map((c) => c.length)); r += 1) {
    cols.forEach((c) => { if (c[r]) out.push(c[r]); });
  }
  return out;
}

let placedKey = null;
function placePanels(resolved, cards) {
  const key = `${resolved}|${cards.join()}`;
  if (placedKey === key) return; // idempotent -- apply() runs every poll
  placedKey = key;
  const colIds = LAYOUT_COLS[resolved] || LAYOUT_COLS.narrow;
  for (const [id] of CARDS) {
    const sec = $(id);
    if (sec) sec.style.display = cards.includes(id) ? '' : 'none';
  }
  // Deal enabled cards across the columns in order, so the list order is
  // reading order and reordering in the menu moves a panel visibly.
  cards.forEach((id, i) => {
    const sec = $(id);
    if (sec) $(colIds[i % colIds.length]).appendChild(sec);
  });
  ['colLeft', 'colCenter', 'colRight'].forEach((colId) => {
    const col = $(colId);
    // index.html's [data-layout] rule is what actually removes #colCenter
    // from grid layout under 'narrow' (display:none there, so it claims no
    // track and #colLeft/#colRight land in #root's two explicit columns
    // instead of wrapping to a second row) -- this is a defensive second
    // layer keyed off real content rather than the layout name.
    if (col) col.classList.toggle('hidden', !Array.from(col.children).some((c) => c.style.display !== 'none'));
  });
}

function apply(s) {
  state = s;

  const disp = s.config.display || {};
  const outCfg = (s.config.outputs || {})[OUTPUT] || (s.config.outputs || {}).default || {};
  // #94: tint_opacity (0-1, the menu's slider) scales the whole tint layer;
  // display.transparent stays as the one-key config revert meaning 0.
  const opacity = disp.transparent ? 0 : (disp.tint_opacity ?? 1);
  document.body.classList.toggle('transparent', opacity < 1);
  $('suntint').style.opacity = opacity;
  const layout = resolveLayout(outCfg.layout || 'auto');
  document.documentElement.dataset.layout = layout;
  placePanels(layout, outputCards(outCfg, layout));
  // Theme toggle surface: a config.json field for now (issue #32) -- a
  // natural future home is the Control Backend (#30) once it exists, but
  // this doesn't block on that. data-theme on <html> drives the CSS
  // variable overrides and the CRT fx layer in index.html.
  document.documentElement.dataset.theme = disp.theme || 'night_ops';
  const root = document.documentElement.style;
  root.setProperty('--safe-top', `${disp.safe_area_top || 0}px`);
  root.setProperty('--safe-bottom', `${disp.safe_area_bottom || 0}px`);

  const tint = sunTint(s.weather && s.weather.sun, tintNow(), disp.tint_strength);
  root.setProperty('--tint-top', tint.top);
  root.setProperty('--tint-glow', tint.glow);
  root.setProperty('--tint-glow-pos', tint.glowPos);
  root.setProperty('--tint-bottom', tint.bottom);

  pushRing(s.sys);
  renderWeather(s.weather, s.config.location);
  renderAirQuality(s.air_quality);
  renderNwsAlerts(s.nws_alerts);
  renderCpuMemBat(s.sys);
  renderUpdates(s.updates);
  renderDisks(s.sys.disks, s.sys.swap);
  renderDiskIO(s.sys);
  renderNetwork(s.sys);
  renderMusic(s.music);
  renderCalendar(s.extra);
  renderProjects(s.projects);
  renderSessions(s.sessions);
  renderLog(s.logs);
  refreshAutoCycles();

  const boot = $('boot');
  if (boot && !boot.dataset.done) {
    boot.dataset.done = '1';
    boot.style.opacity = '0';
    setTimeout(() => boot.remove(), 550);

    // The entrance stagger uses animation-fill-mode:both, which parks the
    // panel at opacity 0 until the animation actually runs. If frames are
    // throttled or animations disabled, that would leave the wallpaper
    // permanently blank -- so always strip the class back off. Visibility
    // must never depend on an animation completing.
    if (!STATIC && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
      const panels = [...document.querySelectorAll('.panel')];
      panels.forEach((p, i) => {
        p.classList.add('fadein');
        p.style.animationDelay = `${i * 60}ms`;
      });
      setTimeout(() => panels.forEach((p) => {
        p.classList.remove('fadein');
        p.style.animationDelay = '';
      }), 1600);
    }
  }

  // #91 verification hook, STATIC-only (never runs on the live wallpaper):
  // a headless render can screenshot a page and still not prove anything
  // clips, since refreshAutoCycles() is a no-op under STATIC and only ever
  // shows page 1. Dump every top-level panel's real rect plus how much
  // overflow each bounded/auto-cycle box is carrying, as plain JSON text a
  // --dump-dom render can read back -- an honest coordinate table instead
  // of eyeballing a screenshot.
  if (STATIC) {
    const dbg = $('layoutdebug');
    if (dbg) {
      const rects = {};
      document.querySelectorAll('main#root > * > [id]').forEach((sec) => {
        if (!sec.id) return;
        const r = sec.getBoundingClientRect();
        rects[sec.id] = {
          top: Math.round(r.top), left: Math.round(r.left),
          right: Math.round(r.right), bottom: Math.round(r.bottom),
        };
      });
      const overflow = {};
      document.querySelectorAll('.auto-cycle, .proj-scroll').forEach((box, i) => {
        overflow[box.id || `box${i}`] = box.scrollHeight - box.clientHeight;
      });
      dbg.textContent = JSON.stringify({
        layout, innerWidth, innerHeight, rects, overflow,
      });
    }
  }
}

async function poll() {
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    apply(await r.json());
    failures = 0;
    $('offline').classList.add('hidden');
  } catch {
    // Two consecutive misses before crying wolf -- a single dropped poll
    // during a server restart should not flash a banner on the wallpaper.
    if (++failures >= 2) $('offline').classList.remove('hidden');
  } finally {
    const ms = ((state?.config.poll) || 5) * 1000;
    setTimeout(poll, ms);
  }
}

/* --------------------------------------------------------------- controls
 * Live since #36: the surface used to have an empty input region, so nothing
 * on the page could ever be clicked. dashd-host now leaves the region at its
 * default (display.interactive), while keyboard mode stays NONE -- so these
 * controls work without the dashboard ever stealing focus from a real window.
 */

/* A one-shot refresh, deliberately NOT poll(). poll() re-arms itself with
 * setTimeout, so calling it from a click handler would fork a second polling
 * chain and permanently double the request rate for the life of the page. */
async function refreshNow() {
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    if (r.ok) apply(await r.json());
  } catch { /* the regular poll is still running; it will catch up */ }
}

// Fingerprint of the bit of music state a transport click can change --
// used only to detect that MPRIS has actually propagated the new state,
// never sent anywhere.
function musicFingerprint() {
  const m = state?.music;
  return m ? `${m.title}|${m.artist}|${m.playing}` : null;
}

async function musicCommand(command) {
  const before = musicFingerprint();
  try {
    await fetch('/api/music/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command }),
    });
  } catch { /* nothing useful to show on a wallpaper; next poll re-syncs */ }
  // Players report the new state asynchronously over MPRIS, so an immediate
  // re-read often still returns the old status -- a single fixed-delay
  // re-poll was frequently too early and left the panel showing the old
  // track until the *next* regular poll caught up. Instead, re-poll a
  // few times with backoff, stopping the moment the fingerprint actually
  // changes, so the common case (fast propagation) updates almost
  // immediately and the slow case still gets several more chances within
  // about a second, rather than one shot at a fixed 250ms.
  for (const delay of [150, 200, 250, 400]) {
    await new Promise((resolve) => setTimeout(resolve, delay));
    await refreshNow();
    if (musicFingerprint() !== before) return;
  }
}

function wireControls() {
  const step = (delta) => {
    projPage += delta;
    if (state) renderProjects(state.projects);
  };
  $('projprev')?.addEventListener('click', () => step(-1));
  $('projnext')?.addEventListener('click', () => step(1));

  $('musicPrev')?.addEventListener('click', () => musicCommand('previous'));
  $('musicNext')?.addEventListener('click', () => musicCommand('next'));
  $('musicToggle')?.addEventListener('click', () => musicCommand('play-pause'));
}

/* ----------------------------------------------------------- context menu
 * #66, "configure from the interface itself"; restructured at #94 into
 * Profiles / Cards / Settings. Data-driven: a new entry is an item in one
 * of the *Items() functions below, not a new code path.
 *
 * Suppresses WebKit's own context-menu (Reload / Inspect Element / Open
 * Frame in New Window ...), which the surface would otherwise show now
 * that #36 gave it pointer reach -- see dashd-host's belt-and-braces
 * `context-menu` handler for the other half of that fix.
 */
// Applies a config patch to the page's own copy of state.config first, so
// the menu and layout react at once, then persists it (#94). Only sections
// whose served shape equals their patch shape are mirrored: `location` is
// served as just a name, so it waits for the refetch below instead.
// Semantics match the server: arrays replace, null deletes a key.
function mergeInto(dst, patch) {
  for (const [k, v] of Object.entries(patch)) {
    if (v === null) {
      delete dst[k];
    } else if (v && typeof v === 'object' && !Array.isArray(v)
        && dst[k] && typeof dst[k] === 'object' && !Array.isArray(dst[k])) {
      mergeInto(dst[k], v);
    } else {
      dst[k] = v;
    }
  }
}

async function patchConfig(patch) {
  if (state) {
    for (const section of ['display', 'outputs', 'profiles']) {
      if (!patch[section]) continue;
      state.config[section] = state.config[section] || {};
      mergeInto(state.config[section], patch[section]);
    }
    apply(state);
    if (menuCtx) renderMenu();
  }
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
  } catch { /* next poll re-syncs from whatever the server actually holds */ }
  await refreshNow();
  if (menuCtx) renderMenu();
}

const setTheme = (theme) => patchConfig({ display: { theme } });

const setLocation = (loc) => patchConfig({
  location: {
    name: loc.name, latitude: loc.latitude,
    longitude: loc.longitude, timezone: loc.timezone,
  },
});

// data/locations.json entries plus whatever's currently configured (#77) --
// deduped by name so the active selection doesn't show up twice when it's
// also one of the saved locations. The current selection only carries a
// `name` in state.config (bin/dashd-serve's build_state trims it to that),
// so an entry with no lat/lon/timezone gets a no-op action instead of
// resending a patch it doesn't have full data for -- it's just there to be
// checked, not reselected.
function locationItems() {
  const known = new Map();
  for (const loc of state?.locations || []) known.set(loc.name, loc);
  const currentName = state?.config?.location;
  if (currentName && !known.has(currentName)) {
    known.set(currentName, { name: currentName });
  }
  return [...known.values()].map((loc) => {
    const checked = loc.name === currentName;
    const complete = loc.latitude != null && loc.longitude != null
      && loc.timezone != null;
    return {
      label: `${loc.name}${checked ? ' ✓' : ''}`,
      action: complete ? () => setLocation(loc) : () => {},
    };
  });
}

// Off / Subtle / Medium / Strong, active one checked (#88), plus an Opacity
// slider (#94): 100% is the opaque surface, 0% removes the tint entirely so
// the wallpaper photo shows through. Strengths compare by value rather than
// name so a hand-edited or pre-#88 config (no tint_strength set at all)
// still lands on Medium via sunTint()'s own default.
function opacitySlider() {
  const disp = state?.config?.display || {};
  const start = disp.transparent ? 0 : (disp.tint_opacity ?? 1);
  const row = document.createElement('div');
  row.className = 'ctx-menu-row ctx-slider';
  const label = document.createElement('span');
  const input = document.createElement('input');
  input.type = 'range';
  input.min = '0';
  input.max = '100';
  input.step = '5';
  input.value = String(Math.round(start * 100));
  const show = () => { label.textContent = `Opacity ${input.value}%`; };
  show();
  // Live preview while dragging touches only the local copy; one POST on
  // release. Re-rendering the menu mid-drag would destroy the slider.
  input.addEventListener('input', () => {
    show();
    state.config.display.tint_opacity = Number(input.value) / 100;
    state.config.display.transparent = false;
    apply(state);
  });
  input.addEventListener('change', () => {
    patchConfig({ display: { transparent: false, tint_opacity: Number(input.value) / 100 } });
  });
  row.append(label, input);
  return row;
}

function tintItems() {
  const current = state?.config?.display?.tint_strength ?? TINT_STRENGTHS.medium;
  return [
    { build: opacitySlider },
    ...Object.entries(TINT_STRENGTHS).map(([name, value]) => ({
      label: `${name[0].toUpperCase()}${name.slice(1)}${value === current ? ' ✓' : ''}`,
      action: () => patchConfig({ display: { tint_strength: value } }),
    })),
  ];
}

function themeItems() {
  const disp = state?.config.display || {};
  const current = disp.theme || 'night_ops';
  return (disp._theme_options || [current]).map((t) => ({
    label: `${t.replace(/_/g, ' ')}${t === current ? ' ✓' : ''}`,
    action: () => setTheme(t),
  }));
}

function settingsItems() {
  return [
    { label: 'Location', children: locationItems },
    { label: 'Tint', children: tintItems },
    { label: 'Theme', children: themeItems },
    { label: 'Reload', action: () => location.reload() },
  ];
}

/* ------------------------------------------------------------------ cards
 * #94: which panels this monitor shows, in what order. outputs.<name>.cards
 * is the ordered list of enabled ids; enabling appends, so panels appear in
 * the order they were enabled. Every menu edit writes the whole list.
 */
function currentOutputCfg() {
  const o = state?.config?.outputs || {};
  return o[OUTPUT] || o.default || {};
}

function currentCards() {
  const oc = currentOutputCfg();
  return outputCards(oc, resolveLayout(oc.layout || 'auto'));
}

const setCards = (cards) => patchConfig({ outputs: { [OUTPUT]: { cards } } });

function moveCard(cards, i, delta) {
  const next = [...cards];
  [next[i], next[i + delta]] = [next[i + delta], next[i]];
  return next;
}

function ctxButton(label, onClick, cls = '') {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = `ctx-menu-item ${cls}`.trim();
  btn.textContent = label;
  btn.addEventListener('click', onClick);
  return btn;
}

function cardRow(id, i, cards) {
  const row = document.createElement('div');
  row.className = 'ctx-menu-row';
  row.appendChild(ctxButton(`✓ ${CARD_LABEL[id]}`,
    () => setCards(cards.filter((c) => c !== id))));
  const up = ctxButton('↑', () => setCards(moveCard(cards, i, -1)), 'ctx-mini');
  up.disabled = i === 0;
  const down = ctxButton('↓', () => setCards(moveCard(cards, i, 1)), 'ctx-mini');
  down.disabled = i === cards.length - 1;
  row.append(up, down);
  return row;
}

function cardItems() {
  const cards = currentCards();
  const items = [{ head: `Cards · ${OUTPUT}` }];
  cards.forEach((id, i) => items.push({ build: () => cardRow(id, i, cards) }));
  for (const [id, label] of CARDS) {
    if (!cards.includes(id)) {
      items.push({ label: `○ ${label}`, keep: true, action: () => setCards([...cards, id]) });
    }
  }
  return items;
}

/* --------------------------------------------------------------- profiles
 * #94: a profile is a saved snapshot of every detected monitor's cards.
 * Click applies it; holding a row for HOLD_MS deletes it, with a progress
 * bar under the row (shown after HOLD_SHOW_MS so a plain click never
 * flashes one). The surface has no keyboard, so Save names them itself.
 */
const HOLD_MS = 3000;
const HOLD_SHOW_MS = 250;

function snapshotOutputs() {
  const cfgOutputs = state?.config?.outputs || {};
  const detected = state?.outputs_detected || [];
  const rows = detected.length
    ? detected
    : [{ key: OUTPUT, width: innerWidth, height: innerHeight }];
  const snap = {};
  for (const r of rows) {
    const oc = cfgOutputs[r.key] || cfgOutputs.default || {};
    snap[r.key] = { cards: outputCards(oc, resolveLayout(oc.layout || 'auto', r.width, r.height)) };
  }
  return snap;
}

function profileActive(profile) {
  const snap = snapshotOutputs();
  const keys = Object.keys(profile.outputs || {});
  return keys.length > 0 && keys.every(
    (k) => snap[k] && snap[k].cards.join() === profile.outputs[k].cards.join());
}

function saveProfile() {
  const profiles = state?.config?.profiles || {};
  let n = 1;
  while (`Profile ${n}` in profiles) n += 1;
  return patchConfig({ profiles: { [`Profile ${n}`]: { outputs: snapshotOutputs() } } });
}

function profileRow(name, profile) {
  const wrap = document.createElement('div');
  wrap.className = 'ctx-hold ctx-menu-row';
  const btn = ctxButton(`${profileActive(profile) ? '✓ ' : ''}${name}`, () => {
    if (swallowClick) { swallowClick = false; return; }
    patchConfig({ outputs: profile.outputs });
  });
  // #96: overwrite this profile with the current layout (same name, so no
  // delete + recreate). Sits outside btn, so it never joins the hold-to-delete.
  const upd = ctxButton('✎', () => patchConfig(
    { profiles: { [name]: { outputs: snapshotOutputs() } } }), 'ctx-mini');
  upd.title = 'Overwrite with current layout';
  let swallowClick = false;
  let showTimer = null;
  let deleteTimer = null;
  const cancel = () => {
    clearTimeout(showTimer);
    clearTimeout(deleteTimer);
    wrap.querySelector('.ctx-hold-bar')?.remove();
  };
  btn.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    swallowClick = false;
    showTimer = setTimeout(() => {
      // A hold long enough to show the bar is a delete attempt, not a click:
      // letting go early must not apply the profile.
      swallowClick = true;
      const bar = document.createElement('div');
      bar.className = 'ctx-hold-bar';
      bar.style.setProperty('--hold-ms', `${HOLD_MS - HOLD_SHOW_MS}ms`);
      const fill = document.createElement('i');
      bar.appendChild(fill);
      wrap.appendChild(bar);
      requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = '100%'; }));
    }, HOLD_SHOW_MS);
    deleteTimer = setTimeout(() => {
      cancel();
      patchConfig({ profiles: { [name]: null } });
    }, HOLD_MS);
  });
  for (const t of ['pointerup', 'pointerleave', 'pointercancel']) btn.addEventListener(t, cancel);
  wrap.append(btn, upd);
  return wrap;
}

function profileItems() {
  const profiles = state?.config?.profiles || {};
  const items = [{ label: '+ Save current layout', keep: true, action: saveProfile }];
  const names = Object.keys(profiles).sort();
  if (!names.length) items.push({ head: 'No saved profiles' });
  for (const name of names) items.push({ build: () => profileRow(name, profiles[name]) });
  return items;
}

function rootItems() {
  return [
    { label: 'Profiles', children: profileItems },
    { label: 'Cards', children: cardItems },
    { label: 'Settings', children: settingsItems },
  ];
}

// The open menu: { menu, x, y, stack } where each stack entry is a function
// returning that level's items, re-run on every render so a level always
// shows current state (a toggled card, a saved profile).
let menuCtx = null;
let ctxOutsideHandler = null;

function closeContextMenu() {
  $('ctxMenu')?.remove();
  menuCtx = null;
  if (ctxOutsideHandler) {
    removeEventListener('pointerdown', ctxOutsideHandler, true);
    ctxOutsideHandler = null;
  }
  removeEventListener('keydown', ctxMenuKeydown, true);
}

function ctxMenuKeydown(e) {
  if (e.key === 'Escape') closeContextMenu();
}

function positionMenu(menu, x, y) {
  // Clamp inside the viewport rather than letting the menu render off-screen
  // -- a right-click near the output's right/bottom edge is the common case
  // on a wallpaper that spans the whole screen. Re-run on every render, not
  // just at open time -- a submenu is rarely the same height as the root.
  const rect = menu.getBoundingClientRect();
  const left = Math.min(x, innerWidth - rect.width - 4);
  const top = Math.min(y, innerHeight - rect.height - 4);
  menu.style.left = `${Math.max(4, left)}px`;
  menu.style.top = `${Math.max(4, top)}px`;
}

// Item shapes: { label, children } opens a level; { label, action } runs
// and closes (or stays open with keep); { head } is a caption; { build }
// returns a custom row element.
function renderMenu() {
  const { menu, x, y, stack } = menuCtx;
  menu.replaceChildren();
  if (stack.length > 1) {
    menu.appendChild(ctxButton('‹ Back', () => { stack.pop(); renderMenu(); }));
  }
  for (const item of stack[stack.length - 1]()) {
    if (item.head) {
      const h = document.createElement('div');
      h.className = 'ctx-menu-head';
      h.textContent = item.head;
      menu.appendChild(h);
    } else if (item.build) {
      menu.appendChild(item.build());
    } else if (item.children) {
      menu.appendChild(ctxButton(`${item.label} ›`, () => {
        stack.push(item.children);
        renderMenu();
      }));
    } else {
      menu.appendChild(ctxButton(item.label, () => {
        if (item.keep) {
          item.action();
        } else {
          closeContextMenu();
          item.action();
        }
      }));
    }
  }
  positionMenu(menu, x, y);
}

function openContextMenu(x, y) {
  closeContextMenu();
  const menu = document.createElement('div');
  menu.id = 'ctxMenu';
  menu.className = 'panel accent-cyan ctx-menu';
  document.body.appendChild(menu);
  menuCtx = { menu, x, y, stack: [rootItems] };
  renderMenu();

  // Any click outside, or Escape, dismisses -- capture phase so it fires
  // before the click that opened it (its own contextmenu listener already
  // stopped propagation) and before a click lands on page content behind it.
  // Must ignore pointerdowns that land inside the menu itself (#68): a menu
  // item's own click handler already calls closeContextMenu() before running
  // its action, and pointerdown fires before click -- an unconditional close
  // here removed the button from the DOM first, so its click never landed
  // and the input fell through to whatever was behind the menu instead.
  setTimeout(() => {
    ctxOutsideHandler = (e) => {
      if (!menu.contains(e.target)) closeContextMenu();
    };
    addEventListener('pointerdown', ctxOutsideHandler, true);
    addEventListener('keydown', ctxMenuKeydown, true);
  }, 0);
}

addEventListener('contextmenu', (e) => {
  e.preventDefault();
  openContextMenu(e.clientX, e.clientY);
});

// SVG geometry is computed from measured pixel sizes, so a monitor change or
// layout reflow has to redraw rather than just rescale.
let resizeTimer;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state) apply(state); }, 200);
});

wireControls();
poll();
