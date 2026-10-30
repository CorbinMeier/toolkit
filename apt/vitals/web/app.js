// vitals dashboard page (v8: compact). Plain JS, no build step; charts by uPlot (vendored). Data comes from
// /vitals/stream (Server-Sent Events): `history` once per connection, then `sample` every tick and
// `tokens` every 30 s. Settings live in this device's localStorage only (spec 14.5).
(function () {
  'use strict';

  const IDS = ['cpu', 'memory', 'disk', 'network', 'tokens', 'claude'];
  const TITLES = { cpu: 'CPU', memory: 'Memory', disk: 'Disk', network: 'Network', tokens: 'Tokens', claude: 'Claude' };
  const NET_CAP = 12;                  // safety net: more active interfaces than this get a `+N more` cell
  const KEY2 = 'vitals.settings.v2', KEY1 = 'vitals.settings.v1';
  const CH_DEF = { compact: 56, comfortable: 120 }, CH_MIN = 24, CH_MAX = 400, CH_STEP = 8;
  const AUTO_MIN = 40, AUTO_MAX = 160;   // compact chart height `auto`: the PLOT is clamped to this range; the box adds the 16 px x axis row
  const AUTO_W = { cpu: 1, memory: 0.8, disk: 1, network: 1, tokens: 1 };   // share of the spare height
  const $ = function (s) { return document.querySelector(s); };
  function mk(tag, cls) { const e = document.createElement(tag); if (cls) e.className = cls; return e; }

  // ---- storage (every access guarded: it can throw or come back empty) ----
  const store = {
    get: function (k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* private window */ } },
    del: function (k) { try { window.localStorage.removeItem(k); } catch (e) { /* ignore */ } }
  };
  function parseObj(s) {
    try { const o = JSON.parse(s || 'null'); return o && typeof o === 'object' && !Array.isArray(o) ? o : null; } catch (e) { return null; }
  }
  // v2 settings: {density, layout, rows, order, sec: {id: {show, graph, bars, summary, h}}, win, twin, theme, idle, netAll}.
  // v1 (before v8) had {show: {id: bool}, order, cols, win, twin, theme, netAll}; the values that map are carried over.
  function loadSettings() {
    const v2 = parseObj(store.get(KEY2));
    if (v2) return v2;
    const v1 = parseObj(store.get(KEY1));
    if (!v1) return {};
    const s = {};
    if (v1.show && typeof v1.show === 'object') {
      s.sec = {};
      Object.keys(v1.show).forEach(function (id) { if (IDS.indexOf(id) >= 0) s.sec[id] = { show: !!v1.show[id] }; });
    }
    if (Array.isArray(v1.order)) s.order = v1.order.filter(function (i) { return IDS.indexOf(i) >= 0; });
    if (v1.win) s.win = +v1.win;
    if (v1.twin) s.twin = String(v1.twin);
    if (v1.theme) s.theme = String(v1.theme);
    if (v1.netAll) s.netAll = true;
    store.set(KEY2, JSON.stringify(s));
    return s;
  }
  let S = loadSettings();
  function saveSettings() { store.set(KEY2, JSON.stringify(S)); }
  function density() { return S.density === 'comfortable' ? 'comfortable' : 'compact'; }
  function layoutMode() { return S.layout === 'rows' || S.layout === 'custom' ? S.layout : 'stacked'; }
  function sec(id) { return (S.sec && S.sec[id]) || {}; }
  function setSec(id, k, v) { S.sec = S.sec || {}; S.sec[id] = S.sec[id] || {}; S.sec[id][k] = v; saveSettings(); }

  // ---- state ----
  let server = null;          // the history event's meta: layout, theme, interval_ms, tls
  let P = [];                 // history points {t, cpu, th, mem, dr, dw, busy, nd, nu, if}
  let tokens = { min: [], hour: [] };
  let last = null;            // the newest sample (same object as /vitals)
  let intervalMs = 1000;
  const cards = {};             // id -> card
  const charts = [];            // every uPlot wrapper, for theme rebuilds
  let zoomed = null;
  let es = null, touched = 0, lastProbe = 0;

  // ---- formatting ----
  function fmtBytes(n) {
    if (n == null || isNaN(n)) return '-';
    let u = ['B', 'K', 'M', 'G', 'T'], i = 0, v = Math.abs(n);
    while (v >= 1024 && i < 4) { v /= 1024; i++; }
    return (n < 0 ? '-' : '') + (v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)) + u[i];
  }
  function fmtRate(n) { return fmtBytes(n) + '/s'; }
  function fmtTok(n) {
    if (n == null) return '-';
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'G';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(Math.round(n));
  }
  function pct(x, d) { return x == null ? '-' : x.toFixed(d || 0) + '%'; }
  function fmtLeft(ms) {
    let m = Math.max(0, Math.round(ms / 60000)), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60);
    return d > 0 ? d + 'd ' + h + 'h' : h > 0 ? h + 'h ' + (m % 60) + 'm' : (m % 60) + 'm';
  }
  function clock(ms) { return new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }

  // ---- colors ----
  function hexRgb(h) {
    let m = /^#?([0-9a-f]{6})$/i.exec(h || '');
    if (!m) return null;
    let n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function rgba(h, a) { let c = hexRgb(h) || [128, 128, 128]; return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')'; }
  function mixHex(a, b, t) {
    let x = hexRgb(a), y = hexRgb(b);
    if (!x || !y) return a || b;
    let c = [0, 1, 2].map(function (i) { return Math.round(x[i] + (y[i] - x[i]) * t); });
    return '#' + c.map(function (v) { return ('0' + v.toString(16)).slice(-2); }).join('');
  }
  const FALLBACK = {
    cpu: ['#4cc95e', '#e5c03a', '#e54848'], memory: ['#2ab5a5', '#6a8bd0', '#9b6be0'],
    cache: ['#2c4654', '#44687a', '#5b8fa8'], disk_read: ['#174e58', '#2b8a9a', '#3ad6f0'],
    disk_write: ['#42590f', '#74962a', '#a6e22e'], net_down: ['#1d3766', '#3262b3', '#4a8aff'],
    net_up: ['#5a205a', '#9a3a9a', '#e050e0'], tokens_in: ['#5d4839', '#9b7a5f', '#e8b48f'],
    tokens_out: ['#57301f', '#98543d', '#D97757'], tokens_cache: ['#37241b', '#6c4a36', '#b07a5c'],
    claude: ['#57301f', '#98543d', '#D97757']
  };
  function stops(key) {
    let s = server && server.theme && server.theme.series && server.theme.series[key];
    let f = FALLBACK[key];
    return [0, 1, 2].map(function (i) { return (s && s[i]) || f[i]; });
  }
  function ramp(key, p) {
    let s = stops(key), t = Math.min(1, Math.max(0, p / 100));
    return t < 0.5 ? mixHex(s[0], s[1], t * 2) : mixHex(s[1], s[2], (t - 0.5) * 2);
  }
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

  function applyDensity() { document.documentElement.setAttribute('data-density', density()); }
  applyDensity();

  function applyTheme() {
    let mode = S.theme || 'server', root = document.documentElement;
    root.setAttribute('data-theme', mode);
    ['--fg', '--muted', '--accent', '--danger', '--claude', '--line'].forEach(function (v) { root.style.removeProperty(v); });
    if (mode === 'server' && server && server.theme) {
      let t = server.theme;
      if (t.title) root.style.setProperty('--fg', t.title);
      if (t.muted) root.style.setProperty('--muted', t.muted);
      if (t.accent) root.style.setProperty('--accent', t.accent);
      if (t.danger) root.style.setProperty('--danger', t.danger);
      if (t.claude) root.style.setProperty('--claude', t.claude);
      if (t.border) root.style.setProperty('--line', rgba(t.border, 0.35));
    }
    let meta = document.querySelector('meta[name=theme-color]');
    if (meta) meta.setAttribute('content', cssVar('--bg') || '#10131a');
    rebuildCharts();
  }
  function rebuildCharts() {
    charts.forEach(function (c) { c.destroy(); });
    if (last) render(true);
  }

  // ---- charts (uPlot): stepped lines, 1px, flat low-opacity fill, no smoothing ----
  function windowSec(spec) { return spec.tokens ? tokSpan() / 1000 : (S.win || 5) * 60; }

  // One-line x axis labels: time of day for windows under two days, the date beyond.
  function axisLabel(ms, spanSec) {
    const d = new Date(ms), p2 = function (n) { return (n < 10 ? '0' : '') + n; };
    if (spanSec > 2 * 86400) return (d.getMonth() + 1) + '/' + d.getDate();
    return p2(d.getHours()) + ':' + p2(d.getMinutes()) + (spanSec <= 120 ? ':' + p2(d.getSeconds()) : '');
  }
  // Tick increments that match the window: seconds only up to 2 minutes, whole minutes above, whole days beyond two days.
  const INCRS_SEC = [1, 2, 5, 10, 15, 30, 60, 120];
  const INCRS_MIN = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
  const INCRS_DAY = [86400, 172800, 604800, 2592000];
  function axisIncrs(spanSec) { return spanSec <= 120 ? INCRS_SEC : spanSec > 2 * 86400 ? INCRS_DAY : INCRS_MIN; }
  // Labels for the ticks `vals` (seconds): a label equal to the previous one becomes ''.
  function axisLabels(vals, spanSec) {
    let prev = null;
    return vals.map(function (v) {
      const l = axisLabel(v * 1000, spanSec);
      if (l === prev) return '';
      prev = l;
      return l;
    });
  }
  // The chart scale's maximum: 100 for a percent chart, else the biggest magnitude (at least the floor).
  function scaleMax(spec, cols) {
    if (spec.kind === 'pct') return 100;
    let m = spec.floor || 1;
    (cols || []).slice(1).forEach(function (a) { (a || []).forEach(function (v) { if (v != null && Math.abs(v) > m) m = Math.abs(v); }); });
    return m;
  }
  function resizeCharts() { charts.forEach(function (c) { c.resize(); }); }

  // compact x labels (v8.4): the chart box = plot + the x axis row (16 px, reserved outside the plot). The auto layout
  // gives every box at least AUTO_MIN + 16, so its labels are never dropped; only a manual box shorter than that hides them.
  const X_AXIS = 16, PLOT_MIN = 40;
  function xLabelsOn(w, h) {
    if (density() !== 'compact') return true;
    return h >= PLOT_MIN + X_AXIS;
  }
  // spec: {kind: 'pct' | 'mirror', floor, fmt, tokens, series: [{label, key, neg}]}
  function makeChart(host, spec) {
    let o = { host: host, spec: spec, u: null, data: null, ro: null, max: scaleMax(spec, null), maxEl: null, span: 0 };
    function create() {
      let w = host.clientWidth, h = host.clientHeight;
      if (w < 30 || h < 20 || !window.uPlot) return;
      let dense = density() === 'compact';
      let muted = cssVar('--muted') || '#8a90a0', grid = cssVar('--line') || '#2a3140';
      let axisFont = dense ? '10px ui-monospace, Menlo, Consolas, monospace' : '11px system-ui, sans-serif';
      let paths = window.uPlot.paths && window.uPlot.paths.stepped ? window.uPlot.paths.stepped({ align: 1 }) : undefined;
      let series = [{}].concat(spec.series.map(function (s) {
        let c = stops(s.key)[2];
        return {
          label: s.label, stroke: c, width: 1, points: { show: false }, paths: paths,
          fill: rgba(c, 0.28), fillTo: function () { return 0; }
        };
      }));
      // x axis: increments follow the window (whole minutes above 2 min), at most ~6 labels in compact, a repeated
      // label is blanked. Compact has no y axis (the scale's max is the `cmax` label); comfortable keeps one.
      o.span = windowSec(spec);
      let maxLabels = dense ? 6 : 10;
      // a compact box too short for a 40 px plot plus the axis row drops the x labels
      o.xOn = xLabelsOn(w, h);
      let axes = [{
        show: o.xOn, stroke: muted, font: axisFont, grid: { stroke: grid, width: 1 }, ticks: { show: false }, size: dense ? X_AXIS : 22, gap: dense ? 2 : 5,
        space: function (u, i, min, max, dim) { return Math.max(dense ? 44 : 60, (dim || w) / maxLabels); },
        incrs: axisIncrs(o.span),
        values: function (u, vals) { return axisLabels(vals, o.span); }
      }];
      // uPlot fills a missing second axis with a default visible y axis (black tick labels), so compact says
      // explicitly: y axis `show: false`. (One y scale only, even for mirrored charts.)
      axes.push(dense ? { scale: 'y', show: false } : {
        scale: 'y', stroke: muted, font: axisFont, grid: { stroke: grid, width: 1 }, ticks: { show: false }, size: 52, gap: 5,
        values: function (u, vals) { return vals.map(function (v) { return spec.fmt(Math.abs(v)); }); }
      });
      o.u = new window.uPlot({
        // comfortable: 12 px of top padding so the top y label (centered on the tick) is not clipped
        width: w, height: h, padding: [dense ? 3 : 12, dense ? 4 : 8, 0, 0], legend: { show: false },
        cursor: { show: false }, select: { show: false },
        scales: {
          x: {
            time: true,
            range: function (u, min, max) {
              let hi = max == null ? Date.now() / 1000 : max;
              return [hi - windowSec(spec), hi];
            }
          },
          y: { range: function () { return spec.kind === 'pct' ? [0, 100] : [-o.max, o.max]; } }
        },
        axes: axes,
        series: series
      }, o.data || emptyData(spec), host);
      if (dense) {
        if (!o.maxEl) { o.maxEl = document.createElement('span'); o.maxEl.className = 'cmax'; }
        host.appendChild(o.maxEl);
        showMax();
      }
    }
    function showMax() { if (o.maxEl) o.maxEl.textContent = spec.fmt(o.max); }
    o.set = function (cols) {
      o.data = cols;
      o.max = scaleMax(spec, cols);
      if (!o.u) create();
      if (o.u) { o.u.setData(cols); showMax(); }
    };
    o.resize = function () {
      if (!o.u) { create(); if (o.u) o.u.setData(o.data || emptyData(spec)); return; }
      const w = host.clientWidth, h = host.clientHeight;
      if (w > 30 && h > 20) {
        if (xLabelsOn(w, h) !== o.xOn) { o.destroy(); create(); if (o.u) o.u.setData(o.data || emptyData(spec)); return; }
        o.u.setSize({ width: w, height: h });
      }
    };
    o.destroy = function () {
      if (o.u) { o.u.destroy(); o.u = null; }
      if (o.maxEl) { o.maxEl.remove(); o.maxEl = null; }
    };
    if (window.ResizeObserver) {
      o.ro = new ResizeObserver(function () { o.resize(); });
      o.ro.observe(host);
    }
    charts.push(o);
    return o;
  }
  function emptyData(spec) { let d = [[]]; spec.series.forEach(function () { d.push([]); }); return d; }

  // ---- bar components (hard-edged, no transitions: sizes change instantly) ----
  // Horizontal thin meters. One column layout for every row of a card: label width and value width in ch, fixed per
  // card (CSS grid tracks read --lbw and --vw), so a bar never changes width when its number does.
  // `lbw`/`vw` (ch) optionally give this group its own fixed column widths (Disk: mounts and I/O sit on one line).
  function Bars(parent, cls, lbw, vw) {
    this.host = mk('div', 'hb ' + (cls || '')); parent.appendChild(this.host); this.rows = new Map();
    if (lbw) { this.host.style.setProperty('--lbw', lbw + 'ch'); this.host.style.setProperty('--vw', vw + 'ch'); }
  }
  Bars.prototype.labelCh = function (ch) { this.host.style.setProperty('--lbw', ch + 'ch'); };
  Bars.prototype.set = function (list) {
    let seen = {}, self = this;
    list.forEach(function (it, i) {
      seen[it.key] = true;
      let r = self.rows.get(it.key);
      if (!r) {
        let el = mk('div', 'bar'), lb = mk('span', 'lb'), tr = mk('div', 'track'), fill = mk('i', 'fill'), mark = mk('b', 'mark'), val = mk('span', 'val');
        tr.appendChild(fill); tr.appendChild(mark);
        el.appendChild(lb); el.appendChild(tr); el.appendChild(val);
        r = { el: el, lb: lb, tr: tr, fill: fill, mark: mark, val: val, days: null };
        self.rows.set(it.key, r);
      }
      if (self.host.children[i] !== r.el) self.host.insertBefore(r.el, self.host.children[i] || null);
      r.lb.textContent = it.label; r.lb.title = it.title || it.label;
      r.fill.style.width = Math.min(100, Math.max(0, it.pct || 0)) + '%';
      r.fill.style.backgroundColor = it.color || '';
      if (it.mark == null) { r.mark.style.display = 'none'; }
      else { r.mark.style.display = 'block'; r.mark.style.left = 'calc(' + Math.min(100, Math.max(0, it.mark)) + '% - 1px)'; }
      r.val.textContent = it.text || '';
      self.days(r, it.days);
    });
    this.rows.forEach(function (r, k) { if (!seen[k]) { r.el.remove(); self.rows.delete(k); } });
    this.host.hidden = !list.length;
  };

  // Day notches (v8.3): `days` = {today: 0..6 or -1} on a week row, null elsewhere. Six 1px notches at k/7 of the bar
  // (the bar is the 7-day window, so the day boundaries fall there for any reset time) and today's segment lighter.
  // Built once per row; only the today segment moves (at a day boundary).
  Bars.prototype.days = function (r, d) {
    if (!d) { if (r.days) { r.days.els.forEach(function (e) { e.remove(); }); r.days = null; r.tr.classList.remove('days'); } return; }
    if (!r.days) {
      let els = [], today = mk('i', 'today');
      r.tr.insertBefore(today, r.fill); els.push(today);
      for (let k = 1; k <= 6; k++) { let n = mk('i', 'notch'); n.style.left = (k * 100 / 7) + '%'; r.tr.appendChild(n); els.push(n); }
      r.days = { els: els, today: today, idx: -2 }; r.tr.classList.add('days');
    }
    if (r.days.idx !== d.today) {
      r.days.idx = d.today;
      if (d.today < 0) r.days.today.style.display = 'none';
      else { r.days.today.style.display = 'block'; r.days.today.style.left = (d.today * 100 / 7) + '%'; r.days.today.style.width = (100 / 7) + '%'; }
    }
  };

  // Vertical columns. kind 'col': one column, height = pct. kind 'pair': a mirrored pair around a midline (up above,
  // down below). kind 'more': a button cell. Column widths are equal (flex) or fixed (CSS), the label sits under it.
  function VCols(parent, cls) { this.host = mk('div', 'vcols ' + (cls || '')); parent.appendChild(this.host); this.cols = new Map(); }
  VCols.prototype.set = function (list) {
    let seen = {}, self = this;
    list.forEach(function (it, i) {
      seen[it.key] = true;
      let r = self.cols.get(it.key);
      if (!r) {
        let el = mk('div', 'vc');
        r = { el: el };
        if (it.kind === 'more') {
          el.className = 'vc morecell';
          let b = mk('button', 'more'); b.type = 'button';
          b.addEventListener('click', function () { if (r.onclick) r.onclick(); });
          el.appendChild(b); r.btn = b;
        } else if (it.kind === 'pair') {
          let up = mk('div', 'vt up'), dn = mk('div', 'vt dn'), uf = mk('i', 'vf'), df = mk('i', 'vf');
          up.appendChild(uf); dn.appendChild(df);
          r.lb = mk('span', 'vl'); r.vv = mk('span', 'vv');
          el.appendChild(up); el.appendChild(dn); el.appendChild(r.lb); el.appendChild(r.vv);
          r.uf = uf; r.df = df;
        } else {
          let tr = mk('div', 'vt'), f = mk('i', 'vf');
          tr.appendChild(f);
          r.lb = mk('span', 'vl'); r.vv = mk('span', 'vv');
          el.appendChild(tr); el.appendChild(r.lb); el.appendChild(r.vv);
          r.f = f;
        }
        self.cols.set(it.key, r);
      }
      if (self.host.children[i] !== r.el) self.host.insertBefore(r.el, self.host.children[i] || null);
      r.el.classList.toggle('dim', !!it.dim);
      if (it.kind === 'more') { r.btn.textContent = it.text; r.onclick = it.onclick; return; }
      r.el.title = it.title || it.label;
      r.lb.textContent = it.label; r.vv.textContent = it.text || '';
      if (it.kind === 'pair') {
        r.uf.style.height = Math.min(100, Math.max(0, it.up || 0)) + '%'; r.uf.style.backgroundColor = it.ucolor || '';
        r.df.style.height = Math.min(100, Math.max(0, it.down || 0)) + '%'; r.df.style.backgroundColor = it.dcolor || '';
      } else {
        r.f.style.height = Math.min(100, Math.max(0, it.pct || 0)) + '%'; r.f.style.backgroundColor = it.color || '';
      }
    });
    this.cols.forEach(function (r, k) { if (!seen[k]) { r.el.remove(); self.cols.delete(k); } });
    this.host.hidden = !list.length;
  };

  // One stacked horizontal 100% strip with a tiny legend.
  function Strip(parent) {
    this.host = mk('div', 'tshare'); this.strip = mk('div', 'strip'); this.legend = mk('div', 'legend');
    this.host.appendChild(this.strip); this.host.appendChild(this.legend); parent.appendChild(this.host);
  }
  Strip.prototype.set = function (list) {
    this.strip.textContent = ''; this.legend.textContent = '';
    let self = this;
    list.forEach(function (it) {
      let sg = mk('i', 'sg'); sg.style.width = it.share + '%'; sg.style.backgroundColor = it.color; sg.title = it.label + ' ' + it.text;
      self.strip.appendChild(sg);
      let lg = mk('span', 'lg'), sw = mk('i', 'sw'); sw.style.backgroundColor = it.color;
      lg.appendChild(sw); lg.appendChild(document.createTextNode(it.label + ' ' + it.text));
      self.legend.appendChild(lg);
    });
    this.host.hidden = !list.length;
  };

  // ---- history helpers ----
  function pointOf(d) {
    let g = function (a, b) { return d[a] && d[a][b] != null ? d[a][b] : null; };
    let ifs = {};
    if (d.network && d.network.interfaces) {
      d.network.interfaces.forEach(function (i) { ifs[i.name] = [i.down_bps, i.up_bps]; });
    }
    return {
      t: d.sampled_at, cpu: g('cpu', 'percent'), th: d.cpu && d.cpu.threads || null, mem: g('memory', 'used_percent'),
      dr: g('disk', 'read_bps'), dw: g('disk', 'write_bps'), busy: g('disk', 'busy_percent'),
      nd: g('network', 'down_bps'), nu: g('network', 'up_bps'), 'if': ifs
    };
  }
  function windowPts() {
    if (!P.length) return P;
    let cutoff = P[P.length - 1].t - (S.win || 5) * 60000 - intervalMs;
    let i = P.length;
    while (i > 0 && P[i - 1].t >= cutoff) i--;
    return P.slice(i);
  }
  function xs(pts) { return pts.map(function (p) { return p.t / 1000; }); }
  function col(pts, k) { return pts.map(function (p) { return p[k]; }); }
  function neg(a) { return a.map(function (v) { return v == null ? null : -v; }); }
  function peak(pts, f, floor) {
    let cut = P.length ? P[P.length - 1].t - 300000 : 0, m = floor || 0;
    for (let i = P.length - 1; i >= 0 && P[i].t >= cut; i--) { let v = f(P[i]); if (v != null && v > m) m = v; }
    return m;
  }
  // An interface counts as active when the page saw non-zero traffic on it in the last 5 minutes (so it does not flicker).
  function activeIf(i) {
    if (i.down_bps > 0 || i.up_bps > 0) return true;
    let cut = P.length ? P[P.length - 1].t - 300000 : 0;
    for (let k = P.length - 1; k >= 0 && P[k].t >= cut; k--) {
      let v = P[k]['if'] && P[k]['if'][i.name];
      if (v && (v[0] > 0 || v[1] > 0)) return true;
    }
    return false;
  }

  // ---- cards ----
  // Card: title row (title button, headline value, summary, Close), chart, bars container (components inside).
  function mkCard(id) {
    let el = mk('section', 'card'); el.dataset.id = id;
    let ct = mk('div', 'ct');
    let tb = mk('button', 'title'); tb.type = 'button'; tb.textContent = TITLES[id];
    tb.setAttribute('aria-label', TITLES[id] + ': tap to zoom');
    let head = mk('span', 'head');
    let sum = mk('span', 'summary');
    let x = mk('button', 'ibtn zoomx'); x.type = 'button'; x.textContent = 'Close';
    ct.appendChild(tb); ct.appendChild(head); ct.appendChild(sum); ct.appendChild(x);
    let chart = mk('div', 'chart');
    let bars = mk('div', 'bars');
    el.appendChild(ct); el.appendChild(chart); el.appendChild(bars);
    tb.addEventListener('click', function () { setZoom(zoomed === id ? null : id); });
    x.addEventListener('click', function () { setZoom(null); });
    let c = { id: id, el: el, head: head, chartEl: chart, barsEl: bars, sumEl: sum, comps: [], chart: null, update: null };
    // fixed label and value column widths (ch) for every bar of the card
    c.layout = function (labelCh, valueCh) {
      bars.style.setProperty('--lbw', labelCh + 'ch');
      bars.style.setProperty('--vw', valueCh + 'ch');
    };
    c.add = function (comp) { c.comps.push(comp); return comp; };
    return c;
  }
  function fail(c, part) {
    c.head.textContent = '';
    c.comps.forEach(function (k) { k.set([]); });
    c.sumEl.className = 'summary err';
    c.sumEl.textContent = 'unavailable: ' + ((part && part.error) || 'no data');
  }
  function ok(c) { c.sumEl.className = 'summary'; }
  function join(parts) { return parts.filter(Boolean).join(' · '); }

  const FAMILY_COLORS = { opus: '#D97757', sonnet: '#E8B48F', haiku: '#98543D' }, OTHER_COLORS = ['#B07A5C', '#8A7A6E', '#6C4A36'];
  function family(name) { const m = /(opus|sonnet|haiku)/i.exec(name); return m ? m[1].toLowerCase() : name; }

  function buildCards() {
    IDS.forEach(function (id) { cards[id] = mkCard(id); });
    // Each card lives in its own block: `c` is a per-card const, so an update closure can never see another card.

    {
    // CPU: one vertical column per thread, equal widths filling the card; `all` in the title.
    const c = cards.cpu;
    const cols = c.add(new VCols(c.barsEl, 'cpu'));
    c.layout(3, 5);
    c.chart = makeChart(c.chartEl, { kind: 'pct', fmt: function (v) { return v + '%'; }, series: [{ label: 'CPU', key: 'cpu' }] });
    c.update = function (d, pts) {
      let p = d.cpu;
      if (!p || p.error) return fail(c, p);
      ok(c);
      c.head.textContent = 'all ' + pct(p.percent);
      c.chart.set([xs(pts), col(pts, 'cpu')]);
      cols.set((p.threads || []).map(function (v, i) {
        return { key: 't' + i, label: String(i), pct: v, text: String(Math.round(v)), title: 'thread ' + i + ': ' + pct(v, 1), color: ramp('cpu', v) };
      }));
      c.sumEl.textContent = join([
        p.freq_ghz != null ? p.freq_ghz.toFixed(1) + ' GHz' : '', p.temp_c != null ? Math.round(p.temp_c) + '°C' : '',
        'load ' + p.load.map(function (v) { return v.toFixed(2); }).join(' '), (p.threads || []).length + ' threads'
      ]);
    };
    }

    {
    // Memory: one thin meter.
    const c = cards.memory;
    const bars = c.add(new Bars(c.barsEl, 'one'));
    c.layout(5, 11);
    c.chart = makeChart(c.chartEl, { kind: 'pct', fmt: function (v) { return v + '%'; }, series: [{ label: 'used', key: 'memory' }] });
    c.update = function (d, pts) {
      let m = d.memory;
      if (!m || m.error) return fail(c, m);
      ok(c);
      c.head.textContent = pct(m.used_percent);
      c.chart.set([xs(pts), col(pts, 'mem')]);
      bars.set([{ key: 'used', label: 'used', pct: m.used_percent, text: fmtBytes(m.used_bytes) + ' ' + pct(m.used_percent), color: ramp('memory', m.used_percent) }]);
      c.sumEl.textContent = fmtBytes(m.used_bytes) + ' / ' + fmtBytes(m.total_bytes) + (m.swap_total_bytes ? ' · swap ' + pct(m.swap_percent) : '');
    };
    }

    {
    // Disk: fullness meters (one line on a wide card), then one line of busy / read / write meters.
    const c = cards.disk;
    c.barsEl.classList.add('drow');     // mounts + busy/read/write on ONE line from 900 px up (CSS), wrapping below
    const mounts = c.add(new Bars(c.barsEl, 'multi', 8, 15));
    const io = c.add(new Bars(c.barsEl, 'multi', 7, 8));
    c.layout(8, 15);
    // one line from 900 px up only if every cell keeps a >= 60 px track; else the groups wrap (class `wrap`)
    let chPx = 0, wrapped = false, mountLbw = 8;
    function diskFit() {
      const vw = window.innerWidth || 1280, bw = c.barsEl.clientWidth;
      if (!chPx) {
        const pr = mk('span', ''); pr.style.width = '100ch'; pr.style.position = 'absolute'; pr.style.visibility = 'hidden';
        c.barsEl.appendChild(pr); chPx = (pr.offsetWidth || 0) / 100; pr.remove();
        if (!(chPx > 3)) chPx = 7.2;
      }
      const lbw = mountLbw;
      const cell = function (l, v) { return (l + v) * chPx + 60 + 8; };
      const need = mounts.rows.size * cell(lbw, 15) + io.rows.size * cell(7, 8) + 14;
      const want = vw < 900 || (bw > 0 && need > bw);
      if (want !== wrapped) { wrapped = want; c.barsEl.classList.toggle('wrap', want); scheduleAuto(); }
    }
    window.addEventListener('resize', function () { if (mounts.rows.size) diskFit(); });
    c.chart = makeChart(c.chartEl, {
      kind: 'mirror', floor: 1 << 20, fmt: fmtRate,
      series: [{ label: 'read', key: 'disk_read' }, { label: 'write', key: 'disk_write', neg: true }]
    });
    c.update = function (d, pts) {
      let k = d.disk;
      if (!k || k.error) return fail(c, k);
      ok(c);
      c.head.textContent = pct(k.busy_percent) + ' busy';
      c.chart.set([xs(pts), col(pts, 'dr'), neg(col(pts, 'dw'))]);
      mounts.set((k.mounts || []).map(function (m) {
        return {
          key: 'm' + m.target, label: m.target, title: m.source, pct: m.used_percent,
          text: pct(m.used_percent) + ' ' + fmtBytes(m.used_bytes) + '/' + fmtBytes(m.total_bytes), color: ramp('cpu', m.used_percent)
        };
      }));
      let rows = (k.devices || []).map(function (v) {
        return { key: 'b' + v.name, label: v.name, title: v.name + ' busy', pct: v.busy_percent, text: pct(v.busy_percent) + ' busy', color: ramp('cpu', v.busy_percent) };
      });
      let pr = peak(pts, function (p) { return p.dr; }, 1 << 20), pw = peak(pts, function (p) { return p.dw; }, 1 << 20);
      rows.push({ key: 'rd', label: 'read', pct: k.read_bps / pr * 100, text: fmtRate(k.read_bps), color: stops('disk_read')[2] });
      rows.push({ key: 'wr', label: 'write', pct: k.write_bps / pw * 100, text: fmtRate(k.write_bps), color: stops('disk_write')[2] });
      io.set(rows);
      // mount label column = longest mount name (+1ch), at most 16ch; then wrap rather than squeeze the tracks
      mountLbw = Math.min(16, (k.mounts || []).reduce(function (n, m) { return Math.max(n, m.target.length); }, 3) + 1);
      mounts.labelCh(mountLbw);
      diskFit();
      let root = (k.mounts || []).filter(function (m) { return m.target === '/'; })[0];
      c.sumEl.textContent = join([k.temp_c != null ? Math.round(k.temp_c) + '°C' : '', root ? fmtBytes(root.avail_bytes) + ' free on /' : '']);
    };
    }

    {
    // Network: a mirrored vertical pair per active interface (up above the midline, down below), name under it,
    // each half scaled to that interface's rolling 5-minute peak of that direction (floor 100 kB/s). `all` is in the title.
    const c = cards.network;
    const cols = c.add(new VCols(c.barsEl, 'net fixed'));
    c.layout(8, 8);
    c.chart = makeChart(c.chartEl, {
      kind: 'mirror', floor: 100000, fmt: fmtRate,
      series: [{ label: 'down', key: 'net_down' }, { label: 'up', key: 'net_up', neg: true }]
    });
    c.update = function (d, pts) {
      let n = d.network;
      if (!n || n.error) return fail(c, n);
      ok(c);
      c.head.textContent = '↓' + fmtBytes(n.down_bps) + ' ↑' + fmtBytes(n.up_bps);
      c.chart.set([xs(pts), col(pts, 'nd'), neg(col(pts, 'nu'))]);
      let ifs = n.interfaces || [];
      let showIdle = !!S.idle || zoomed === 'network';
      let shown = ifs.filter(function (i) { return showIdle || activeIf(i); });
      let cells = shown.map(function (i) {
        // `included` exists from readout schema 2; older servers leave it out and everything counts.
        let inc = i.included;
        let pd = Math.max(peak(pts, function (p) { return p['if'][i.name] ? p['if'][i.name][0] : null; }, 100000), i.down_bps);
        let pu = Math.max(peak(pts, function (p) { return p['if'][i.name] ? p['if'][i.name][1] : null; }, 100000), i.up_bps);
        return {
          kind: 'pair', key: 'n:' + i.name, label: i.name, dim: inc === false,
          title: i.name + (inc === false ? ' (not counted)' : '') + ' ↓' + fmtRate(i.down_bps) + ' ↑' + fmtRate(i.up_bps),
          down: i.down_bps / pd * 100, up: i.up_bps / pu * 100, dcolor: stops('net_down')[2], ucolor: stops('net_up')[2],
          text: '↓' + fmtBytes(i.down_bps) + ' ↑' + fmtBytes(i.up_bps)
        };
      });
      // Safety net only: a long list keeps NET_CAP cells and a `+N more` cell (the zoom view shows everything).
      if (zoomed !== 'network' && cells.length > NET_CAP) {
        if (!S.netAll) {
          let hidden = cells.length - NET_CAP;
          cells = cells.slice(0, NET_CAP);
          cells.push({ kind: 'more', key: 'more', text: '+' + hidden + ' more', onclick: function () { S.netAll = true; saveSettings(); render(true); } });
        } else {
          cells.push({ kind: 'more', key: 'more', text: 'show fewer', onclick: function () { S.netAll = false; saveSettings(); render(true); } });
        }
      }
      cols.set(cells);
      c.sumEl.textContent = ifs.length + ' interfaces, ' + ifs.filter(activeIf).length + ' active';
    };
    }

    {
    // Tokens: rate columns (input / output / cache wr / cache rd) and one stacked per-model share strip.
    const c = cards.tokens;
    c.barsEl.classList.add('trow');
    const cols = c.add(new VCols(c.barsEl, 'fixed showv'));
    const strip = c.add(new Strip(c.barsEl));
    c.layout(8, 8);
    c.chart = makeChart(c.chartEl, {
      kind: 'mirror', floor: 100, fmt: fmtTok, tokens: true,
      series: [{ label: 'in', key: 'tokens_in' }, { label: 'out', key: 'tokens_out', neg: true }]
    });
    c.update = function (d) {
      let t = d.tokens;
      if (!t || t.error) return fail(c, t);
      ok(c);
      let s = tokSeries();
      c.chart.set([s.xs, s.a, s.b]);
      let r = t.per_min_5m, today = t.today;
      c.head.textContent = fmtTok(r.input + r.cache_write + r.output) + '/min';
      let pin = 0, pout = 0, pcr = 0;
      tokens.min.forEach(function (b) { if (b[1] > pin) pin = b[1]; if (b[2] > pout) pout = b[2]; if (b[3] > pcr) pcr = b[3]; });
      let sc = function (v, p) { return v / Math.max(p, v, 1) * 100; };
      cols.set([
        { key: 'in', label: 'in', title: 'input per minute', pct: sc(r.input, pin), text: fmtTok(r.input), color: stops('tokens_in')[2] },
        { key: 'out', label: 'out', title: 'output per minute', pct: sc(r.output, pout), text: fmtTok(r.output), color: stops('tokens_out')[2] },
        { key: 'cw', label: 'cache wr', title: 'cache write per minute', pct: sc(r.cache_write, pin), text: fmtTok(r.cache_write), color: stops('tokens_cache')[2] },
        { key: 'cr', label: 'cache rd', title: 'cache read per minute', pct: sc(r.cache_read, pcr), text: fmtTok(r.cache_read), color: stops('tokens_cache')[1] }
      ]);
      let models = t.by_model_today || {}, total = 0, sums = {}, other = 0;
      Object.keys(models).forEach(function (m) {
        let x = models[m], f = family(m);
        sums[f] = (sums[f] || 0) + x.input + x.cache_write + x.cache_read + x.output;
      });
      Object.keys(sums).forEach(function (f) { total += sums[f]; });
      strip.set(Object.keys(sums).filter(function (f) { return sums[f] > 0; }).sort(function (a, b) { return sums[b] - sums[a]; }).map(function (f) {
        let share = sums[f] / total * 100;
        return { label: f, share: share, text: pct(share), color: FAMILY_COLORS[f] || OTHER_COLORS[other++ % OTHER_COLORS.length] };
      }));
      let ix = t.index && t.index.phase !== 'live' ? 'indexing… ' + Math.round(t.index.percent) + '%' : '';
      c.sumEl.textContent = join([
        'today in ' + fmtTok(today.input + today.cache_write) + ' out ' + fmtTok(today.output) + ' cache ' + fmtTok(today.cache_read), ix
      ]);
    };
    }

    {
    // Claude: thin rows with the pace marker; the values sit on the same line.
    const c = cards.claude;
    const bars = c.add(new Bars(c.barsEl, 'one stack'));
    c.layout(12, 30);
    c.update = function (d) {
      let k = d.claude;
      if (!k || k.error) return fail(c, k);
      ok(c);
      let rows = [], now = Date.now();
      let orange = cssVar('--claude') || '#D97757', red = cssVar('--danger') || '#ef4444';
      let wk = k.week;
      // today's day index in the week window: from the reset time when known, else from the elapsed percent
      let dayOf = function (resets) {
        let i = resets ? Math.floor((now - (resets - 604800e3)) / 86400e3) : (wk && wk.elapsed_percent != null ? Math.floor(wk.elapsed_percent * 7 / 100) : -1);
        return { today: i >= 0 && i <= 6 ? i : -1 };
      };
      let add = function (key, label, used, expected, resets, reset, week) {
        if (used == null) return;
        let over = (expected != null && used > expected) || used >= 95;
        let text = Math.round(used) + '%' + (expected != null ? ' / ' + Math.round(expected) + '%' : '');
        if (reset && resets) text += '  ' + clock(resets) + ' (' + fmtLeft(resets - now) + ')';
        rows.push({ key: key, label: label, pct: used, mark: expected, text: text, color: over ? red : orange, days: week ? dayOf(resets || (wk && wk.resets_at)) : null });
      };
      let s = k.session, w = k.week;
      if (s) add('s', '5 Hour', s.percent, s.elapsed_percent, s.resets_at, true);
      if (w && w.day_part) add('d', 'Daily', w.day_part.percent, null, null, false);
      if (w) add('w', 'Weekly', w.percent, w.elapsed_percent, w.resets_at, true, true);
      // A model week with no usage (rounds to 0%) is not shown (v8.5); the value columns are fixed, so nothing shifts.
      (k.models || []).forEach(function (m, i) { if (Math.round(m.percent) <= 0) return; add('m' + i, m.label, m.percent, w ? w.elapsed_percent : null, m.resets_at, false, true); });
      bars.set(rows);
      c.head.textContent = s ? pct(s.percent) : '';
      let stale = k.stale ? 'Stale - last read ' + clock(k.read_at) : '';
      c.sumEl.textContent = join([k.source, stale]);
    };
    }
  }

  // Tokens window helpers
  const SPAN = { '1h': 3600e3, '6h': 21600e3, '24h': 86400e3, '7d': 604800e3, '30d': 2592000e3 };
  function tokWin() { return S.twin && SPAN[S.twin] ? S.twin : (server && server.layout && SPAN[server.layout.tokens_window] ? server.layout.tokens_window : '24h'); }
  function tokSpan() { return SPAN[tokWin()]; }
  function tokSeries() {
    let w = tokWin(), hour = w === '7d' || w === '30d', size = hour ? 3600000 : 60000;
    let src = hour ? tokens.hour : tokens.min, n = Math.round(SPAN[w] / size);
    let lastB = Math.floor(Date.now() / size) * size, m = {}, X = [], A = [], B = [];
    src.forEach(function (r) { m[r[0]] = r; });
    for (let i = n - 1; i >= 0; i--) {
      let t = lastB - i * size, r = m[t];
      X.push(t / 1000); A.push(r ? r[1] : 0); B.push(r ? -r[2] : 0);
    }
    return { xs: X, a: A, b: B };
  }

  // ---- layout ----
  // Section order for the stacked layout: the device's order, then the server's, then the rest.
  function order() {
    let base = [];
    ((server && server.layout && server.layout.rows) || []).forEach(function (r) { r.forEach(function (x) { base.push(x.id); }); });
    let out = [];
    (S.order || []).concat(base, IDS).forEach(function (i) { if (IDS.indexOf(i) >= 0 && out.indexOf(i) < 0) out.push(i); });
    return out;
  }
  function visible(id) {
    let v = sec(id).show;
    if (v !== undefined) return !!v;
    let s = server && server.layout && server.layout.sections[id];
    return s ? !!s.enabled : true;
  }
  // A part (graph, bars, summary) is on by the device's choice, else the server's; the Claude section has no chart.
  function part(id, k) {
    if (zoomed === id) return !(k === 'graph' && id === 'claude');
    let v = sec(id)[k];
    if (v !== undefined) return !!v && !(k === 'graph' && id === 'claude');
    if (k === 'graph' && id === 'claude') return false;
    let cfg = (server && server.layout && server.layout.sections[id]) || {};
    return cfg[k] !== false;
  }
  // Chart height: a manual per-section value wins; else `auto` in compact (autoFit), else the density default.
  function chartH(id) { return sec(id).h || CH_DEF[density()]; }
  function isAuto(id) { return density() === 'compact' && !sec(id).h; }

  // ---- auto chart height (compact): the spare viewport height is shared among the auto charts ----
  const autoH = {};            // id -> px, set by autoFit
  let autoTimer = 0;
  // Pure: share `spare` px among items [{id, w}] by weight, each clamped to [lo, hi], water-filling so clamped items
  // give or take the rest; integer px, the rounding remainder goes to the items still below `hi`.
  function distribute(items, spare, lo, hi) {
    const out = {};
    if (!items.length) return out;
    if (spare <= lo * items.length) { items.forEach(function (it) { out[it.id] = lo; }); return out; }
    if (spare >= hi * items.length) { items.forEach(function (it) { out[it.id] = hi; }); return out; }
    let free = items.slice(), left = spare;
    for (let guard = 0; guard < 10 && free.length; guard++) {
      const tw = free.reduce(function (n, it) { return n + it.w; }, 0);
      // clamp one direction per pass (the low ones first), so the clamped ones never push the rest out of range
      let bad = free.filter(function (it) { return left * it.w / tw < lo; });
      if (!bad.length) bad = free.filter(function (it) { return left * it.w / tw > hi; });
      if (!bad.length) break;
      bad.forEach(function (it) { const v = left * it.w / tw; out[it.id] = v < lo ? lo : hi; left -= out[it.id]; });
      free = free.filter(function (it) { return bad.indexOf(it) < 0; });
    }
    const tw = free.reduce(function (n, it) { return n + it.w; }, 0);
    const share = left;
    free.forEach(function (it) { out[it.id] = Math.floor(share * it.w / tw); });
    // hand the remainder out one px at a time (largest weight first)
    let rest = Math.round(spare - items.reduce(function (n, it) { return n + out[it.id]; }, 0));
    const order = items.slice().sort(function (a, b) { return b.w - a.w; });
    for (let i = 0; rest > 0 && i < 1000; i++) {
      const it = order[i % order.length];
      if (out[it.id] < hi) { out[it.id]++; rest--; }
    }
    return out;
  }
  // One measurement (reads only), one write: non-chart height = page height minus the auto charts' current height.
  function autoFit() {
    if (zoomed || density() !== 'compact' || !window.innerHeight) return;
    const body = document.body || document.documentElement;
    const items = [];
    let autoNow = 0;
    IDS.forEach(function (id) {
      const c = cards[id];
      if (!c || !AUTO_W[id] || c.el.hidden || !isAuto(id) || !part(id, 'graph')) return;
      items.push({ id: id, w: AUTO_W[id] });
      autoNow += c.chartEl.clientHeight;
    });
    if (!items.length) return;
    const spare = window.innerHeight - (body.offsetHeight - autoNow);
    const sizes = distribute(items, spare, AUTO_MIN + X_AXIS, AUTO_MAX + X_AXIS);
    let changed = false;
    IDS.forEach(function (id) {
      if (sizes[id] == null) { if (autoH[id] != null) { delete autoH[id]; } return; }
      if (autoH[id] !== sizes[id]) { autoH[id] = sizes[id]; changed = true; }
      cards[id].el.style.setProperty('--ch', sizes[id] + 'px');
    });
    if (changed) resizeCharts();
  }
  // Debounced and coalesced: any number of triggers in a burst give one fit, after the next frame.
  function scheduleAuto() {
    if (autoTimer) return;
    autoTimer = setTimeout(function () {
      autoTimer = 0;
      if (window.requestAnimationFrame) window.requestAnimationFrame(autoFit); else autoFit();
    }, 50);
  }
  function serverRows() {
    let rows = [], seen = {};
    ((server && server.layout && server.layout.rows) || []).forEach(function (r) {
      let nr = [];
      r.forEach(function (x) { if (IDS.indexOf(x.id) >= 0 && !seen[x.id]) { seen[x.id] = 1; nr.push({ id: x.id, w: Math.max(1, Math.min(9, Math.round(x.w) || 1)) }); } });
      if (nr.length) rows.push(nr);
    });
    IDS.forEach(function (id) { if (!seen[id]) rows.push([{ id: id, w: 1 }]); });
    return rows;
  }
  // The custom rows, normalized: unknown or repeated sections dropped, weights integers 1-9, missing sections appended
  // as rows of their own. Empty rows are kept (the editor shows them); the grid skips them.
  function customRows() {
    let seen = {}, out = [];
    (Array.isArray(S.rows) ? S.rows : []).forEach(function (r) {
      let nr = [];
      (Array.isArray(r) ? r : []).forEach(function (x) {
        if (x && IDS.indexOf(x.id) >= 0 && !seen[x.id]) { seen[x.id] = 1; nr.push({ id: x.id, w: Math.max(1, Math.min(9, Math.round(x.w) || 1)) }); }
      });
      out.push(nr);
    });
    IDS.forEach(function (id) { if (!seen[id]) out.push([{ id: id, w: 1 }]); });
    return out;
  }
  function effectiveRows() {
    let mode = layoutMode();
    if (mode === 'rows') return serverRows();
    if (mode === 'custom') return customRows();
    return order().map(function (id) { return [{ id: id, w: 1 }]; });
  }
  function applyLayout() {
    let grid = $('#grid');
    let old = grid.children.slice ? grid.children.slice() : Array.prototype.slice.call(grid.children);
    let rowEls = [];
    effectiveRows().forEach(function (r) {
      let list = r.filter(function (x) { return visible(x.id) || zoomed === x.id; });
      if (!list.length) return;
      let re = mk('div', 'lrow');
      re.style.setProperty('--cols', list.map(function (x) { return 'minmax(0,' + x.w + 'fr)'; }).join(' '));
      list.forEach(function (x) { re.appendChild(cards[x.id].el); });
      rowEls.push(re);
    });
    old.forEach(function (e) { e.remove(); });
    rowEls.forEach(function (e) { grid.appendChild(e); });
    IDS.forEach(function (id) {
      let c = cards[id];
      c.el.hidden = !visible(id) && zoomed !== id;
      c.chartEl.classList.toggle('off', !part(id, 'graph'));
      c.barsEl.hidden = !part(id, 'bars');
      c.sumEl.hidden = !part(id, 'summary');
      // manual height wins; else the last auto fit (compact) until the next one; else the density default
      if (zoomed === id) c.el.style.removeProperty('--ch');
      else if (sec(id).h) c.el.style.setProperty('--ch', sec(id).h + 'px');
      else if (isAuto(id) && autoH[id] != null) c.el.style.setProperty('--ch', autoH[id] + 'px');
      else c.el.style.removeProperty('--ch');
    });
    grid.setAttribute('data-layout', layoutMode());
    scheduleAuto();
  }
  function setZoom(id) {
    zoomed = id;
    Object.keys(cards).forEach(function (k) { cards[k].el.classList.toggle('zoom', k === id); });
    document.documentElement.style.overflow = id ? 'hidden' : '';
    applyLayout();
    if (last) render(true);
    resizeCharts();
    if (window.requestAnimationFrame) window.requestAnimationFrame(resizeCharts);
  }
  // Re-place the cards, refill them and resize the charts: after any layout or section change.
  function relayout() {
    applyLayout();
    if (last) render(true);
    resizeCharts();
  }

  // ---- settings sheet ----
  function btn(text, act, label, cls) {
    let b = mk('button', 'ibtn ' + (cls || '')); b.type = 'button'; b.textContent = text; b.dataset.act = act;
    b.setAttribute('aria-label', label || text); b.title = label || text;
    return b;
  }
  function stepper(value, act, dec, inc, lab) {
    let w = mk('span', 'step'), n = mk('span', 'sv'); n.textContent = String(value); n.dataset.act = act + '-value';
    let m = btn('−', act + '-', 'Decrease ' + lab), p = btn('+', act + '+', 'Increase ' + lab);
    m.addEventListener('click', dec); p.addEventListener('click', inc);
    w.appendChild(m); w.appendChild(n); w.appendChild(p);
    return w;
  }
  function toggle(text, on, label, fn) {
    let b = btn(text, 'toggle-' + text.toLowerCase(), label, 'tog');
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.addEventListener('click', fn);
    return b;
  }

  function renderSections() {
    let ul = $('#set-sections'); ul.textContent = '';
    let ord = order(), stacked = layoutMode() === 'stacked';
    ord.forEach(function (id, i) {
      let li = mk('li'); li.dataset.id = id;
      let lab = mk('label', 'nm');
      let cb = mk('input'); cb.type = 'checkbox'; cb.checked = visible(id); cb.dataset.act = 'show';
      cb.addEventListener('change', function () { setSec(id, 'show', cb.checked); relayout(); renderRows(); });
      lab.appendChild(cb); lab.appendChild(document.createTextNode(TITLES[id]));
      li.appendChild(lab);
      [['graph', 'Line', 'timeline'], ['bars', 'Bars', 'bars'], ['summary', 'Sum', 'summary']].forEach(function (k) {
        if (k[0] === 'graph' && id === 'claude') return;
        li.appendChild(toggle(k[1], part(id, k[0]), 'Show ' + k[2] + ' of ' + TITLES[id], function () {
          setSec(id, k[0], !part(id, k[0])); relayout(); renderSections();
        }));
      });
      if (id !== 'claude') {
        let step = function (dir) {
          return function () { setSec(id, 'h', Math.max(CH_MIN, Math.min(CH_MAX, chartH(id) + dir * CH_STEP))); relayout(); renderSections(); };
        };
        // compact: `auto` is a state (the value shows `auto`); − n + sets a manual height, the auto button goes back
        let st = stepper(isAuto(id) ? 'auto' : chartH(id), 'h', step(-1), step(1), 'chart height of ' + TITLES[id]);
        st.children[1].style.minWidth = '4ch';
        li.appendChild(st);
        let ab = btn('auto', 'h-auto', 'Chart height of ' + TITLES[id] + ': fit the screen automatically');
        ab.setAttribute('aria-pressed', isAuto(id) ? 'true' : 'false');
        ab.disabled = density() !== 'compact' || isAuto(id);
        ab.addEventListener('click', function () {
          if (S.sec && S.sec[id]) delete S.sec[id].h;
          saveSettings(); relayout(); renderSections();
        });
        li.appendChild(ab);
      }
      if (stacked) {
        [['↑', -1, 'up'], ['↓', 1, 'down']].forEach(function (b) {
          let bt = btn(b[0], b[2], 'Move ' + TITLES[id] + ' ' + b[2]);
          bt.disabled = (b[1] < 0 && i === 0) || (b[1] > 0 && i === ord.length - 1);
          bt.addEventListener('click', function () {
            let o = ord.slice(), j = i + b[1], t = o[i]; o[i] = o[j]; o[j] = t;
            S.order = o; saveSettings(); relayout(); renderSections();
          });
          li.appendChild(bt);
        });
      }
      ul.appendChild(li);
    });
  }

  // The custom rows editor: each row holds sections with integer width weights.
  function editRows(fn) {
    let rows = customRows();
    fn(rows);
    S.rows = rows; saveSettings();
    relayout(); renderRows();
  }
  function moveSection(rows, ri, ci, dir) {
    let it = rows[ri].splice(ci, 1)[0], src = rows[ri];
    if (dir < 0) { if (ri === 0) { rows.unshift([it]); ri += 1; } else rows[ri - 1].push(it); }
    else if (ri === rows.length - 1) rows.push([it]); else rows[ri + 1].unshift(it);
    if (!src.length) rows.splice(rows.indexOf(src), 1);
  }
  function renderRows() {
    let wrap = $('#set-custom'); wrap.hidden = layoutMode() !== 'custom';
    if (wrap.hidden) return;
    let host = $('#set-rows'); host.textContent = '';
    let rows = customRows();
    rows.forEach(function (r, ri) {
      let box = mk('div', 'erow'); box.dataset.row = String(ri);
      let hd = mk('div', 'ehead'), t = mk('span', 'muted'); t.textContent = 'Row ' + (ri + 1) + (r.length ? '' : ' (empty)');
      hd.appendChild(t);
      let rm = btn('Remove row', 'rmrow', 'Remove row ' + (ri + 1));
      rm.disabled = rows.length < 2;
      rm.addEventListener('click', function () {
        editRows(function (rs) {
          let gone = rs.splice(ri, 1)[0], into = rs[ri - 1] || rs[ri];
          gone.forEach(function (x) { into.push(x); });
        });
      });
      hd.appendChild(rm);
      box.appendChild(hd);
      let chips = mk('div', 'chips');
      r.forEach(function (x, ci) {
        let chip = mk('div', 'echip'); chip.dataset.id = x.id;
        let nm = mk('span', 'nm'); nm.textContent = TITLES[x.id]; chip.appendChild(nm);
        let acts = [
          ['←', 'left', 'Move ' + TITLES[x.id] + ' left', ci === 0, function (rs) { let a = rs[ri]; let t = a[ci]; a[ci] = a[ci - 1]; a[ci - 1] = t; }],
          ['→', 'right', 'Move ' + TITLES[x.id] + ' right', ci === r.length - 1, function (rs) { let a = rs[ri]; let t = a[ci]; a[ci] = a[ci + 1]; a[ci + 1] = t; }],
          ['↑', 'up', 'Move ' + TITLES[x.id] + ' into the previous row', false, function (rs) { moveSection(rs, ri, ci, -1); }],
          ['↓', 'down', 'Move ' + TITLES[x.id] + ' into the next row', false, function (rs) { moveSection(rs, ri, ci, 1); }]
        ];
        acts.forEach(function (a) {
          let b = btn(a[0], a[1], a[2]); b.disabled = a[3];
          b.addEventListener('click', function () { editRows(a[4]); });
          chip.appendChild(b);
        });
        let w = function (dir) { return function () { editRows(function (rs) { rs[ri][ci].w = Math.max(1, Math.min(9, rs[ri][ci].w + dir)); }); }; };
        chip.appendChild(stepper(x.w, 'w', w(-1), w(1), 'width of ' + TITLES[x.id]));
        chips.appendChild(chip);
      });
      box.appendChild(chips);
      host.appendChild(box);
    });
  }

  function renderSettings() {
    renderSections();
    renderRows();
    let nets = $('#set-nets'); nets.textContent = '';
    let ifs = (last && last.network && last.network.interfaces) || [];
    if (!ifs.length) { let e = mk('li'); e.textContent = 'none seen yet'; nets.appendChild(e); }
    ifs.forEach(function (i) {
      let li = mk('li');
      let n = mk('span'); n.textContent = i.name; n.style.flex = '1';
      let t = mk('span', 'tag' + (i.included === false ? '' : ' on'));
      t.textContent = i.included === false ? 'not counted' : 'counted';
      li.appendChild(n); li.appendChild(t); nets.appendChild(li);
    });
    $('#set-density').value = density();
    $('#set-layout').value = layoutMode();
    $('#set-win').value = String(S.win || 5);
    $('#set-twin').value = tokWin();
    $('#set-theme').value = S.theme || 'server';
    $('#set-idle').checked = !!S.idle;
    $('#set-info').textContent = server
      ? 'Server: ' + server.history_min + ' min of history, one sample every ' + server.interval_ms + ' ms, ' + (server.tls ? 'HTTPS available' : 'no certificate') + '.'
      : '';
  }

  // ---- rendering ----
  function render(force) {
    if (!last || !server) return;
    if (document.hidden && !force) return;
    scheduleAuto();   // content height can change with the data (mounts, interfaces); one coalesced fit
    let pts = windowPts();
    IDS.forEach(function (id) {
      let c = cards[id];
      if (c.el.hidden && zoomed !== id) return;
      if (zoomed && zoomed !== id) return;
      try { c.update(last, pts); } catch (e) { if (window.console) console.error(id, e); }
    });
  }

  function setStatus(text, cls) {
    let s = $('#status');
    s.textContent = text; s.className = 'pill' + (cls ? ' ' + cls : '');
  }

  // ---- stream ----
  function probe() {
    let now = Date.now();
    if (now - lastProbe < 10000) return;
    lastProbe = now;
    fetch('/vitals', { method: 'HEAD', cache: 'no-store' }).then(function (r) {
      if (r.status === 401) window.location.reload();
    }).catch(function () { /* offline: the stream retries */ });
  }
  function connect() {
    if (es) { try { es.close(); } catch (e) { /* ignore */ } }
    setStatus(last ? 'reconnecting…' : 'connecting…', 'warn');
    touched = Date.now();
    es = new EventSource('/vitals/stream');
    es.onopen = function () { touched = Date.now(); };
    es.onerror = function () {
      setStatus('reconnecting…', 'warn');
      probe();
      if (es.readyState === 2) setTimeout(connect, 3000);
    };
    es.addEventListener('history', function (ev) {
      let h = JSON.parse(ev.data);
      touched = Date.now();
      server = h; P = h.points || []; tokens = h.tokens || { min: [], hour: [] };
      intervalMs = h.interval_ms || 1000;
      $('#banner').hidden = !!h.tls;
      if (!cards.cpu) buildCards();
      applyTheme(); applyLayout();
      setStatus('live', 'live');
      renderSettings();
    });
    es.addEventListener('sample', function (ev) {
      let d = JSON.parse(ev.data);
      touched = Date.now();
      last = d;
      P.push(pointOf(d));
      let max = Math.ceil(1800000 / intervalMs) + 10;
      if (P.length > max) P.splice(0, P.length - max);
      setStatus('live', 'live');
      render();
    });
    es.addEventListener('tokens', function (ev) {
      tokens = JSON.parse(ev.data);
      touched = Date.now();
      if (last && !document.hidden) render(true);
    });
  }
  setInterval(function () {
    if (es && Date.now() - touched > Math.max(3 * intervalMs, 8000)) connect();
  }, 2000);

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    if (Date.now() - touched > Math.max(3 * intervalMs, 5000)) connect();
    if (wakeWanted() && !wakeLock) acquireWake();
    render(true);
    scheduleAuto();
  });
  window.addEventListener('resize', function () { resizeCharts(); scheduleAuto(); });
  window.addEventListener('orientationchange', function () { setTimeout(function () { resizeCharts(); scheduleAuto(); }, 200); });
  window.addEventListener('online', function () { connect(); });
  window.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { if (!$('#sheet').hidden) closeSheet(); else if (zoomed) setZoom(null); }
  });

  // ---- header buttons ----
  let wakeLock = null;
  function wakeWanted() { return store.get('vitals.wake') === '1'; }
  function acquireWake() {
    if (!navigator.wakeLock) return;
    navigator.wakeLock.request('screen').then(function (l) {
      wakeLock = l;
      l.addEventListener('release', function () { wakeLock = null; });
    }).catch(function () { wakeLock = null; });
  }
  function setupButtons() {
    let wake = $('#wake');
    if (window.isSecureContext && navigator.wakeLock) {
      wake.hidden = false;
      wake.setAttribute('aria-pressed', wakeWanted() ? 'true' : 'false');
      if (wakeWanted()) acquireWake();
      wake.addEventListener('click', function () {
        let on = !wakeWanted();
        store.set('vitals.wake', on ? '1' : '0');
        wake.setAttribute('aria-pressed', on ? 'true' : 'false');
        if (on) acquireWake(); else if (wakeLock) { wakeLock.release(); wakeLock = null; }
      });
    }
    let full = $('#full');
    if (document.documentElement.requestFullscreen) {
      full.hidden = false;
      full.addEventListener('click', function () {
        if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(function () { /* refused */ });
      });
    }
    $('#gear').addEventListener('click', function () { renderSettings(); $('#sheet').hidden = false; });
    $('#sheet-close').addEventListener('click', closeSheet);
    $('#sheet').addEventListener('click', function (e) { if (e.target === this) closeSheet(); });
    let on = function (id, fn) { $(id).addEventListener('change', function () { fn(this.value, this); saveSettings(); }); };
    on('#set-density', function (v) { S.density = v === 'comfortable' ? 'comfortable' : 'compact'; applyDensity(); relayout(); rebuildCharts(); renderSections(); });
    on('#set-layout', function (v) {
      let before = effectiveRows();   // what is on screen now: the starting point of a first custom layout
      S.layout = v === 'rows' || v === 'custom' ? v : 'stacked';
      if (S.layout === 'custom' && !Array.isArray(S.rows)) S.rows = before;
      relayout(); renderSections(); renderRows();
    });
    on('#set-win', function (v) { S.win = +v || 5; rebuildCharts(); });      // rebuilt: the tick increments follow the window
    on('#set-twin', function (v) { S.twin = v; rebuildCharts(); });
    on('#set-theme', function (v) { S.theme = v; applyTheme(); });
    on('#set-idle', function (v, el) { S.idle = !!el.checked; render(true); });
    $('#add-row').addEventListener('click', function () { editRows(function (rs) { rs.push([]); }); });
    $('#reset-layout').addEventListener('click', function () {
      delete S.layout; delete S.rows; delete S.order; delete S.sec; saveSettings();
      relayout(); renderSettings();
    });
    $('#reset').addEventListener('click', function () {
      S = {}; store.del(KEY2); applyDensity(); applyTheme(); relayout(); rebuildCharts(); renderSettings();
    });
    if (window.location.protocol === 'https:') $('#cert').hidden = false;
    let deferred = null;
    window.addEventListener('beforeinstallprompt', function (e) {
      e.preventDefault(); deferred = e; $('#install').hidden = false;
    });
    $('#install').addEventListener('click', function () {
      if (deferred) { deferred.prompt(); deferred = null; $('#install').hidden = true; }
    });
    setInterval(function () { $('#clock').textContent = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' }); }, 1000);
  }
  function closeSheet() { $('#sheet').hidden = true; }

  setupButtons();
  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/sw.js').catch(function () { /* optional */ });
  }
  connect();
})();
