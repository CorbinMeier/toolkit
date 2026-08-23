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

// One .sys-grid row: label | value (+ status bar/chart) | sub (#57,
// standardizing System onto the same conventions Weather's .kv-grid
// settled on -- no icons, one font size everywhere, no stacked second
// line). `hot` swaps the value to the same alert-badge treatment Weather
// uses (#49) instead of the retired icon-tone signal; the bar/chart
// itself is unrelated and keeps its own independent coloring (DESIGN.md's
// Utilization Bars component is a fixed position-based spectrum, not a
// threshold). `sub` is the old second-line info (CPU temp/freq, MEM
// used/total, BAT time-remaining, NET upload rate) as a dash-prefixed
// third column instead of a stacked line under the value.
function sysRow({ label, value, hot = false, bar = null, sub = '' }) {
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

  const subEl = document.createElement('div');
  subEl.className = 'sys-sub num text-faint';
  subEl.textContent = sub ? `- ${sub}` : '';

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
  const isHot = (p) => p > 88;
  const barTone = (p) => isHot(p) ? 'var(--color-warm)' : 'var(--color-accent)';
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

// Network: dedicated area below Storage, one row, one step chart with two
// lines (download/upload). No long-term storage for this one (see
// #12) -- it's the ring buffer's fine 5s resolution that makes
// brief throughput bursts visible at all, so a DB-backed tier would just
// flatten them. No hot/alert threshold defined for throughput, so the
// value never gets the badge treatment.
function renderNetwork(s) {
  const box = $('net');
  box.replaceChildren();
  box.append(...sysRow({
    label: 'NET',
    value: `down ${bytes(s.net.down)}/s`,
    bar: stepChart([
      { values: ringSince('down', RING_WINDOW_MS), tone: 'var(--color-accent)' },
      { values: ringSince('up', RING_WINDOW_MS), tone: 'var(--color-warm)' },
    ]),
    sub: `up ${bytes(s.net.up)}/s`,
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

/* -------------------------------------------------------- weather panels */
// No icons in this card (user request, #41). Spreadsheet-style key:value
// grid (#50, .kv-grid in index.html) -- Condition/Temp/Feels/High-Low/Wind/
// Humidity/Rain/UV each get one row, key column left, value column right,
// all sharing one CSS grid so the value column lines up top to bottom
// (the old 2x2 table plus a separately-laid-out Wind/Humid/Rain/UV list
// didn't share a column grid, so their value columns started at different
// x positions). Upcoming (renderWeek) is removed from this card (still
// called, targets a hidden node -- #40's pattern for Hourly/Sun & Moon).
function renderWeather(w) {
  const descEl = $('wdesc');
  const feelsEl = $('wfeels');
  const tempEl = $('wtemp');
  const hiloEl = $('whilo');
  const windEl = $('wwind');
  const humidEl = $('whumid');
  const rainEl = $('wrain');
  const uvEl = $('wuv');
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
