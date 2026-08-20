/* Desktop dashboard renderer.
 *
 * Clock ticks locally every 250ms (never waits on the network); everything
 * else redraws from /api/state on a poll. Render is a pure function of the
 * last good state, so a failed poll just keeps the previous frame up and
 * raises the offline banner instead of blanking the wallpaper.
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

/* ------------------------------------------------------------------ clock */
// No digital time readout -- the user's system already shows it. Date stays,
// since it's information the system clock doesn't surface at a glance.
function tickClock() {
  $('date').textContent = new Date().toLocaleDateString(undefined,
    { weekday: 'long', month: 'long', day: 'numeric' });
}

/* ------------------------------------------------------------ sys panel */
function bar(label, pct, detail, tone = 'var(--color-accent)') {
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <div class="flex justify-between items-baseline mb-[0.45vmin]">
      <span class="text-muted tracking-[0.16em] uppercase
            text-[clamp(.52rem,1.1vmin,.78rem)]">${label}</span>
      <span class="num text-ink/90 text-[clamp(.55rem,1.2vmin,.85rem)]">${detail}</span>
    </div>
    <div class="h-[0.85vmin] min-h-[3px] rounded-full overflow-hidden"
         style="background:oklch(0.5 0.02 260/.22)">
      <div style="width:${Math.min(100, pct)}%;background:${tone};height:100%;
                  border-radius:inherit;transition:width .6s ease"></div>
    </div>`;
  return wrap;
}

/* Per-core load as a row of vertical bars -- reads as one texture at a
 * glance, and distinguishes "one pinned core" from "everything busy". */
function coreStrip(cores) {
  const wrap = document.createElement('div');
  wrap.className = 'flex items-end gap-[0.28vmin] h-[3.2vmin] min-h-[12px] mt-[0.3vmin]';
  cores.forEach((p) => {
    const b = document.createElement('div');
    b.className = 'flex-1 rounded-sm';
    b.style.cssText =
      `height:${Math.max(8, p)}%;` +
      `background:${p > 85 ? 'var(--color-warm)' : 'var(--color-accent)'};` +
      `opacity:${0.30 + (p / 100) * 0.7};transition:height .5s ease`;
    wrap.appendChild(b);
  });
  return wrap;
}

function renderSys(s) {
  const box = $('sys');
  box.replaceChildren();
  const hot = (p) => p > 88 ? 'var(--color-warm)' : 'var(--color-accent)';

  box.appendChild(bar('CPU', s.cpu,
    `${s.cpu.toFixed(0)}%${s.temp_c ? ` · ${s.temp_c}°C` : ''}` +
    `${s.cpu_freq ? ` · ${(s.cpu_freq / 1000).toFixed(1)}GHz` : ''}`,
    hot(s.cpu)));
  if (s.cpu_cores?.length) box.appendChild(coreStrip(s.cpu_cores));

  box.appendChild(bar('Memory', s.mem.pct,
    `${bytes(s.mem.used)} / ${bytes(s.mem.total)}`, hot(s.mem.pct)));
  if (s.swap.total > 0 && s.swap.pct > 1) box.appendChild(
    bar('Swap', s.swap.pct, `${bytes(s.swap.used)}`, hot(s.swap.pct)));

  // Every real mount, not just / -- this box has the room and the extra
  // volumes are the ones that silently fill up.
  s.disks.filter((d) => !d.mount.startsWith('/boot'))
    .forEach((d) => box.appendChild(bar(
      d.mount === '/' ? 'Disk /' : d.mount,
      d.pct, `${bytes(d.total - d.used)} free`, hot(d.pct))));

  if (s.battery) box.appendChild(bar('Battery', s.battery.pct,
    `${s.battery.pct}%${s.battery.plugged ? ' · AC' :
      s.battery.secs_left ? ` · ${dur(s.battery.secs_left)}` : ''}`,
    s.battery.pct < 20 && !s.battery.plugged
      ? 'var(--color-warm)' : 'var(--color-accent)'));

  const load = s.load.map((n) => n.toFixed(2)).join('  ');
  $('sysfoot').innerHTML =
    `<div class="flex justify-between gap-[1vmin]">
       <span>↓${bytes(s.net.down)}/s&nbsp;&nbsp;↑${bytes(s.net.up)}/s</span>
       <span>load ${load}</span>
     </div>
     <div class="flex justify-between gap-[1vmin] mt-[0.4vmin] opacity-70">
       <span>up ${dur(s.uptime)} · ${s.procs} procs</span>
       <span>${s.host}</span>
     </div>`;
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

  renderSpark(w);
  renderWeek(w);
  renderSunMoon(w);
}

function renderSpark(w) {
  const svg = $('spark');
  svg.replaceChildren();
  const pts = w.hourly.filter((h) => h.temp != null);
  if (pts.length < 2) return;

  // Match the viewBox to the real pixel box so nothing is distorted.
  const box = svg.getBoundingClientRect();
  const W = Math.max(Math.round(box.width), 160);
  const H = Math.max(Math.round(box.height), 70);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const fs = Math.max(10, Math.min(Math.round(H * 0.13), 22));

  const temps = pts.map((p) => p.temp);
  const lo = Math.min(...temps), hi = Math.max(...temps);
  const span = Math.max(hi - lo, 1);
  const topPad = fs * 1.9, botPad = fs * 1.1;
  const x = (i) => (i / (pts.length - 1)) * W;
  const y = (t) => H - botPad - ((t - lo) / span) * (H - topPad - botPad);

  // precipitation probability as background columns
  pts.forEach((p, i) => {
    if (!p.precip) return;
    svg.appendChild(el('rect', {
      x: x(i) - W / pts.length / 2, y: H - (p.precip / 100) * H,
      width: W / pts.length, height: (p.precip / 100) * H,
      fill: 'var(--color-accent)', opacity: '.13',
    }));
  });

  const line = pts.map((p, i) => `${i ? 'L' : 'M'} ${x(i).toFixed(1)} ${y(p.temp).toFixed(1)}`).join(' ');
  const grad = el('linearGradient', { id: 'tg', x1: '0', y1: '0', x2: '0', y2: '1' });
  grad.appendChild(el('stop', { offset: '0', 'stop-color': 'var(--color-warm)', 'stop-opacity': '.38' }));
  grad.appendChild(el('stop', { offset: '1', 'stop-color': 'var(--color-warm)', 'stop-opacity': '0' }));
  const defs = el('defs'); defs.appendChild(grad); svg.appendChild(defs);

  svg.appendChild(el('path', {
    d: `${line} L ${W} ${H} L 0 ${H} Z`, fill: 'url(#tg)',
  }));
  svg.appendChild(el('path', {
    d: line, fill: 'none', stroke: 'var(--color-warm)', 'stroke-width': '2',
    'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    'vector-effect': 'non-scaling-stroke',
  }));

  // label the extremes only -- more than that is unreadable at a glance
  [[temps.indexOf(hi), hi, -1], [temps.indexOf(lo), lo, 1]].forEach(
    ([i, t, dir]) => {
      svg.appendChild(el('circle', { cx: x(i), cy: y(t), r: fs * 0.22,
        fill: 'var(--color-ink)' }));
      const label = el('text', {
        x: Math.min(Math.max(x(i), fs * 1.6), W - fs * 1.6),
        y: y(t) + dir * fs * 0.9 + (dir > 0 ? fs * 0.8 : 0),
        fill: 'var(--color-ink)', 'font-size': fs, 'text-anchor': 'middle',
        'font-family': 'Fira Mono, monospace',
      });
      label.textContent = `${t}°`;
      svg.appendChild(label);
    });

  const step = Math.max(1, Math.floor(pts.length / 6));
  $('hourlabels').replaceChildren(...pts.filter((_, i) => i % step === 0)
    .map((p) => {
      const s = document.createElement('span');
      s.textContent = hhmm(p.time);
      return s;
    }));
}

function renderWeek(w) {
  const today = new Date().toISOString().slice(0, 10);
  $('week').replaceChildren(...w.daily.slice(0, 7).map((d) => {
    const isToday = d.date === today;
    const row = document.createElement('div');
    row.className = 'flex items-center gap-[1.2vmin] py-[0.5vmin] ' +
      (isToday ? 'text-ink' : 'text-muted');
    const name = isToday ? 'Today'
      : new Date(d.date + 'T12:00').toLocaleDateString(undefined, { weekday: 'short' });

    const label = document.createElement('span');
    label.className = 'w-[22%] tracking-wide text-[clamp(.6rem,1.4vmin,1.05rem)]' +
      (isToday ? ' font-medium' : '');
    label.textContent = name;

    const ico = document.createElement('span');
    ico.className = 'w-[clamp(1.1rem,2.9vmin,2.1rem)] h-[clamp(1.1rem,2.9vmin,2.1rem)] shrink-0';
    ico.appendChild(weatherIcon(d.icon));

    const range = document.createElement('span');
    range.className = 'num flex-1 text-right text-[clamp(.62rem,1.5vmin,1.1rem)]';
    range.innerHTML = `<span class="text-ink/90">${d.hi}°</span>` +
      `<span class="text-faint ml-[0.9vmin]">${d.lo}°</span>`;

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
  $('loc').textContent = s.config.location;

  const disp = s.config.display || {};
  const outCfg = (s.config.outputs || {})[OUTPUT] || (s.config.outputs || {}).default || {};
  document.body.classList.toggle('transparent', !!disp.transparent);
  document.documentElement.dataset.layout = outCfg.layout || 'auto';
  const root = document.documentElement.style;
  root.setProperty('--safe-top', `${disp.safe_area_top || 0}px`);
  root.setProperty('--safe-bottom', `${disp.safe_area_bottom || 0}px`);

  tickClock();
  renderWeather(s.weather);
  renderSys(s.sys);

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

setInterval(tickClock, 250);
tickClock();
poll();
