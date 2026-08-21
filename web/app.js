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

let state = null;
let failures = 0;

// A window's worth of *this browser session's* /api/state polls (default
// every 5s). Backs the Network chart, which wants finer resolution than
// the server-side history DB offers (throughput bursts are brief; 30s-
// resolution DB samples would flatten them). Capped by wall-clock age, not
// point count, so it self-adjusts to whatever the configured poll interval
// actually is.
const RING_WINDOW_MS = 5 * 60 * 1000;
let ring = { t: [], down: [], up: [] };
function pushRing(s) {
  const now = Date.now();
  ring.t.push(now); ring.down.push(s.net.down); ring.up.push(s.net.up);
  const cutoff = now - RING_WINDOW_MS;
  while (ring.t.length && ring.t[0] < cutoff) {
    ring.t.shift(); ring.down.shift(); ring.up.shift();
  }
}
function ringSince(key, ms) {
  const cutoff = Date.now() - ms;
  const i = ring.t.findIndex((t) => t >= cutoff);
  return i === -1 ? [] : ring[key].slice(i);
}

/* ------------------------------------------------------------------ icons */
/* Hand-rolled so there is zero external asset dependency. */
function weatherIcon(kind) {
  const g = el('svg', { viewBox: '0 0 64 64', class: 'w-full h-full' });
  const sun = (cx, cy, r, col = 'var(--color-warm)') => {
    g.appendChild(el('circle', { cx, cy, r, fill: col, opacity: '.95' }));
    for (let i = 0; i < 8; i++) {
      const a = (i * Math.PI) / 4;
      g.appendChild(el('line', {
        x1: cx + Math.cos(a) * (r + 3), y1: cy + Math.sin(a) * (r + 3),
        x2: cx + Math.cos(a) * (r + 8), y2: cy + Math.sin(a) * (r + 8),
        stroke: col, 'stroke-width': 3, 'stroke-linecap': 'round', opacity: '.8',
      }));
    }
  };
  const moon = (cx, cy, r) => {
    const p = el('path', {
      d: `M ${cx + r * 0.35} ${cy - r} a ${r} ${r} 0 1 0 ${r * 0.62} ${r * 1.7}
          a ${r * 0.82} ${r * 0.82} 0 1 1 ${-r * 0.62} ${-r * 1.7} Z`,
      fill: 'var(--color-ink)', opacity: '.9',
    });
    g.appendChild(p);
  };
  const cloud = (dx = 0, dy = 0, col = 'var(--color-ink)', op = '.85') => {
    g.appendChild(el('path', {
      d: `M ${18 + dx} ${44 + dy} a 10 10 0 0 1 0 -20 a 13 13 0 0 1 25 -4
          a 9 9 0 0 1 2 24 Z`,
      fill: col, opacity: op,
    }));
  };
  const drops = (col, n = 3, dash = false) => {
    for (let i = 0; i < n; i++) {
      g.appendChild(el('line', {
        x1: 22 + i * 9, y1: 48, x2: 19 + i * 9, y2: 58,
        stroke: col, 'stroke-width': 3, 'stroke-linecap': 'round',
        ...(dash ? { 'stroke-dasharray': '2 4' } : {}),
      }));
    }
  };

  switch (kind) {
    case 'clear':          sun(32, 30, 13); break;
    case 'clear-night':    moon(34, 30, 15); break;
    case 'partly':         sun(24, 24, 9); cloud(6, 4); break;
    case 'partly-night':   moon(26, 23, 11); cloud(6, 4); break;
    case 'cloudy':         cloud(2, 0, 'var(--color-muted)'); cloud(8, 6); break;
    case 'fog':
      cloud(4, -2, 'var(--color-muted)', '.6');
      for (let i = 0; i < 3; i++) g.appendChild(el('line', {
        x1: 14, y1: 46 + i * 6, x2: 50, y2: 46 + i * 6,
        stroke: 'var(--color-muted)', 'stroke-width': 3, 'stroke-linecap': 'round',
        opacity: .7 - i * .15 }));
      break;
    case 'drizzle': cloud(4, -4); drops('var(--color-accent)', 3, true); break;
    case 'rain':    cloud(4, -4); drops('var(--color-accent)', 3); break;
    case 'sleet':   cloud(4, -4); drops('var(--color-accent)', 2);
                    g.appendChild(el('circle', { cx: 44, cy: 54, r: 3,
                      fill: 'var(--color-ink)' })); break;
    case 'snow':
      cloud(4, -4);
      for (let i = 0; i < 3; i++) g.appendChild(el('circle', {
        cx: 21 + i * 10, cy: 53 + (i % 2) * 5, r: 3.2, fill: 'var(--color-ink)',
        opacity: '.9' }));
      break;
    case 'storm':
      cloud(4, -6, 'var(--color-muted)');
      g.appendChild(el('path', { d: 'M 34 44 L 27 57 L 33 57 L 29 66 L 41 52 L 34 52 Z',
        fill: 'var(--color-warm)' }));
      break;
    default: sun(32, 30, 13);
  }
  return g;
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
const hhmm = (iso) => (iso || '').slice(11, 16);
const POINTS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
  'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
const compass = (deg) => POINTS[Math.round(deg / 22.5) % 16];

/* ------------------------------------------------------------ sys panel */
/* Hand-rolled system-metric icons -- same zero-dependency philosophy as
 * weatherIcon(). stroke/fill are set once on the root <svg> and inherit
 * down through SVG's normal property cascade; individual shapes only
 * override fill where they're meant to read as solid, not outlined. */
function sysIcon(kind, tone = 'currentColor') {
  const g = el('svg', { viewBox: '0 0 24 24', class: 'sysicon',
    fill: 'none', stroke: tone, 'stroke-width': 1.6,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round' });
  switch (kind) {
    case 'cpu':
      g.appendChild(el('rect', { x: 6, y: 6, width: 12, height: 12, rx: 1.2 }));
      g.appendChild(el('rect', { x: 9.5, y: 9.5, width: 5, height: 5, rx: 0.6 }));
      [9, 12, 15].forEach((p) => {
        g.appendChild(el('line', { x1: p, y1: 1.5, x2: p, y2: 6 }));
        g.appendChild(el('line', { x1: p, y1: 18, x2: p, y2: 22.5 }));
        g.appendChild(el('line', { x1: 1.5, y1: p, x2: 6, y2: p }));
        g.appendChild(el('line', { x1: 18, y1: p, x2: 22.5, y2: p }));
      });
      break;
    case 'mem':
      g.appendChild(el('rect', { x: 2.5, y: 7, width: 19, height: 9, rx: 1 }));
      [6, 9.5, 13, 16.5, 20].forEach((x) => {
        g.appendChild(el('line', { x1: x, y1: 16, x2: x, y2: 19.5 }));
      });
      break;
    case 'disk':
      // Floppy disk (#17): body with the classic cut top-right
      // corner, a metal shutter near the top, and a label area below --
      // reads as storage media at a glance, unlike the old plain-disk glyph.
      g.appendChild(el('path', {
        d: 'M4.5 3 H15.5 L20.5 8 V19.5 A1.5 1.5 0 0 1 19 21 H5 '
          + 'A1.5 1.5 0 0 1 3.5 19.5 V4.5 A1.5 1.5 0 0 1 4.5 3 Z',
      }));
      g.appendChild(el('rect', { x: 7.5, y: 3, width: 7, height: 5.5 }));
      g.appendChild(el('rect', { x: 6.5, y: 13, width: 11, height: 6.5, rx: 0.5 }));
      break;
    case 'battery':
      g.appendChild(el('rect', { x: 1.5, y: 7, width: 17, height: 10, rx: 1.5 }));
      g.appendChild(el('rect', { x: 19.5, y: 10, width: 2.2, height: 4, rx: 0.6, fill: tone }));
      break;
    case 'net':
      g.appendChild(el('line', { x1: 8, y1: 3, x2: 8, y2: 15 }));
      g.appendChild(el('polyline', { points: '4.5,11.5 8,15 11.5,11.5' }));
      g.appendChild(el('line', { x1: 16, y1: 21, x2: 16, y2: 9 }));
      g.appendChild(el('polyline', { points: '12.5,12.5 16,9 19.5,12.5' }));
      break;
    case 'music':
      // Eighth note -- same hand-rolled stroke language as the rest of
      // System's icons, no emoji/external asset.
      g.appendChild(el('line', { x1: 15.5, y1: 3, x2: 15.5, y2: 16.5 }));
      g.appendChild(el('line', { x1: 15.5, y1: 3, x2: 20, y2: 5.5 }));
      g.appendChild(el('circle', { cx: 12, cy: 17.5, r: 3.2, fill: tone }));
      break;
    // Networked-devices row icon (#27): a small node with radiating links,
    // reads as "host on the network" at a glance, same hand-drawn language
    // as the rest of this glyph set.
    case 'device':
      g.appendChild(el('circle', { cx: 12, cy: 12, r: 3 }));
      [0, 1, 2, 3].forEach((i) => {
        const a = (i * Math.PI) / 2 + Math.PI / 4;
        g.appendChild(el('line', {
          x1: 12 + Math.cos(a) * 5, y1: 12 + Math.sin(a) * 5,
          x2: 12 + Math.cos(a) * 10, y2: 12 + Math.sin(a) * 10,
        }));
      });
      break;
    // Weather stat icons -- same hand-rolled stroke language as the System
    // icons above (no emoji, no external asset), for the Wind/Humidity/
    // Rain/UV rows and the sunrise/sunset readout (#9, Forecast
    // brought to System's icon-led row treatment).
    case 'wind':
      g.appendChild(el('path', { d: 'M2 8 H13.5 a2.75 2.75 0 1 0 -2.75 -2.75' }));
      g.appendChild(el('path', { d: 'M2 12.5 H17.5 a2.75 2.75 0 1 1 -2.75 2.75' }));
      g.appendChild(el('path', { d: 'M2 17 H10' }));
      break;
    case 'humidity':
      g.appendChild(el('path', {
        d: 'M12 2.5 C12 2.5 5 11.2 5 15.5 A7 7 0 0 0 19 15.5 C19 11.2 12 2.5 12 2.5 Z',
      }));
      break;
    case 'rain':
      g.appendChild(el('path', {
        d: 'M6 14.5 a4 4 0 0 1 0.4 -7.98 a5 5 0 0 1 9.5 -0.9 '
          + 'a3.5 3.5 0 0 1 0.6 8.88 Z',
      }));
      g.appendChild(el('line', { x1: 9, y1: 18, x2: 8, y2: 21.5 }));
      g.appendChild(el('line', { x1: 14, y1: 18, x2: 13, y2: 21.5 }));
      break;
    case 'uv':
      g.appendChild(el('circle', { cx: 12, cy: 12, r: 5 }));
      for (let i = 0; i < 8; i++) {
        const a = (i * Math.PI) / 4;
        g.appendChild(el('line', {
          x1: 12 + Math.cos(a) * 8, y1: 12 + Math.sin(a) * 8,
          x2: 12 + Math.cos(a) * 10.5, y2: 12 + Math.sin(a) * 10.5,
        }));
      }
      break;
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
    case 'schedule':
      g.appendChild(el('rect', { x: 3, y: 5, width: 18, height: 16, rx: 1.5 }));
      g.appendChild(el('line', { x1: 3, y1: 9.5, x2: 21, y2: 9.5 }));
      g.appendChild(el('line', { x1: 7.5, y1: 2.5, x2: 7.5, y2: 6.5 }));
      g.appendChild(el('line', { x1: 16.5, y1: 2.5, x2: 16.5, y2: 6.5 }));
      break;
    default: break;
  }
  return g;
}

// Same stroke-arrow language as the 'net' sysIcon, as an inline HTML string
// for embedding inside a sysRow value/sub string (Network's down/up
// throughput) instead of a plain unicode ↓/↑ glyph.
function arrowGlyph(dir) {
  const points = dir === 'up' ? '4,9 8,4 12,9' : '4,7 8,12 12,7';
  const [y1, y2] = dir === 'up' ? [4, 13] : [2, 12];
  return `<svg viewBox="0 0 16 16" class="icon-inline-sm" fill="none" stroke="currentColor"
    stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
    <polyline points="${points}"/><line x1="8" y1="${y1}" x2="8" y2="${y2}"/></svg>`;
}

// Three-stop hex interpolation (crimson -> warm -> online) used to color
// each segment of utilBars() -- a fixed spectrum across the bar's full
// length, revealed proportionally by how many segments are filled, the
// same effect as the reference racing-stat graphic (#31).
function lerpHex(a, b, t) {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  const c = pa.map((v, i) => Math.round(v + (pb[i] - v) * t));
  return `#${c.map((v) => v.toString(16).padStart(2, '0')).join('')}`;
}
function spectrumColor(t) {
  return t < 0.5 ? lerpHex('#e8615a', '#fed33f', t / 0.5)
    : lerpHex('#fed33f', '#2bfea0', (t - 0.5) / 0.5);
}

/* Angled utilization bar (#31, replacing the status LED -- user
 * feedback: "not working out"). A row of slanted parallelogram segments,
 * same idea as a car-stat readout: segments up to the filled count light up
 * in a fixed red -> gold -> green spectrum across the bar's position (not
 * tied to the metric's own percentage bands), the rest sit dim/unlit as the
 * empty track. Battery has no percentage bands to speak of either -- same
 * fixed spectrum, just fewer segments lit at low charge. */
function utilBars(pct, segments = 10) {
  const clamped = Math.max(0, Math.min(100, pct ?? 0));
  const filled = Math.round((clamped / 100) * segments);
  const svg = el('svg', { viewBox: '0 0 100 20', preserveAspectRatio: 'none',
    class: 'util-bars w-[clamp(3.2rem,8vmin,5.6rem)] h-[clamp(0.7rem,1.6vmin,0.95rem)] shrink-0 ml-auto' });
  const w = 5, gap = 2.2, skew = 2.6, h = 16, top = 2, left = 3;
  for (let i = 0; i < segments; i++) {
    const x = left + i * (w + gap);
    const points = [
      [x + skew, top], [x + skew + w, top], [x + w, top + h], [x, top + h],
    ].map((p) => p.join(',')).join(' ');
    const lit = i < filled;
    svg.appendChild(el('polygon', {
      points,
      fill: lit ? spectrumColor(i / (segments - 1)) : 'oklch(0.32 0.02 260 / .45)',
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
 * here. */
let gradientSeq = 0;
function stepChart(series, cls) {
  const svg = el('svg', { viewBox: '0 0 100 30', preserveAspectRatio: 'none',
    class: cls || 'w-[clamp(3.4rem,9vmin,6.4rem)] h-[clamp(0.8rem,1.8vmin,1.05rem)] shrink-0 ml-auto' });
  const all = series.flatMap((sr) => sr.values);
  if (all.length < 2) return svg;
  const lo = Math.min(...all), hi = Math.max(...all);
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

/* Short, fixed-width fill bar for a disk row -- deliberately not a
 * full-width bar (the previous design): a partition's percentage reads
 * fine as a small swatch next to its number. */
function miniBar(pct, tone = 'var(--color-accent)') {
  const wrap = document.createElement('div');
  wrap.className = 'w-[clamp(2.6rem,6.5vmin,4.2rem)] h-[0.5vmin] min-h-[3px] ' +
    'rounded-full overflow-hidden ml-auto';
  wrap.style.background = 'oklch(0.5 0.02 260/.22)';
  const fill = document.createElement('div');
  fill.style.cssText = `width:${Math.min(100, pct ?? 0)}%;background:${tone};` +
    'height:100%;border-radius:inherit;transition:width .6s ease';
  wrap.appendChild(fill);
  return wrap;
}

// One <tr>: icon | label | value | tail node (utilization bar, histograph,
// or mini bar). Same shape for every System row now -- CPU/MEM/BAT used to
// lead with a status LED instead of a tail column (#16, #17);
// that LED is retired (#31, "not working out") in favor of an angled
// utilization-bar graphic in the same tail slot Storage/Network already
// use, so every row shares one column layout -- a real <table>, not
// independently-sized rows.
function sysRow({ icon, iconNode, label, value, sub = '', tail = null, tone = 'var(--color-accent)' }) {
  const tr = document.createElement('tr');

  const iconTd = document.createElement('td');
  iconTd.style.color = tone;
  iconTd.appendChild(iconNode || sysIcon(icon, tone));

  const labelTd = document.createElement('td');
  labelTd.className = 'text-muted tracking-[0.1em] uppercase pl-[0.6vmin] ' +
    'text-[clamp(.44rem,.92vmin,.66rem)] truncate';
  labelTd.textContent = label;

  const valueTd = document.createElement('td');
  valueTd.className = 'num text-right text-ink/90 text-[clamp(.48rem,1.02vmin,.74rem)]';
  // Inline, not a pl-[...] utility class: .sys-table td{padding:0.32vmin 0}
  // is a class+element selector, higher specificity than a single-class
  // Tailwind utility, so it silently zeroes any pl-*/pr-* class applied to
  // a <td> here. Relying on a wide auto-sized label column to create
  // separation isn't an option either now that tables are compact (#17) --
  // the label column tracks its own content width, so a longer label like
  // "RECOVERY" would otherwise sit flush against the value with no gap.
  valueTd.style.paddingLeft = '0.9em';
  valueTd.innerHTML = sub
    ? `${value}<div class="text-faint text-[clamp(.4rem,.82vmin,.58rem)] leading-tight">${sub}</div>`
    : value;

  const tailTd = document.createElement('td');
  tailTd.className = 'pl-[0.6vmin]';
  if (tail) tailTd.appendChild(tail);
  tr.append(iconTd, labelTd, valueTd, tailTd);
  return tr;
}

// CPU/Memory/Battery value rows. Trend graphs used to render here (#17,
// #33) but were removed (#34) -- not wanted on the wallpaper. Metrics
// collection is untouched (dashd-collect, metrics.db, /api/history all still
// live) for possible later use. Each row's tail column is an angled
// utilization-bar graphic (#31, replacing the retired status LED).
function renderCpuMemBat(s) {
  const box = $('sys');
  box.replaceChildren();
  const hot = (p) => p > 88 ? 'var(--color-warm)' : 'var(--color-accent)';
  const pad2 = (n) => String(Math.round(n)).padStart(2, '0');

  const cpuTone = hot(s.cpu);
  box.appendChild(sysRow({
    icon: 'cpu', label: 'CPU', tone: cpuTone,
    value: `${pad2(s.cpu)}%`,
    sub: [s.temp_c ? `${s.temp_c}°C` : null,
      s.cpu_freq ? `${(s.cpu_freq / 1000).toFixed(1)}GHz` : null].filter(Boolean).join(' · '),
    tail: utilBars(s.cpu),
  }));

  const memTone = hot(s.mem.pct);
  box.appendChild(sysRow({
    icon: 'mem', label: 'MEM', tone: memTone,
    value: `${pad2(s.mem.pct)}%`,
    sub: `${bytes(s.mem.used)} / ${bytes(s.mem.total)}`,
    tail: utilBars(s.mem.pct),
  }));

  if (s.battery) {
    const battTone = s.battery.pct <= 35 && !s.battery.plugged
      ? 'var(--color-warm)' : 'var(--color-accent)';
    box.appendChild(sysRow({
      icon: 'battery', label: 'BAT', tone: battTone,
      value: `${pad2(s.battery.pct)}%`,
      sub: !s.battery.plugged && s.battery.secs_left ? dur(s.battery.secs_left) : '',
      tail: utilBars(s.battery.pct),
    }));
  }

  const load = s.load.map((n) => n.toFixed(2)).join('  ');
  $('sysfoot').innerHTML =
    `<div class="flex justify-between gap-[1vmin]">
       <span>up ${dur(s.uptime)} · ${s.procs} procs</span>
       <span>${s.host}</span>
     </div>
     <div class="flex justify-between gap-[1vmin] mt-[0.3vmin] opacity-70">
       <span>load ${load}</span>
     </div>`;
}

// Storage: its own dedicated area (#12), not mixed into the
// cpu/mem/bat rows above. Every partition lsblk reports a real utilization
// for, not just psutil's mounted view -- see dashd-serve's disk_tree().
function renderDisks(disks) {
  const box = $('disks');
  box.replaceChildren();
  const hot = (p) => p > 88 ? 'var(--color-warm)' : 'var(--color-accent)';
  disks.forEach((d) => {
    const label = d.mount && d.mount !== '[SWAP]'
      ? (d.mount.split('/').filter(Boolean).pop() || '/') : d.name;
    box.appendChild(sysRow({
      icon: 'disk', label: label.toUpperCase(), tone: hot(d.pct ?? 0),
      value: d.pct != null ? `${d.pct.toFixed(0)}%` : bytes(d.size),
      tail: miniBar(d.pct, hot(d.pct ?? 0)),
    }));
  });
}

// Network: dedicated area below Storage, one row, one step chart with two
// lines (download/upload). No long-term storage for this one (see
// #12) -- it's the ring buffer's fine 5s resolution that makes
// brief throughput bursts visible at all, so a DB-backed tier would just
// flatten them.
function renderNetwork(s) {
  const box = $('net');
  box.replaceChildren();
  box.appendChild(sysRow({
    icon: 'net', label: 'NET', tone: 'var(--color-accent)',
    value: `${arrowGlyph('down')}${bytes(s.net.down)}/s`,
    sub: `${arrowGlyph('up')}${bytes(s.net.up)}/s`,
    tail: stepChart([
      { values: ringSince('down', RING_WINDOW_MS), tone: 'var(--color-accent)' },
      { values: ringSince('up', RING_WINDOW_MS), tone: 'var(--color-warm)' },
    ]),
  }));
}

// Music: its own dedicated area below Network, only shown while something
// is actually playing/paused (#26). Source-agnostic -- whatever
// dashd-serve's now_playing() picked up from MPRIS, be it a local player
// or a browser tab. The section collapses entirely when idle rather than
// showing a permanent empty row.
function renderMusic(music) {
  const section = $('musicSection');
  const box = $('music');
  box.replaceChildren();
  if (!music) {
    section.classList.add('hidden');
    return;
  }
  section.classList.remove('hidden');
  const pct = music.length_secs
    ? Math.min(100, ((music.position_secs ?? 0) / music.length_secs) * 100)
    : null;
  box.appendChild(sysRow({
    icon: 'music', label: music.playing ? 'PLAYING' : 'PAUSED',
    tone: music.playing ? 'var(--color-accent)' : 'var(--color-muted)',
    value: music.title,
    sub: [music.artist, music.album].filter(Boolean).join(' · '),
    tail: pct != null ? miniBar(pct, 'var(--color-accent)') : null,
  }));
}

// Networked devices (#27): one row per host nmap's ping sweep found up,
// same icon|label|value|tail sys-table shape as everything else in System.
// scan_target/scan_interval_seconds live in config.json, editable from the
// Control Backend (#30) -- this panel just renders whatever dashd-serve's
// cached_devices() last found.
function renderDevices(devices) {
  const box = $('devices');
  box.replaceChildren();
  if (!devices || !devices.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.className = 'text-faint text-[clamp(.42rem,.85vmin,.6rem)] py-[0.2vmin]';
    td.textContent = 'no devices found';
    tr.appendChild(td);
    box.appendChild(tr);
    return;
  }
  devices.forEach((d) => {
    box.appendChild(sysRow({
      icon: 'device', label: (d.hostname || d.ip).toUpperCase(),
      tone: 'var(--color-online)',
      value: d.hostname ? d.ip : 'up',
    }));
  });
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

/* -------------------------------------------------------- weather panels */
function renderWeather(w) {
  if (w.unavailable) {
    $('wstats').replaceChildren(sysRow({
      icon: 'uv', label: 'NOW', tone: 'var(--color-warm)', value: '—',
      sub: 'weather unavailable',
    }));
    return;
  }

  // Current conditions + Wind/Humidity/Rain/UV: one sys-table of icon |
  // label | value rows, same treatment as System's CPU/MEM/BAT -- no
  // oversized glowing hero temperature/icon readout (user feedback: "not a
  // fan of the LARGE CURRENT WEATHER... more of a fan of layout and
  // organized data, not specialized 'THIS IS TO BE BROUGHT TO YOUR
  // ATTENTION' decisions").
  const nowIcon = weatherIcon(w.icon);
  nowIcon.setAttribute('class', 'sysicon');
  const rows = [
    sysRow({
      iconNode: nowIcon, label: 'NOW', tone: 'var(--color-warm)',
      value: `${w.temp}${w.units.temp}`,
      sub: `${w.desc} · feels ${w.apparent}${w.units.temp} · `
        + `H${w.high}° L${w.low}°`
        + (w.stale ? ` · ${Math.round(w.age_seconds / 60)}m old` : ''),
    }),
    sysRow({ icon: 'wind', label: 'Wind', value: `${w.wind}${w.units.wind}`, sub: compass(w.wind_dir) }),
    sysRow({ icon: 'humidity', label: 'Humidity', value: `${w.humidity}%` }),
    sysRow({ icon: 'rain', label: 'Rain', value: `${w.precip_prob ?? 0}%`, sub: 'today' }),
    sysRow({ icon: 'uv', label: 'UV', value: w.uv == null ? '—' : `${Math.round(w.uv)}`, sub: 'index' }),
  ];
  $('wstats').replaceChildren(...rows);

  renderHourly(w);
  renderWeek(w);
  renderSunMoon(w);
}

/* Compact hourly strip: time + icon + temp, no chart. Replaces a "Next 24
 * hours" sparkline that was vague and cost far more vertical space than
 * the info was worth (see #8). */
function renderHourly(w) {
  const pts = w.hourly.filter((h) => h.temp != null).slice(0, 6);
  $('hourly').replaceChildren(...pts.map((p) => {
    const cell = document.createElement('div');
    cell.className = 'flex flex-col items-center gap-[0.25vmin] flex-1 min-w-0';

    const t = document.createElement('div');
    t.className = 'text-faint num text-[clamp(.48rem,1vmin,.68rem)]';
    t.textContent = hhmm(p.time);

    const ico = document.createElement('div');
    ico.className = 'w-[clamp(1rem,2.4vmin,1.7rem)] h-[clamp(1rem,2.4vmin,1.7rem)]';
    ico.appendChild(weatherIcon(p.icon));

    const temp = document.createElement('div');
    temp.className = 'num text-ink/90 text-[clamp(.6rem,1.4vmin,1rem)]';
    temp.textContent = `${p.temp}°`;

    cell.append(t, ico, temp);
    return cell;
  }));
}

function renderWeek(w) {
  const today = new Date().toISOString().slice(0, 10);
  $('week').replaceChildren(...w.daily.slice(0, 7).map((d) => {
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

    const ico = document.createElement('span');
    ico.className = 'w-[clamp(0.85rem,2vmin,1.4rem)] h-[clamp(0.85rem,2vmin,1.4rem)] shrink-0';
    ico.appendChild(weatherIcon(d.icon));

    const range = document.createElement('span');
    range.className = 'num flex-1 text-right text-[clamp(.55rem,1.2vmin,.88rem)]';
    range.innerHTML = `<span class="text-ink/90">${d.hi}°</span>` +
      `<span class="text-faint ml-[0.7vmin]">${d.lo}°</span>`;

    row.append(label, ico, range);
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

  const grid = document.createElement('div');
  grid.className = 'grid grid-cols-7 gap-y-[0.3vmin] text-center';

  ['S', 'M', 'T', 'W', 'T', 'F', 'S'].forEach((d) => {
    const head = document.createElement('div');
    head.className = 'text-faint uppercase text-[clamp(.42rem,.85vmin,.6rem)]';
    head.textContent = d;
    grid.appendChild(head);
  });

  for (let i = 0; i < first.getDay(); i++) grid.appendChild(document.createElement('div'));

  for (let day = 1; day <= daysInMonth; day++) {
    const iso = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const isToday = day === today;

    const cell = document.createElement('div');
    cell.className = 'num flex flex-col items-center justify-center gap-[0.1vmin] ' +
      'text-[clamp(.48rem,1vmin,.7rem)] ' + (isToday ? 'text-ink font-medium' : 'text-muted');
    cell.textContent = String(day);

    if (events.has(iso)) {
      const dot = document.createElement('span');
      dot.className = 'w-[0.22em] h-[0.22em] shrink-0';
      dot.style.background = isToday ? 'var(--color-ink)' : 'var(--color-warm)';
      cell.appendChild(dot);
    }

    grid.appendChild(cell);
  }

  host.replaceChildren(title, grid);
}

// Schedule (#23): source-agnostic today's-agenda list -- reads
// state.extra.schedule, the same data/extra.json passthrough any script can
// already populate (see CLAUDE.md's Data section) without touching the
// server. Each item is {time: "HH:MM", title}; items whose time has already
// passed today render dimmed, same muted/ink treatment renderWeek() uses for
// past vs. current days. No source is wired up yet, so this renders the
// empty state until something writes extra.json.
function renderSchedule(items) {
  const box = $('schedule');
  if (!items || !items.length) {
    box.replaceChildren(sysRow({
      icon: 'schedule', label: 'TODAY', tone: 'var(--color-faint)', value: '—',
      sub: 'no scheduled items',
    }));
    return;
  }

  const nowHM = new Date().toTimeString().slice(0, 5);
  box.replaceChildren(...items.map((item) => {
    const past = item.time && item.time < nowHM;
    const row = document.createElement('div');
    row.className = 'flex items-center gap-[0.8vmin] py-[0.15vmin] ' +
      (past ? 'text-faint' : 'text-ink');

    const time = document.createElement('span');
    time.className = 'num w-[3.6em] shrink-0 text-[clamp(.5rem,1.05vmin,.78rem)]';
    time.textContent = item.time || '';

    const title = document.createElement('span');
    title.className = 'flex-1 truncate tracking-wide text-[clamp(.52rem,1.1vmin,.82rem)]';
    title.textContent = item.title || '';

    row.append(time, title);
    return row;
  }));
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

/* ------------------------------------------------------------------ tasks */
// Source-agnostic (#25): dashd-serve reads data/tasks.json verbatim, so
// this just renders whatever {"items": [{"text","done","due"}]} shape
// shows up -- no assumption about what wrote it.
function renderTasks(tasks) {
  const root = $('tasks');
  const items = Array.isArray(tasks?.items) ? tasks.items : [];
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'text-faint text-[clamp(.5rem,1.05vmin,.76rem)]';
    empty.textContent = 'No tasks';
    root.replaceChildren(empty);
    return;
  }
  root.replaceChildren(...items.map((t) => {
    const row = document.createElement('div');
    row.className = 'flex items-center gap-[0.7vmin] py-[0.15vmin] ' +
      (t.done ? 'text-faint line-through' : 'text-ink');

    const box = el('svg', { viewBox: '0 0 16 16', class: 'icon-inline shrink-0' });
    box.appendChild(el('rect', {
      x: 1, y: 1, width: 14, height: 14, rx: 2, fill: 'none',
      stroke: t.done ? 'var(--color-faint)' : 'var(--panel-accent, var(--color-accent))',
      'stroke-width': '1.4',
    }));
    if (t.done) {
      box.appendChild(el('polyline', {
        points: '4,8.5 7,11.5 12,5', fill: 'none', stroke: 'var(--color-faint)',
        'stroke-width': '1.6', 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      }));
    }

    const label = document.createElement('span');
    label.className = 'flex-1 tracking-wide text-[clamp(.52rem,1.1vmin,.82rem)] truncate';
    label.textContent = t.text || '';

    row.append(box, label);

    if (t.due) {
      const due = document.createElement('span');
      due.className = 'num text-faint text-[clamp(.48rem,1vmin,.7rem)]';
      due.textContent = t.due;
      row.append(due);
    }
    return row;
  }));
}

/* ------------------------------------------------------------------ poll */
/* ------------------------------------------------------- auto-cycle reveal
 * The desktop surface sits below every window with no pointer/keyboard
 * reach (#29) -- a panel whose content outgrows its box has no scrollbar a
 * user could ever grab. Any element marked class="auto-cycle" (with a CSS
 * height/max-height so it can actually overflow) opts into a passive
 * scroll-dwell-reset loop that reveals the rest of its content over time.
 * refreshAutoCycles() is called every apply() and is the only integration
 * point a future panel (networked devices, log highlighter, tasks, ...)
 * needs -- it doesn't touch anything that already fits its box.
 */
const autoCycles = new WeakMap();

function stopAutoCycle(el) {
  const c = autoCycles.get(el);
  if (!c) return;
  clearTimeout(c.timer);
  autoCycles.delete(el);
  el.scrollTop = 0;
}

function driveAutoCycle(el, overflow, { stepPx = 1, stepMs = 45, dwellMs = 2600 } = {}) {
  const c = { overflow, timer: null };
  autoCycles.set(el, c);
  const step = () => {
    const max = el.scrollHeight - el.clientHeight;
    if (el.scrollTop >= max - 0.5) {
      c.timer = setTimeout(() => {
        el.scrollTop = 0;
        c.timer = setTimeout(step, dwellMs);
      }, dwellMs);
      return;
    }
    el.scrollTop = Math.min(max, el.scrollTop + stepPx);
    c.timer = setTimeout(step, stepMs);
  };
  c.timer = setTimeout(step, dwellMs);
}

function refreshAutoCycles() {
  // Static snapshot renders (headless screenshot, ?static=1) and
  // prefers-reduced-motion both want a single settled frame, not a
  // perpetually running timer chain.
  const skipMotion = STATIC || matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.querySelectorAll('.auto-cycle').forEach((box) => {
    const overflow = box.scrollHeight - box.clientHeight;
    const running = autoCycles.get(box);
    if (overflow <= 2 || skipMotion) {
      if (running) stopAutoCycle(box);
      return;
    }
    // Same shape as the in-flight cycle -- let it keep going instead of
    // yanking back to the top on every ~5s poll.
    if (running && Math.abs(running.overflow - overflow) < 4) return;
    if (running) stopAutoCycle(box);
    driveAutoCycle(box, overflow);
  });
}

function apply(s) {
  state = s;

  const disp = s.config.display || {};
  const outCfg = (s.config.outputs || {})[OUTPUT] || (s.config.outputs || {}).default || {};
  document.body.classList.toggle('transparent', !!disp.transparent);
  document.documentElement.dataset.layout = outCfg.layout || 'auto';
  // Theme toggle surface: a config.json field for now (issue #32) -- a
  // natural future home is the Control Backend (#30) once it exists, but
  // this doesn't block on that. data-theme on <html> drives the CSS
  // variable overrides and the CRT fx layer in index.html.
  document.documentElement.dataset.theme = disp.theme || 'night_ops';
  const root = document.documentElement.style;
  root.setProperty('--safe-top', `${disp.safe_area_top || 0}px`);
  root.setProperty('--safe-bottom', `${disp.safe_area_bottom || 0}px`);

  pushRing(s.sys);
  renderWeather(s.weather);
  renderCpuMemBat(s.sys);
  renderDisks(s.sys.disks);
  renderNetwork(s.sys);
  renderMusic(s.music);
  renderCalendar(s.extra);
  renderTasks(s.tasks);
  renderSchedule((s.extra && s.extra.schedule) || []);
  renderDevices(s.devices);
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

// SVG geometry is computed from measured pixel sizes, so a monitor change or
// layout reflow has to redraw rather than just rescale.
let resizeTimer;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state) apply(state); }, 200);
});

poll();
