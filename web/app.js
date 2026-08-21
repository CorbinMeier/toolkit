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
/* Hand-rolled system-metric icons, zero external asset dependency.
 * stroke/fill are set once on the root <svg> and inherit down through
 * SVG's normal property cascade; individual shapes only override fill
 * where they're meant to read as solid, not outlined. */
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
      // Square corners (user request); width/height use miniBar()'s
      // default scale, unified with CPU/MEM/BAT's utilBars() (#42).
      tail: miniBar(d.pct, hot(d.pct ?? 0), undefined, 'rounded-none'),
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
// in case it's wanted back); title and artist/album sit in one row, title
// floated left and artist/album floated right; the progress bar spans the
// full panel width with square (not pill) corners.
function renderMusic(music) {
  const box = $('music');
  box.replaceChildren();
  if (!music) {
    const idle = document.createElement('div');
    idle.className = 'text-faint text-[clamp(.5rem,1.05vmin,.76rem)]';
    idle.textContent = 'Nothing playing';
    box.appendChild(idle);
    return;
  }

  // const status = music.playing ? 'PLAYING' : 'PAUSED';

  const pct = music.length_secs
    ? Math.min(100, ((music.position_secs ?? 0) / music.length_secs) * 100)
    : null;

  const row = document.createElement('div');
  row.className = 'flex items-baseline justify-between gap-[0.8vmin]';
  const title = document.createElement('div');
  title.className = 'text-ink tracking-wide truncate text-[clamp(.52rem,1.1vmin,.8rem)]';
  title.textContent = music.title || '';
  const artist = document.createElement('div');
  artist.className = 'text-faint tracking-wide truncate text-right shrink-0 ' +
    'max-w-[45%] text-[clamp(.46rem,.95vmin,.7rem)]';
  artist.textContent = [music.artist, music.album].filter(Boolean).join(' · ');
  row.append(title, artist);
  box.appendChild(row);

  if (pct != null) {
    box.appendChild(miniBar(pct, 'var(--color-accent)', 'w-full', 'rounded-none',
      'h-[0.5vmin] min-h-[3px]'));
  }
}

// A small glowing circle -- green/up or dim/offline -- replacing the old
// node-glyph icon (user feedback: "the icon you used here is odd").
// Reuses .dot (same status-dot CSS the offline banner already uses).
// --color-offline-dot (not --color-crimson) so a theme can dim this
// instead of recoloring it (#44) -- night_ops keeps it red like every
// other alert, Retro Terminal makes it a dark green next to "up"'s
// bright green.
function statusDot(status) {
  const dot = document.createElement('span');
  dot.className = 'dot';
  const color = status === 'up' ? 'var(--color-online)' : 'var(--color-offline-dot)';
  dot.style.background = color;
  dot.style.boxShadow = `0 0 4px ${color}`;
  return dot;
}

// Networked devices (#27): one row per remembered device -- status dot |
// identifier | device name, same sys-table shape as everything else in
// System. Devices are remembered per-network (lib/devices.py's registry,
// a list of gateways each with a list of connected devices -- user
// request: "structured data"), keyed by the default gateway's identity,
// not just whatever the last scan happened to find: a device that
// doesn't answer this round shows "offline" (red dot) instead of
// disappearing, and hopping onto a different network shows that
// network's own remembered devices, not a pile of stale entries from
// wherever the laptop was before.
//
// IPv6 is the main identifier now (user request: "I want ipv6 to be the
// main way to identify devices") -- d.ipv6 falls back to d.ip (IPv4) and
// then d.mac only when IPv6 hasn't been resolved for that device yet, so
// there's always SOMETHING to identify it by even without a name. The
// name column is d.name_override (user-settable, hand-edited into
// data/devices.json -- see lib/devices.py's merge_scan(), which never
// touches it once set) first, then the resolved d.hostname, then nothing
// -- a device with neither still gets a row (user request: "if there's
// no name, and no override, it should show on the devices list"), it
// just has a blank name cell instead of being filtered out.
//
// scan_target/scan_interval_seconds live in config.json, editable from the
// Control Backend (#30) -- this panel just renders whatever dashd-serve's
// cached_devices() last found. `scanning` (state.devices_scanning) toggles
// the header spinner while a background nmap sweep is actually running.
// `gateway` (state.devices_gateway) is the current network's identity,
// shown top-right in the header as "which profile we're connecting to"
// (user request) -- its IPv6 when known, else its IPv4, same identifier
// preference as the device rows.
function renderDevices(devices, scanning, gateway) {
  const spinner = $('devicesSpinner');
  if (spinner) spinner.classList.toggle('hidden', !scanning);

  const netLabel = $('devicesNetwork');
  if (netLabel) netLabel.textContent = gateway ? (gateway.ipv6 || gateway.ip) : '';

  // Device count in parens right after the "Devices" label (user
  // request) -- total remembered rows, up + offline alike, matching what
  // actually renders below rather than just the "up" subset.
  const count = $('devicesCount');
  if (count) count.textContent = devices && devices.length ? ` (${devices.length})` : '';

  const box = $('devices');
  box.replaceChildren();
  if (!devices || !devices.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.className = 'text-faint text-[clamp(.42rem,.85vmin,.6rem)] py-[0.2vmin]';
    td.textContent = gateway ? 'no devices found' : 'no network connection';
    tr.appendChild(td);
    box.appendChild(tr);
    return;
  }
  devices.forEach((d) => {
    box.appendChild(sysRow({
      iconNode: statusDot(d.status),
      label: d.ipv6 || d.ip || d.mac || '',
      value: d.name_override || d.hostname || '—',
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
// No icons in this card (user request, #41). A header-less two-row,
// two-column grid: row1 is description | feels-like temp, row2 is current
// temp | today's high/low. Wind/Humidity/Rain/UV drop the value column
// entirely -- one "LABEL: value" line each in #wdetails. Upcoming
// (renderWeek) is removed from this card (still called, targets a hidden
// node -- #40's pattern for Hourly/Sun & Moon).
function renderWeather(w) {
  const descEl = $('wdesc');
  const feelsEl = $('wfeels');
  const tempEl = $('wtemp');
  const hiloEl = $('whilo');
  if (w.unavailable) {
    if (descEl) descEl.textContent = 'weather unavailable';
    if (feelsEl) feelsEl.textContent = '';
    if (tempEl) tempEl.textContent = '—';
    if (hiloEl) hiloEl.textContent = '';
    $('wdetails').replaceChildren();
    return;
  }

  if (descEl) {
    descEl.textContent = w.desc
      + (w.stale ? ` · ${Math.round(w.age_seconds / 60)}m old` : '');
  }
  if (feelsEl) feelsEl.textContent = `feels ${w.apparent}${w.units.temp}`;
  if (tempEl) tempEl.textContent = `${w.temp}${w.units.temp}`;
  if (hiloEl) hiloEl.textContent = `H${w.high}° L${w.low}°`;

  const detailLine = (label, value) => {
    const div = document.createElement('div');
    div.textContent = `${label}: ${value}`;
    return div;
  };
  $('wdetails').replaceChildren(
    detailLine('Wind', `${w.wind}${w.units.wind} ${compass(w.wind_dir)}`),
    detailLine('Humid', `${w.humidity}%`),
    detailLine('Rain', `${w.precip_prob ?? 0}%`),
    detailLine('UV', w.uv == null ? '—' : `${Math.round(w.uv)}`),
  );

  renderHourly(w);
  renderWeek(w);
  renderSunMoon(w);
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
    cell.className = 'flex flex-col items-center justify-center gap-[0.1vmin]';

    // #41/#42: the border marks today, not just bolder/brighter text, so it
    // reads at a glance -- but it wraps only the number itself (fixed 2px,
    // fit-content box), not the whole grid cell, so it stays a small compact
    // square instead of stretching to the column width.
    const num = document.createElement('span');
    num.className = 'num inline-flex items-center justify-center leading-none ' +
      'text-[clamp(.48rem,1vmin,.7rem)] p-1 box-border ' +
      (isToday
        ? 'text-ink font-medium border-2 border-[var(--color-warm)]'
        : 'text-muted border-2 border-transparent');
    num.textContent = String(day);
    cell.appendChild(num);

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
  renderDevices(s.devices, s.devices_scanning, s.devices_gateway);
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
