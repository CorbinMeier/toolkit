/* Desktop dashboard renderer.
 *
 * No date/clock/location readout -- the user's own system already shows
 * all three (see ISSUES.md #5, #9). Everything redraws from /api/state on
 * a poll, plus a slower, independent poll of /api/history for the CPU/
 * Memory/Battery step-line-chart tiers (see ISSUES.md #12). Render is a
 * pure function of the last good state, so a failed poll just keeps the
 * previous frame up and raises the offline banner instead of blanking the
 * wallpaper.
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

// Rows from /api/history (SQLite-backed, ~30s resolution -- see
// lib/metrics.py), refreshed by the slow pollHistory() poll below. Feeds
// the 5-minute-and-longer step-chart tiers for CPU/Memory/Battery, and
// persists across a page reload since it's server-side. Named
// `trendHistory`, not `history` -- that's a reserved browser global
// (Window.history).
let trendHistory = [];
// A window's worth of *this browser session's* /api/state polls (default
// every 5s), independent of trendHistory. This is what makes a genuine
// "30 second" chart possible at all -- /api/history's own samples are ~30s
// apart, so a 30s window of *those* would be 0-1 points. Also backs the
// Network chart, which wants the finer resolution regardless of window
// (throughput bursts are brief; 30s-resolution DB samples would flatten
// them). Capped by wall-clock age, not point count, so it self-adjusts to
// whatever the configured poll interval actually is.
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
function historySince(key, ms) {
  const cutoff = Date.now() - ms;
  return trendHistory.filter((r) => r.ts * 1000 >= cutoff && r[key] != null)
    .map((r) => r[key]);
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
      // Floppy disk (ISSUES.md #17): body with the classic cut top-right
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
    default: break;
  }
  return g;
}

// Piecewise-linear dim -> bright ramp for the CPU/MEM LED: green's climb is
// gentle (0.12-0.40 across its whole 0-70% span), yellow accelerates
// (0.40-0.70 across 70-90%), red is steepest (0.70-1.00 across 90-100%) --
// so the LED's brightness itself telegraphs "how close to trouble", not
// just its hue. Continuous across band boundaries (only the slope changes,
// never a jump), same underlying idea as #16's straight pct/100 mapping.
function ledGlow(pct) {
  const p = Math.max(0, Math.min(100, pct));
  if (p >= 90) return 0.70 + (p - 90) / 10 * 0.30;
  if (p >= 70) return 0.40 + (p - 70) / 20 * 0.30;
  return 0.12 + p / 70 * 0.28;
}

/* Small status LED for the CPU/MEM/BAT rows (ISSUES.md #16, #17 -- an
 * earlier version wrapped the icon in a much larger background ring, which
 * read as too heavy; a real LED doesn't grow, it brightens). Sized in em
 * so it's always close to the row's own text height. A fixed deep base
 * disc (.icon-led, CSS) sits under a second disc (.icon-led__fill) whose
 * opacity and glow both scale with --led-glow.
 *
 * CPU/MEM are traffic-light banded off their percentage -- green (dim,
 * good) below 70%, yellow (brightening, approaching limits) 70-89%, red
 * (bright, danger) from 90%, flashing on top of red from 95%. Battery
 * ignores pct entirely and instead reflects a discrete charge state (see
 * renderCpuMemBat) via ringState, since "charging" isn't something a glow
 * level alone can express -- its red-only charging/dim/low-flash states
 * are unchanged from #16.
 */
function metricLed({ pct = null, ringState = null } = {}) {
  const dot = document.createElement('span');
  const fill = document.createElement('span');
  fill.className = 'icon-led__fill';
  if (ringState) {
    const flash = ringState === 'low';
    // low is the one state that stays the alarm red; charging/dim both
    // read as green (on mains vs. running fine unplugged).
    const colorClass = ringState === 'low' ? '' : ' icon-led--green';
    dot.className = `icon-led icon-led--batt-${ringState}${colorClass}${flash ? ' icon-led--flash' : ''}`;
  } else {
    const clamped = Math.max(0, Math.min(100, pct ?? 0));
    // red is the fill's default color (below) -- only green/yellow need an
    // override class, so the 90%+ band adds nothing extra here.
    const colorClass = clamped >= 90 ? '' : clamped >= 70 ? ' icon-led--yellow' : ' icon-led--green';
    const flash = clamped >= 95;
    dot.className = `icon-led${colorClass}${flash ? ' icon-led--flash' : ''}`;
    fill.style.setProperty('--led-glow', ledGlow(clamped).toFixed(2));
  }
  dot.appendChild(fill);
  return dot;
}

/* Step line chart -- compact enough to sit on a single line next to text
 * (a tier label, in sysTierRow() below). "Step" rather than a diagonal
 * line: each sample holds its value until the next one arrives (a
 * staircase), which reads truer for a polled metric than interpolating
 * between two readings that were never actually in between. Accepts one
 * or more {values, tone, fill} series sharing one x/y scale, so the
 * Network chart can plot download and upload as two lines without them
 * fighting over independent scales. `fill: true` (used by graphCell() for
 * CPU/MEM, ISSUES.md #17 revised) additionally fills the area under the
 * step line with a gradient fading to transparent -- more visual presence
 * at wallpaper viewing distance than a bare stroke, without adding any
 * more information than the line already carries. Deliberately
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

// One <tr>. Two shapes, chosen by whether `led` is passed:
//  - CPU/MEM/BAT: LED | icon | label | value -- LED leads the row, the
//    most immediate signal (ISSUES.md #17; it used to trail the label).
//  - Storage/Network: icon | label | value | tail node (histograph or mini
//    bar), unchanged since #12.
// Either way this is what keeps every row's columns aligned -- a real
// <table>, not independently-sized rows.
function sysRow({ icon, label, value, sub = '', tail = null, tone = 'var(--color-accent)', led = null }) {
  const tr = document.createElement('tr');

  const iconTd = document.createElement('td');
  iconTd.style.color = tone;
  iconTd.appendChild(sysIcon(icon, tone));

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

  if (led) {
    const ledTd = document.createElement('td');
    ledTd.style.paddingRight = '0.5em'; // same specificity issue as valueTd above
    ledTd.appendChild(metricLed(led));
    tr.append(ledTd, iconTd, labelTd, valueTd);
  } else {
    const tailTd = document.createElement('td');
    tailTd.className = 'pl-[0.6vmin]';
    if (tail) tailTd.appendChild(tail);
    tr.append(iconTd, labelTd, valueTd, tailTd);
  }
  return tr;
}

// One trend graph: window label top-left, its value range top-right --
// decodes the step chart's otherwise-unlabeled auto-scaled y-axis, so a
// standalone label column isn't needed. Filled (ISSUES.md #17 revised,
// replacing the earlier three-tier row) so the trend reads with more
// visual presence at a glance than a bare line.
function graphCell(label, values, tone) {
  const wrap = document.createElement('div');
  wrap.className = 'graph-cell';
  const labels = document.createElement('div');
  labels.className = 'graph-cell__labels';
  const left = document.createElement('span');
  left.textContent = label;
  const right = document.createElement('span');
  if (values.length) {
    const lo = Math.min(...values), hi = Math.max(...values);
    right.textContent = Math.round(lo) === Math.round(hi)
      ? `${Math.round(lo)}` : `${Math.round(lo)}–${Math.round(hi)}`;
  }
  labels.append(left, right);
  wrap.append(labels, stepChart([{ values, tone, fill: true }], 'graph-cell__chart'));
  return wrap;
}

// One full-width trend graph beneath a CPU/MEM row (ISSUES.md #17 revised
// -- was three side-by-side 30S/5M/30M tiers, collapsed to a single
// 30-minute window per user feedback: three independently-auto-scaled
// mini charts made magnitude impossible to compare across tiers and the
// 30S tier was near-flat dead space at this size). Spans under the
// label+value columns (colspan 2 each) -- the LED+icon columns stay
// empty, same indent as where the metric's own icon sits in the row
// above.
function metricGraphRow(label, values, tone) {
  const tr = document.createElement('tr');
  const spacer = document.createElement('td');
  spacer.colSpan = 2;
  const cell = document.createElement('td');
  cell.colSpan = 2;
  const row = document.createElement('div');
  row.className = 'graph-row';
  row.appendChild(graphCell(label, values, tone));
  cell.appendChild(row);
  tr.append(spacer, cell);
  return tr;
}

// CPU/Memory: one filled trend graph each, the last 30 minutes
// (SQLite-backed /api/history, which survives a page reload) -- revised
// from #17's original 30S/5M/30M three-tier row per user feedback (the
// tiers weren't independently comparable and 30S was near-flat dead
// space at this size). Battery has no graph at all (#17 -- not needed);
// its LED-driven charge state is signal enough.
function renderCpuMemBat(s) {
  const box = $('sys');
  box.replaceChildren();
  const hot = (p) => p > 88 ? 'var(--color-warm)' : 'var(--color-accent)';

  const cpuTone = hot(s.cpu);
  box.appendChild(sysRow({
    icon: 'cpu', label: 'CPU', tone: cpuTone,
    value: `${s.cpu.toFixed(0)}%`,
    sub: [s.temp_c ? `${s.temp_c}°C` : null,
      s.cpu_freq ? `${(s.cpu_freq / 1000).toFixed(1)}GHz` : null].filter(Boolean).join(' · '),
    led: { pct: s.cpu },
  }));
  box.appendChild(metricGraphRow('30M', historySince('cpu_pct', 30 * 60_000), cpuTone));

  const memTone = hot(s.mem.pct);
  box.appendChild(sysRow({
    icon: 'mem', label: 'MEM', tone: memTone,
    value: `${s.mem.pct.toFixed(0)}%`,
    sub: `${bytes(s.mem.used)} / ${bytes(s.mem.total)}`,
    led: { pct: s.mem.pct },
  }));
  box.appendChild(metricGraphRow('30M', historySince('mem_pct', 30 * 60_000), memTone));

  if (s.battery) {
    const battTone = s.battery.pct <= 35 && !s.battery.plugged
      ? 'var(--color-warm)' : 'var(--color-accent)';
    // Battery's LED ignores pct and reflects a discrete charge state
    // instead -- "charging" isn't a glow level. Priority: actually
    // charging beats a low-battery warning (a low battery that's plugged
    // in and recovering isn't the emergency a flashing light implies).
    // charging/dim both read as green (on mains vs. running fine on its
    // own); low is the one state that stays red and flashes (ISSUES.md
    // #18 -- red used to mean "plugged in", which read backwards).
    const ringState = s.battery.charging ? 'charging'
      : s.battery.pct <= 35 ? 'low' : 'dim';
    box.appendChild(sysRow({
      icon: 'battery', label: 'BAT', tone: battTone,
      value: `${s.battery.pct}%`,
      sub: !s.battery.plugged && s.battery.secs_left ? dur(s.battery.secs_left) : '',
      led: { ringState },
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

// Storage: its own dedicated area (ISSUES.md #12), not mixed into the
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
// ISSUES.md #12) -- it's the ring buffer's fine 5s resolution that makes
// brief throughput bursts visible at all, so a DB-backed tier would just
// flatten them.
function renderNetwork(s) {
  const box = $('net');
  box.replaceChildren();
  box.appendChild(sysRow({
    icon: 'net', label: 'NET', tone: 'var(--color-accent)',
    value: `↓${bytes(s.net.down)}/s`,
    sub: `↑${bytes(s.net.up)}/s`,
    tail: stepChart([
      { values: ringSince('down', RING_WINDOW_MS), tone: 'var(--color-accent)' },
      { values: ringSince('up', RING_WINDOW_MS), tone: 'var(--color-warm)' },
    ]),
  }));
}

/* -------------------------------------------------------- weather panels */
function renderWeather(w) {
  if (w.unavailable) { $('wdesc').textContent = 'weather unavailable'; return; }

  $('wicon').replaceChildren(weatherIcon(w.icon));
  $('temp').textContent = w.temp;
  $('tempunit').textContent = w.units.temp;
  $('wdesc').textContent = w.desc;
  $('wfeels').textContent =
    `feels ${w.apparent}${w.units.temp}   ·   H ${w.high}°  L ${w.low}°` +
    (w.stale ? `   ·   ${Math.round(w.age_seconds / 60)}m old` : '');

  const cells = [
    ['Wind', `${w.wind}`, `${w.units.wind} ${compass(w.wind_dir)}`],
    ['Humidity', `${w.humidity}`, '%'],
    ['Rain', `${w.precip_prob ?? 0}`, '% today'],
    ['UV', w.uv == null ? '—' : `${Math.round(w.uv)}`, 'index'],
  ];
  $('wstats').replaceChildren(...cells.map(([k, v, u]) => {
    const d = document.createElement('div');
    d.innerHTML = `
      <div class="text-faint uppercase tracking-[0.18em]
           text-[clamp(.5rem,1.05vmin,.72rem)]">${k}</div>
      <div class="num text-ink/95 text-[clamp(.9rem,2.3vmin,1.7rem)]
           font-light leading-tight">${v}<span
           class="text-faint text-[clamp(.5rem,1.05vmin,.75rem)] ml-1">${u}</span></div>`;
    return d;
  }));

  renderHourly(w);
  renderWeek(w);
  renderSunMoon(w);
}

/* Compact hourly strip: time + icon + temp, no chart. Replaces a "Next 24
 * hours" sparkline that was vague and cost far more vertical space than
 * the info was worth (see ISSUES.md #8). */
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

  $('suntimes').innerHTML =
    `<span class="text-faint">↑</span> ${hhmm(w.sun.sunrise)}` +
    `<span class="text-faint ml-[1.4vmin]">↓</span> ${hhmm(w.sun.sunset)}`;

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
function apply(s) {
  state = s;

  const disp = s.config.display || {};
  const outCfg = (s.config.outputs || {})[OUTPUT] || (s.config.outputs || {}).default || {};
  document.body.classList.toggle('transparent', !!disp.transparent);
  document.documentElement.dataset.layout = outCfg.layout || 'auto';
  const root = document.documentElement.style;
  root.setProperty('--safe-top', `${disp.safe_area_top || 0}px`);
  root.setProperty('--safe-bottom', `${disp.safe_area_bottom || 0}px`);

  historyHours = s.config.metrics_retain_hours || historyHours;
  pushRing(s.sys);
  renderWeather(s.weather);
  renderCpuMemBat(s.sys);
  renderDisks(s.sys.disks);
  renderNetwork(s.sys);

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

// Separate, slower poll for the 5-minute-and-longer step-chart tiers --
// history.db samples every ~30s (config.refresh.metrics_sample_seconds),
// so polling it on the same 5s cadence as /api/state would be pure waste.
// Fetches the *entire* retention window in one call and lets
// historySince() slice it per tier client-side, rather than one fetch per
// tier -- cheap on loopback, and keeps this to a single request. Failure
// just keeps showing the last-known history, same philosophy as poll().
let historyHours = 24; // refined from state.config.metrics_retain_hours in apply()
const HISTORY_POLL_MS = 60_000;
async function pollHistory() {
  try {
    const r = await fetch(`/api/history?hours=${historyHours}`, { cache: 'no-store' });
    if (r.ok) ({ samples: trendHistory } = await r.json());
  } catch { /* keep the last-known history */ }
  finally { setTimeout(pollHistory, HISTORY_POLL_MS); }
}

// SVG geometry is computed from measured pixel sizes, so a monitor change or
// layout reflow has to redraw rather than just rescale.
let resizeTimer;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state) apply(state); }, 200);
});

poll();
pollHistory();
