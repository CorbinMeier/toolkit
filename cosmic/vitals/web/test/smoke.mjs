// Smoke test for web/app.js: runs it against a hand-rolled fake DOM (no npm, no browser), feeds the recorded
// `history` and `sample` events of fixture.json and fails on any exception, console.error or card whose
// update did not run. uPlot is stubbed. Also checks the v8 rules (spec 14.5): compact and stacked by default,
// square and stepped, vertical CPU columns, idle interfaces hidden, the custom rows editor, storage v2 and the
// v1 migration. Run: node web/test/smoke.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = path.dirname(fileURLToPath(import.meta.url));
const code = fs.readFileSync(path.join(dir, '..', 'app.js'), 'utf8');
const css = fs.readFileSync(path.join(dir, '..', 'app.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
const fixture = JSON.parse(fs.readFileSync(path.join(dir, 'fixture.json'), 'utf8'));

const errors = [];
const fail = (m) => { console.error('SMOKE FAIL: ' + m); process.exit(1); };
const listeners = (o) => o.__l || (o.__l = {});
function emit(o, type, ev) { (listeners(o)[type] || []).forEach((f) => f.call(o, ev || { type })); }

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.children = []; this.parent = null; this.dataset = {};
    this.attrs = {}; this.hidden = false; this.className = ''; this._t = ''; this.value = ''; this.checked = false; this.disabled = false;
    this.clientWidth = 400; this._ch = 150; this.title = '';
    const get = () => this.className.split(/\s+/).filter(Boolean);
    const put = (a) => { this.className = a.join(' '); };
    this.classList = {
      add: (c) => { const a = get(); if (!a.includes(c)) a.push(c); put(a); },
      remove: (c) => put(get().filter((x) => x !== c)),
      toggle: (c, on) => { const has = get().includes(c); const want = on === undefined ? !has : !!on; if (want && !has) this.classList.add(c); if (!want && has) this.classList.remove(c); },
      contains: (c) => get().includes(c)
    };
    const st = {};
    this.style = { setProperty: (k, v) => { st[k] = v; }, removeProperty: (k) => { delete st[k]; }, get: (k) => st[k] };
  }
  // a chart box is as tall as its card's --ch (56 px when unset); 0 when switched off or the card is hidden
  get clientHeight() {
    if (this.hasClass('chart') && this.parent) {
      if (this.hasClass('off') || this.parent.hidden) return 0;
      const v = this.parent.style.get('--ch');
      return v ? parseFloat(v) : 56;
    }
    return this._ch;
  }
  set clientHeight(v) { this._ch = v; }
  get textContent() { return this._t; }
  set textContent(v) { this._t = String(v); if (this._t === '') { this.children.forEach((c) => { c.parent = null; }); this.children = []; } }
  appendChild(c) { return this.insertBefore(c, null); }
  insertBefore(c, ref) {
    if (c.parent) c.parent.children.splice(c.parent.children.indexOf(c), 1);
    c.parent = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, f) { (listeners(this)[t] = listeners(this)[t] || []).push(f); }
  querySelector(sel) { return findAll(this, (e) => e.hasClass(sel.slice(1)))[0] || null; }
  hasClass(c) { return (' ' + this.className + ' ').includes(' ' + c + ' '); }
  click() { if (!this.disabled) emit(this, 'click', { target: this }); }
}
function findAll(el, pred, out = []) { for (const c of el.children) { if (pred(c)) out.push(c); findAll(c, pred, out); } return out; }

class FakeUPlot {
  constructor(opts, data, host) {
    this.opts = opts; this.data = data; this.setSizes = 0; this.setDatas = 0; this.destroyed = false;
    if (!opts.width || !opts.height) throw new Error('uPlot created without a size');
    // the x axis must render a single line (the canvas is only a couple of rows tall)
    const labels = opts.axes[0].values(null, [Date.now() / 1000]);
    if (/\n|\s\d+\/\d+\/\d+/.test(labels[0])) throw new Error('x axis label is not one line: ' + labels[0]);
    this.root = new El('div'); host.appendChild(this.root); FakeUPlot.all.push(this);
  }
  setData(d) { this.data = d; this.setDatas++; }
  setSize() { this.setSizes++; }
  destroy() { this.destroyed = true; }
}
FakeUPlot.all = [];
FakeUPlot.paths = { stepped: (o) => { const f = () => null; f.stepped = o; return f; } };   // no spline on purpose

// A fresh page: its own DOM, stubs and stream, sharing `lsData` (the device's localStorage) between boots.
function boot(lsData) {
  const byId = new Proxy({}, { get: (t, k) => t[k] || (t[k] = new El('div')) });
  byId['#sheet'].hidden = true;
  const root = new El('html');
  const doc = {
    documentElement: root, hidden: false,
    querySelector: (s) => (s.startsWith('#') ? byId[s] : null),
    createElement: (t) => new El(t),
    createTextNode: (t) => { const e = new El('#text'); e.textContent = t; return e; },
    addEventListener: (t, f) => (listeners(doc)[t] = listeners(doc)[t] || []).push(f)
  };
  // page height = fixed non-chart content + every chart's height (what the browser would measure)
  const body = new El('body');
  Object.defineProperty(body, 'offsetHeight', { get: () => app.nonChart + app.cards().reduce((n, c) => n + c.querySelector('.chart').clientHeight, 0) });
  doc.body = body;
  const timers = [];
  let es = null;
  class FakeES { constructor() { es = this; this.readyState = 1; } addEventListener(t, f) { listeners(this)[t] = [f]; } close() {} }
  const win = {
    document: doc, uPlot: FakeUPlot,
    localStorage: { getItem: (k) => (k in lsData ? lsData[k] : null), setItem: (k, v) => { lsData[k] = String(v); }, removeItem: (k) => { delete lsData[k]; } },
    console: { error: (...a) => errors.push(a.map(String).join(' ')), log() {} },
    EventSource: FakeES, ResizeObserver: class { observe() {} }, isSecureContext: false,
    navigator: {}, location: { protocol: 'http:', reload() {} },
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    setInterval: () => 0, setTimeout: (f) => { timers.push(f); return timers.length; }, innerHeight: 700, innerWidth: 1280, fetch: () => Promise.resolve({ status: 200 }),
    requestAnimationFrame: (f) => f(), addEventListener: (t, f) => (listeners(win)[t] = listeners(win)[t] || []).push(f)
  };
  win.window = win;
  Object.defineProperty(root, 'dataset', { value: {} });
  vm.createContext(win);
  vm.runInContext(code, win, { filename: 'app.js' });
  if (!es) fail('app.js never opened the event stream');
  const app = {
    byId, root, win, grid: byId['#grid'], nonChart: 400,
    flush: () => { while (timers.length) timers.shift()(); },
    ch: () => ['cpu', 'memory', 'disk', 'network', 'tokens'].map((id) => app.card(id)).filter((c) => c && !c.hidden).map((c) => c.querySelector('.chart').clientHeight),
    send: (t, o) => emit(es, t, { data: JSON.stringify(o) }),
    rows: () => app.grid.children,
    cards: () => app.grid.children.flatMap((r) => r.children),
    card: (id) => app.cards().find((e) => e.dataset.id === id),
    layout: () => app.rows().map((r) => r.children.map((c) => c.dataset.id).join('+')).join(' | '),
    stored: () => JSON.parse(lsData['vitals.settings.v2'] || 'null'),
    all: (el, pred) => findAll(el, pred),
    change: (id, v) => { const e = byId[id]; if (typeof v === 'boolean') e.checked = v; else e.value = v; emit(e, 'change', {}); },
    openSheet: () => byId['#gear'].click(),
    // a button inside the sheet: by action and the dataset id of its nearest li / chip
    btn: (act, id) => {
      const within = (e) => { for (let p = e.parent; p; p = p.parent) if (p.dataset.id === id) return true; return false; };
      const hit = findAll(byId['#set-sections'], (e) => e.dataset.act === act && (!id || within(e))).concat(findAll(byId['#set-rows'], (e) => e.dataset.act === act && (!id || within(e))));
      if (!hit.length) fail('no sheet button ' + act + ' for ' + id);
      return hit[0];
    }
  };
  try {
    app.send('history', fixture.history);
    fixture.samples.forEach((s) => app.send('sample', s));
  } catch (e) { fail('exception while handling events: ' + e.stack); }
  if (errors.length) fail('console.error: ' + errors.join(' | '));
  return app;
}

// ---- 1. defaults ----
const ls = {};
const app = boot(ls);
if (app.cards().length !== 6) fail('expected 6 cards, got ' + app.cards().length);
if (app.root.attrs['data-density'] !== 'compact') fail('compact must be the default density, got ' + app.root.attrs['data-density']);
if (app.grid.attrs['data-layout'] !== 'stacked') fail('stacked must be the default layout');
if (app.rows().length !== 6 || app.rows().some((r) => r.children.length !== 1)) fail('stacked: one section per row, got ' + app.layout());
if (app.layout() !== 'cpu | memory | disk | network | tokens | claude') fail('stacked order: ' + app.layout());
for (const el of app.cards()) {
  const id = el.dataset.id;
  if (el.hidden) continue;
  const bars = el.querySelector('.bars'), sum = el.querySelector('.summary');
  if (/unavailable/.test(sum.textContent) && !fixture.allowUnavailable.includes(id)) fail(id + ' card shows ' + sum.textContent);
  if (!sum.textContent) fail(id + ' card: update never ran (empty summary)');
  if (!bars.children.some((k) => k.children.length) && !fixture.allowUnavailable.includes(id)) fail(id + ' card has no bars');
  if (bars.style.get('--lbw') == null || bars.style.get('--vw') == null) fail(id + ' bars have no fixed column widths');
}
const chartIds = ['cpu', 'memory', 'disk', 'network', 'tokens'];
const live = FakeUPlot.all.filter((u) => u.setDatas > 0).length;
if (live < chartIds.length) fail('only ' + live + ' charts received data, want ' + chartIds.length);

// ---- 2. square, stepped, flat ----
for (const m of css.matchAll(/border-radius\s*:\s*([^;}]+)/g)) if (!/^0(px)?\s*$/.test(m[1].trim())) fail('CSS has a rounded corner: border-radius: ' + m[1]);
if (/box-shadow\s*:\s*(?!none)/.test(css) || /gradient/.test(css) || /transition\s*:\s*(?!none)/.test(css)) fail('CSS has a shadow, gradient or transition');
if (/borderRadius|createLinearGradient|paths\.spline|transition|\.animate\(/.test(code.replace(/\/\/.*$/gm, ''))) fail('app.js sets a radius, gradient, spline or transition');
for (const u of FakeUPlot.all) {
  u.opts.series.slice(1).forEach((s) => {
    if (!s.paths || !s.paths.stepped || s.paths.stepped.align !== 1) fail('chart series is not stepped (align 1)');
    if (s.width !== 1) fail('chart line is not 1px');
    if (typeof s.fill !== 'string') fail('chart fill must be flat, not a gradient function');
  });
}

// ---- 3. CPU: one vertical column per thread, equal widths ----
const cpuCols = app.card('cpu').querySelector('.vcols');
const colsN = cpuCols.children.filter((c) => c.hasClass('vc'));
if (colsN.length !== fixture.samples[0].cpu.threads.length) fail('cpu: want one column per thread, got ' + colsN.length);
if (cpuCols.hasClass('fixed')) fail('cpu columns must be equal and fill the card, not fixed width');
const heights = colsN.map((c) => parseFloat(c.children[0].children[0].style.height));
if (!heights.some((h) => h > 0 && h <= 100)) fail('cpu column fills not set');
if (!/all \d+%/.test(app.card('cpu').querySelector('.head').textContent)) fail('cpu title lacks the `all` value');

// ---- 4. network: idle hidden, mirrored pairs, fixed widths, totals in the title ----
const netCells = () => app.card('network').querySelector('.vcols').children.filter((c) => c.hasClass('vc') && !c.hasClass('morecell'));
const netNames = () => netCells().map((c) => app.all(c, (e) => e.hasClass('vl'))[0].textContent);
const ifs = fixture.samples[fixture.samples.length - 1].network.interfaces;
const activeNames = ifs.filter((i) => i.down_bps > 0 || i.up_bps > 0).map((i) => i.name);
if (netNames().join() !== activeNames.join()) fail('network: idle interfaces must be hidden; shown ' + netNames() + ' want ' + activeNames);
if (!/^↓\S+ ↑\S+$/.test(app.card('network').querySelector('.head').textContent)) fail('network title lacks the totals: ' + app.card('network').querySelector('.head').textContent);
if (!netCells().every((c) => c.children[0].hasClass('up') && c.children[1].hasClass('dn'))) fail('network: pair is not up-above, down-below');
if (!app.card('network').querySelector('.vcols').hasClass('fixed')) fail('network columns must be fixed width');
app.openSheet();
app.change('#set-idle', true);
if (netNames().length !== 12) fail('network: idle shown should cap at 12 cells, got ' + netNames().length);
const more = () => app.all(app.card('network'), (e) => e.hasClass('more'))[0];
if (!more() || more().textContent !== '+' + (ifs.length - 12) + ' more') fail('network: missing `+N more`');
more().click();
if (netNames().length !== ifs.length || more().textContent !== 'show fewer') fail('network: expand failed');
more().click();
app.change('#set-idle', false);
if (netNames().length !== activeNames.length) fail('network: hiding idle again failed');
app.card('network').querySelector('.title').click();
if (netNames().length !== ifs.length) fail('network: zoom must show every interface');
app.card('network').querySelector('.zoomx').click();

// ---- 5. zoom ----
const before = FakeUPlot.all.reduce((n, u) => n + u.setSizes, 0);
const cpu = app.card('cpu');
cpu.querySelector('.title').click();
if (!cpu.className.includes('zoom')) fail('zoom class not set');
if (!cpu.querySelector('.vcols').children.length || !cpu.querySelector('.summary').textContent) fail('zoomed card lost its bars or summary');
if (FakeUPlot.all.reduce((n, u) => n + u.setSizes, 0) <= before) fail('zoom did not resize the charts');
cpu.querySelector('.zoomx').click();
if (cpu.className.includes('zoom')) fail('unzoom failed');
emit(app.win, 'resize');

// ---- 6. tokens, claude shapes ----
const tok = app.card('tokens');
if (app.all(tok, (e) => e.hasClass('vc')).length !== 4) fail('tokens: want 4 rate columns');
if (!app.all(tok, (e) => e.hasClass('strip'))[0].children.length) fail('tokens: no per-model share strip');
{
  // v8.5: a model with zero tokens is neither a strip segment nor a legend entry; a non-zero one is both.
  const z = JSON.parse(JSON.stringify(fixture.samples[fixture.samples.length - 1]));
  const z0 = { input: 0, cache_write: 0, cache_read: 0, output: 0 };
  const strip = () => app.all(tok, (e) => e.hasClass('strip'))[0], leg = () => app.all(tok, (e) => e.hasClass('lg'));
  z.tokens.by_model_today.fable = z0; app.send('sample', z);
  if (strip().children.length !== 2 || leg().length !== 2 || leg().some((l) => /fable/.test(l.children.map((c) => c.textContent).join('')))) fail('tokens: a zero-token model must be hidden');
  z.tokens.by_model_today.fable = { input: 10, cache_write: 0, cache_read: 0, output: 5 }; app.send('sample', z);
  if (strip().children.length !== 3 || !leg().some((l) => /fable/.test(l.children.map((c) => c.textContent).join('')))) fail('tokens: a non-zero model must show');
  app.send('sample', fixture.samples[fixture.samples.length - 1]);
}
const cl = app.card('claude');
if (!app.all(cl, (e) => e.hasClass('bar')).length) fail('claude: no rows');
if (!/\d+% \/ \d+%/.test(app.all(cl, (e) => e.hasClass('val'))[0].textContent)) fail('claude: values not on the row');
// day notches (v8.3): 6 per week row at k/7 of the bar, none on 5 Hour or Daily, evenly spaced without resets_at
{
  const rowOf = (a, label) => a.all(a.card('claude'), (e) => e.hasClass('bar')).find((b) => b.children[0].textContent === label);
  const notches = (row) => row.children[1].children.filter((e) => e.hasClass('notch')).map((e) => e.style.left);
  const want = [1, 2, 3, 4, 5, 6].map((k) => (k * 100 / 7) + '%');
  // v8.5: Fable is 0% in the fixture, so its row is not shown; it comes back as soon as it is above 0.
  if (rowOf(app, 'Week (Fable)')) fail('claude: a 0% model week must be hidden');
  for (const l of ['5 Hour', 'Daily', 'Weekly']) if (!rowOf(app, l)) fail('claude: ' + l + ' must always show');
  const lbw = () => app.card('claude').querySelector('.bars').style.get('--lbw');
  const lbw0 = lbw();
  {
    const up = JSON.parse(JSON.stringify(fixture.samples[fixture.samples.length - 1])); up.claude.models[0].percent = 3;
    app.send('sample', up);
    if (!rowOf(app, 'Week (Fable)')) fail('claude: a model week above 0% must show');
    if (lbw() !== lbw0) fail('claude: the label column moved when a row appeared');
    app.send('sample', fixture.samples[fixture.samples.length - 1]);
    if (rowOf(app, 'Week (Fable)')) fail('claude: the model week must hide again at 0%');
    app.send('sample', up);
  }
  for (const l of ['Weekly', 'Week (Fable)']) {
    const r = rowOf(app, l); if (!r) fail('claude: no row ' + l);
    if (JSON.stringify(notches(r)) !== JSON.stringify(want)) fail('day notches on ' + l + ': ' + notches(r));
  }
  for (const l of ['5 Hour', 'Daily']) { const r = rowOf(app, l); if (!r || notches(r).length) fail('claude: ' + l + ' must have no notches'); }
  const wk = JSON.parse(JSON.stringify(fixture.samples[fixture.samples.length - 1])); const c0 = wk.claude;
  delete c0.week.resets_at; delete c0.models[0].resets_at;
  wk.claude.models[0].percent = 3;
  app.send('sample', wk);
  for (const l of ['Weekly', 'Week (Fable)']) if (JSON.stringify(notches(rowOf(app, l))) !== JSON.stringify(want)) fail('fallback notches on ' + l);
  if (app.all(app.card('claude'), (e) => e.hasClass('notch')).length !== 12) fail('claude: notches were duplicated on update');
}
const hbarH = parseFloat((/--barh:\s*(\d+)px/.exec(css) || [])[1]);
if (!(hbarH <= 8)) fail('compact meters must be at most 8px thick');
if (!/--ch:\s*56px/.test(css)) fail('compact chart height must default to 56px');

// ---- 7. settings: density, section parts, chart height, storage v2 ----
app.change('#set-density', 'comfortable');
if (app.root.attrs['data-density'] !== 'comfortable' || app.stored().density !== 'comfortable') fail('density change not applied or stored');
app.change('#set-density', 'compact');
app.btn('h+', 'cpu').click();
if (app.stored().sec.cpu.h !== 64 || app.card('cpu').style.get('--ch') !== '64px') fail('chart height step: ' + JSON.stringify(app.stored().sec));
app.btn('toggle-line', 'disk').click();
if (app.stored().sec.disk.graph !== false || !app.card('disk').querySelector('.chart').hasClass('off')) fail('timeline toggle not applied');
app.btn('toggle-line', 'disk').click();

// ---- 8. custom layout editor ----
app.change('#set-layout', 'custom');
if (app.grid.attrs['data-layout'] !== 'custom' || app.layout() !== 'cpu | memory | disk | network | tokens | claude') fail('custom starts from what was on screen: ' + app.layout());
app.btn('up', 'memory').click();                     // memory into the row above, next to cpu
if (app.layout() !== 'cpu+memory | disk | network | tokens | claude') fail('move into previous row: ' + app.layout());
app.btn('w+', 'memory').click(); app.btn('w+', 'memory').click();
if (app.rows()[0].style.get('--cols') !== 'minmax(0,1fr) minmax(0,3fr)') fail('weights: ' + app.rows()[0].style.get('--cols'));
app.btn('down', 'tokens').click();                   // tokens into the next row (before claude)
if (app.layout() !== 'cpu+memory | disk | network | tokens+claude') fail('move into next row: ' + app.layout());
app.btn('left', 'claude').click();
if (app.layout() !== 'cpu+memory | disk | network | claude+tokens') fail('move within row: ' + app.layout());
app.byId['#add-row'].click();
if (app.stored().rows.length !== 5 || app.stored().rows[4].length !== 0) fail('add row: ' + JSON.stringify(app.stored().rows));
const rm = app.all(app.byId['#set-rows'], (e) => e.dataset.act === 'rmrow' && e.parent.parent.dataset.row === '4')[0];
rm.click();
if (app.stored().rows.length !== 4) fail('remove row: ' + JSON.stringify(app.stored().rows));
if (app.stored().layout !== 'custom') fail('layout not stored');
const want = 'cpu+memory | disk | network | claude+tokens';
if (app.layout() !== want) fail('custom layout: ' + app.layout());
// survives a reload (same localStorage, v2 key)
const again = boot(ls);
if (again.layout() !== want) fail('custom layout did not survive a reload: ' + again.layout());
if (again.rows()[0].style.get('--cols') !== 'minmax(0,1fr) minmax(0,3fr)') fail('weights lost on reload');
if (again.stored().sec.cpu.h !== 64) fail('chart height lost on reload');
// server rows mode
again.openSheet();
again.change('#set-layout', 'rows');
if (again.layout() !== 'cpu+memory | disk+network | tokens | claude') fail('server rows mode: ' + again.layout());
again.change('#set-layout', 'stacked');
again.byId['#reset-layout'].click();
if (again.layout() !== 'cpu | memory | disk | network | tokens | claude' || again.stored().layout !== undefined) fail('reset layout');

// ---- 9. v1 -> v2 migration ----
const ls1 = { 'vitals.settings.v1': JSON.stringify({ show: { memory: false }, order: ['claude', 'cpu'], win: 10, twin: '7d', theme: 'light', cols: 3, netAll: true }) };
const mig = boot(ls1);
const st = mig.stored();
if (!st || st.sec.memory.show !== false || st.win !== 10 || st.twin !== '7d' || st.theme !== 'light' || st.netAll !== true || st.order[0] !== 'claude') fail('v1 migration: ' + JSON.stringify(st));
if (mig.layout() !== 'claude | cpu | disk | network | tokens') fail('v1 migration layout (memory hidden, claude first): ' + mig.layout());
if (mig.card('memory')) fail('v1 migration: memory should stay hidden');
if (mig.root.attrs['data-density'] !== 'compact') fail('migrated settings must be compact');
// ---- 10. v8.1: auto chart height, clean x axis, no y axis in compact ----
const fitFrom = FakeUPlot.all.length;
const fit = boot({});
const fitU = () => FakeUPlot.all.slice(fitFrom).filter((u) => !u.destroyed);
const CHART_IDS = ['cpu', 'memory', 'disk', 'network', 'tokens'];
const chPx = (a, id) => { const v = a.card(id).style.get('--ch'); return v == null ? null : parseFloat(v); };
const autoSum = (a, ids) => ids.reduce((n, id) => n + chPx(a, id), 0);
const liveU = (a, label) => fitU().filter((u) => u.opts.series[1].label === label).pop();
fit.flush();
// spare = innerHeight 700 - 400 non-chart px = 300, shared by weight, each box within [40+16,160+16] (plot + x axis row)
if (autoSum(fit, CHART_IDS) !== 300) fail('auto: chart heights sum to ' + autoSum(fit, CHART_IDS) + ', want the spare 300');
for (const id of CHART_IDS) if (chPx(fit, id) < 56 || chPx(fit, id) > 176) fail('auto: ' + id + ' outside the clamp: ' + chPx(fit, id));
if (!(chPx(fit, 'memory') < chPx(fit, 'cpu'))) fail('auto: weights ignored (memory 0.8 vs cpu 1)');
// a manual height wins; the others share what is left
fit.openSheet();
fit.btn('h+', 'cpu').click(); fit.flush();
if (chPx(fit, 'cpu') !== 64) fail('manual height must win over auto: ' + chPx(fit, 'cpu'));
if (autoSum(fit, CHART_IDS.slice(1)) !== 300 - 64) fail('auto: others must share the spare minus the manual chart: ' + autoSum(fit, CHART_IDS.slice(1)));
const hval = (a, id) => a.all(a.byId['#set-sections'], (e) => e.dataset.act === 'h-value' && e.parent.parent.dataset.id === id)[0].textContent;
if (hval(fit, 'cpu') !== '64' || hval(fit, 'memory') !== 'auto') fail('settings must show auto as a state: ' + hval(fit, 'cpu') + ' / ' + hval(fit, 'memory'));
// clamps: too little room -> 40 each (the page scrolls); plenty -> 160 each
fit.win.innerHeight = 440; emit(fit.win, 'resize'); fit.flush();
if (!CHART_IDS.slice(1).every((id) => chPx(fit, id) === 56)) fail('auto: min clamp 56 not applied: ' + CHART_IDS.map((id) => chPx(fit, id)));
fit.win.innerHeight = 3000; emit(fit.win, 'resize'); fit.flush();
if (!CHART_IDS.slice(1).every((id) => chPx(fit, id) === 176)) fail('auto: max clamp 176 not applied: ' + CHART_IDS.map((id) => chPx(fit, id)));
// the way back to auto
fit.btn('h-auto', 'cpu').click(); fit.flush();
if (fit.stored().sec.cpu.h !== undefined || chPx(fit, 'cpu') !== 176 || hval(fit, 'cpu') !== 'auto') fail('auto button: ' + JSON.stringify(fit.stored().sec) + ' ' + chPx(fit, 'cpu'));
// a burst of triggers is one fit (debounced), and a fit with nothing changed writes nothing
const sets = FakeUPlot.all.reduce((n, u) => n + u.setSizes, 0);
fit.win.innerHeight = 700;
for (let i = 0; i < 20; i++) emit(fit.win, 'resize');
fit.flush(); fit.flush();
if (autoSum(fit, CHART_IDS) !== 300) fail('auto after resize burst: ' + autoSum(fit, CHART_IDS));
fit.flush();
if (FakeUPlot.all.reduce((n, u) => n + u.setSizes, 0) > sets + 5 * 21 + 5) fail('auto: layout thrash, too many chart resizes');

// x axis: increments match the window, and no two identical consecutive non-empty labels
const base = Math.floor(Date.now() / 60000) * 60;
const axisOf = (label) => liveU(fit, label).opts.axes[0];
const nonEmptyRepeat = (labels) => { const l = labels.filter(Boolean); return l.some((x, i) => i && x === l[i - 1]); };
let ax = axisOf('CPU');                                  // 5 minute window
if (!ax.incrs.every((i) => i >= 60 && i % 60 === 0)) fail('5 min window must tick on whole minutes: ' + ax.incrs);
const secTicks = Array.from({ length: 60 }, (_, i) => base + i * 5);      // 5 s ticks: 12 per minute
if (nonEmptyRepeat(ax.values(null, secTicks))) fail('5 min x axis repeats a label: ' + ax.values(null, secTicks).join(','));
if (ax.values(null, secTicks).filter(Boolean).length > 6) fail('5 min axis labels should be whole minutes');
if (ax.space(null, 0, 0, 1, 600) < 100) fail('compact x axis must allow at most ~6 labels per 600 px');
ax = axisOf('in');                                        // tokens, 24 h window
const dayTicks = Array.from({ length: 200 }, (_, i) => base + i * 20);    // 20 s ticks: far finer than a minute label
if (nonEmptyRepeat(ax.values(null, dayTicks))) fail('24 h x axis repeats a label: ' + ax.values(null, dayTicks).join(','));
if (!ax.incrs.every((i) => i >= 60)) fail('24 h window must not tick below a minute');
fit.change('#set-win', '2');                              // 2 minutes: seconds are allowed
ax = axisOf('CPU');
if (ax.incrs[0] !== 1 || !/^\d\d:\d\d:\d\d$/.test(ax.values(null, [base + 7])[0])) fail('2 min window should label seconds: ' + ax.incrs);
if (nonEmptyRepeat(ax.values(null, Array.from({ length: 40 }, (_, i) => base + i * 0.5)))) fail('2 min x axis repeats a label');
fit.change('#set-win', '5');

// compact: no y axis, the scale max as a small label in the corner; comfortable: a y axis with top room
if (!fitU().every((u) => u.opts.axes.length === 2 && u.opts.axes[0].scale !== 'y' && u.opts.axes.every((a) => !(a.scale === 'y' && a.show !== false)))) fail('compact charts: one shown x axis and every y axis show:false');
if (!fitU().every((u) => u.opts.axes.filter((a) => a.show !== false).length === 1)) fail('compact charts must draw exactly one axis (x): ' + JSON.stringify(fitU().map((u) => u.opts.axes.map((a) => a.show))));
if (!/AUTO_MIN = 40/.test(code)) fail('auto floor must be 40');
// v8.4: x labels are never dropped by the auto layout, at any width; canvas = plot floor (40) + axis row (16)
{
  if (liveU(fit, 'CPU').opts.axes[0].show === false) fail('compact chart must keep its x labels');
  for (const w of [520, 1280]) {
    fit.win.innerWidth = w; fit.win.innerHeight = 440; emit(fit.win, 'resize'); fit.flush();   // every auto chart at its floor
    const live = fitU();
    if (!live.every((u) => u.opts.axes[0].show !== false)) fail('x labels dropped at ' + w + ' px with auto heights');
    if (!live.every((u) => u.opts.height >= 40 + 16)) fail('canvas must be plot floor + axis row at ' + w + ': ' + live.map((u) => u.opts.height));
  }
  fit.win.innerHeight = 700; emit(fit.win, 'resize'); fit.flush();
}
// Disk: mount label column = longest mount name (+1ch, max 16ch); wrap instead of squeezing
{
  const hbs = fit.all(fit.card('disk'), (e) => e.hasClass('hb'));
  const mh = hbs[0];
  if (mh.style.get('--lbw') !== (Math.min(16, '/boot/efi'.length + 1)) + 'ch') fail('disk mount label width: ' + mh.style.get('--lbw'));
  if (!/\.bars\.drow\.wrap\b/.test(css) || !/minmax\(60px, 1fr\)/.test(css)) fail('disk wrap rule / 60px tracks missing in css');
  if (!/Math\.min\(16,/.test(code)) fail('disk label width must cap at 16ch');
}
const maxOf = (id) => fit.card(id).querySelector('.cmax');
if (!maxOf('cpu') || maxOf('cpu').textContent !== '100%' || maxOf('memory').textContent !== '100%') fail('compact pct charts need a `100%` max label');
if (!/\/s$/.test(maxOf('network').textContent) || !/\/s$/.test(maxOf('disk').textContent)) fail('compact rate charts need a max label like 1.2M/s: ' + maxOf('network').textContent);
if (!/^\.cmax\b|\n\.cmax\b|\.cmax \{[^}]*position: absolute[^}]*top: 0[^}]*left/.test(css)) fail('.cmax must sit in the top-left corner');
fit.change('#set-density', 'comfortable'); fit.flush();
if (!fitU().every((u) => u.opts.axes.length === 2 && u.opts.padding[0] >= 12)) fail('comfortable charts need a y axis with >= 12 px top padding');
if (maxOf('cpu')) fail('comfortable charts have no corner max label');
if (CHART_IDS.some((id) => fit.card(id).style.get('--ch') != null)) fail('comfortable keeps fixed heights (no auto)');
if (hval(fit, 'cpu') !== '120') fail('comfortable shows the fixed height: ' + hval(fit, 'cpu'));
fit.change('#set-density', 'compact');

if (errors.length) fail('console.error: ' + errors.join(' | '));
console.log('SMOKE PASS (' + app.cards().length + ' cards, ' + live + ' charts fed, compact+stacked default, stepped square charts, ' + colsN.length + ' cpu columns, idle hidden, custom rows + v2 storage + v1 migration, auto chart height, clean axes ok)');
