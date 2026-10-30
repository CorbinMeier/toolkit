// Headless driver for the reorder logic in web/rules.js: no browser, no display.
// Usage: node rules_move_driver.js <path to rules.js>; prints {"failures": [...], "checks": n}.
// Part 1 exercises the pure window.ncRulesMove functions; part 2 mounts the real rules list in a
// minimal DOM stand-in and drives the handle's real keydown / drop handlers, asserting the
// bridge message each one sends.
"use strict";
const fs = require("fs");
const vm = require("vm");

let checks = 0;
const failures = [];
function eq(actual, expected, what) {
  checks++;
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    failures.push(what + ": expected " + JSON.stringify(expected) + ", got " + JSON.stringify(actual));
  }
}

// ---- minimal DOM ----------------------------------------------------------------------
const timers = [];
let activeElement = null;
let docRoot = null;
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attrs = {};
    this.listeners = {};
    this.classSet = new Set();
    this._text = "";
    this.scrollTop = 0;
    const self = this;
    this.classList = {
      add: (c) => self.classSet.add(c),
      remove: (...cs) => cs.forEach((c) => self.classSet.delete(c)),
      contains: (c) => self.classSet.has(c),
      toggle: (c, on) => ((on === undefined ? !self.classSet.has(c) : on) ? self.classSet.add(c) : self.classSet.delete(c)),
    };
  }
  set className(v) { this.classSet = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return Array.from(this.classSet).join(" "); }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  get isConnected() { let e = this; while (e.parentNode) e = e.parentNode; return e === docRoot; }
  focus() { activeElement = this; }
  getBoundingClientRect() { return this._rect || { top: 0, height: 0 }; }
  fire(type, ev) {
    ev = Object.assign({ prevented: false, preventDefault() { this.prevented = true; } }, ev);
    (this.listeners[type] || []).forEach((fn) => fn(ev));
    return ev;
  }
  walk(fn) { this.children.forEach((c) => { fn(c); c.walk(fn); }); }
  all(cls) { const out = []; this.walk((c) => { if (c.classSet.has(cls)) out.push(c); }); return out; }
  // only the one selector rules.js uses: .rl-row[data-index='N'] .rl-handle
  querySelector(sel) {
    const m = /^\.rl-row\[data-index='(\d+)'\] \.rl-handle$/.exec(sel);
    if (!m) throw new Error("unsupported selector " + sel);
    const row = this.all("rl-row").find((r) => r.getAttribute("data-index") === m[1]);
    return row ? row.all("rl-handle")[0] || null : null;
  }
}
docRoot = new El("body");
const scroller = docRoot.appendChild(new El("div"));
const mountRoot = scroller.appendChild(new El("div"));

const sent = [];
let rules = [];
function rulesReply() { return { ok: true, rules: rules.map((n) => ({ name: n, enabled: true, summary: "" })), editable: true, path: "/x/rules.toml" }; }
const kit = {
  el(tag, cls, text) { const e = new El(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; },
  btn(label, cls, fn) { const e = kit.el("button", cls, label); e.addEventListener("click", fn); return e; },
  ask(msg) {
    sent.push(msg);
    if (msg.type === "rules_move") {
      const [r] = rules.splice(msg.index, 1);
      rules.splice(msg.to, 0, r);
    }
    return Promise.resolve(rulesReply());
  },
  errText: (r) => String((r && r.error) || "error"),
  onRulesTab: () => true,
};
const sandbox = {
  document: { createElement: (t) => new El(t), createTextNode: (t) => { const e = new El("#text"); e.textContent = t; return e; } },
  window: { ncSettingsKit: kit, setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {} },
  JSON, Object, Array, String, Number, Math, Promise, console,
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), sandbox);
const M = sandbox.window.ncRulesMove;

// ---- targetIndex: every key, the edges, no-ops ----------------------------------------------
const T = (k, i, n) => M.targetIndex(k, i, n);
eq(T("ArrowUp", 2, 5), 1, "up in the middle");
eq(T("ArrowDown", 2, 5), 3, "down in the middle");
eq(T("ArrowUp", 0, 5), -1, "up on the first row is a no-op");
eq(T("ArrowDown", 4, 5), -1, "down on the last row is a no-op");
eq(T("ArrowUp", 1, 5), 0, "up to the first row");
eq(T("ArrowDown", 3, 5), 4, "down to the last row");
eq(T("Home", 3, 5), 0, "Home goes to the top");
eq(T("Home", 0, 5), -1, "Home on the first row is a no-op");
eq(T("End", 1, 5), 4, "End goes to the bottom");
eq(T("End", 4, 5), -1, "End on the last row is a no-op");
eq(T("Home", 1, 5), 0, "Home from the second row");
eq(T("End", 3, 5), 4, "End from the second to last row");
for (const k of ["Enter", " ", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Tab", "Escape", "a", "", undefined]) {
  eq(T(k, 2, 5), -1, "not a move key: " + String(k));
}
eq(T("ArrowUp", 0, 1), -1, "single rule: up");
eq(T("ArrowDown", 0, 1), -1, "single rule: down");
eq(T("Home", 0, 1), -1, "single rule: Home");
eq(T("End", 0, 1), -1, "single rule: End");
eq(T("ArrowDown", 0, 2), 1, "two rules: down");
eq(T("ArrowUp", 1, 2), 0, "two rules: up");
eq(T("ArrowDown", 5, 5), -1, "index past the end");
eq(T("ArrowUp", 5, 5), 4, "up from past the end lands inside, as before");
eq(T("End", 0, 0), -1, "empty list");

// ---- dropIndex: before/after self and neighbours --------------------------------------------
const D = (f, o, b, n) => M.dropIndex(f, o, b, n);
eq(D(2, 2, true, 5), -1, "before itself is a no-op");
eq(D(2, 2, false, 5), -1, "after itself is a no-op");
eq(D(2, 3, true, 5), -1, "before the next row (= after itself) is a no-op");
eq(D(2, 1, false, 5), -1, "after the previous row (= before itself) is a no-op");
eq(D(2, 3, false, 5), 3, "after the next row");
eq(D(2, 1, true, 5), 1, "before the previous row");
eq(D(0, 4, false, 5), 4, "first row dropped after the last");
eq(D(0, 4, true, 5), 3, "first row dropped before the last");
eq(D(4, 0, true, 5), 0, "last row dropped before the first");
eq(D(4, 0, false, 5), 1, "last row dropped after the first");
eq(D(0, 1, false, 5), 1, "down one");
eq(D(1, 0, true, 5), 0, "up one to the top");
eq(D(3, 0, true, 5), 0, "to the top from the middle");
eq(D(1, 4, false, 5), 4, "to the bottom from the middle");
eq(D(1, 3, true, 5), 2, "before row 3 moving down lands at 2");
eq(D(3, 1, false, 5), 2, "after row 1 moving up lands at 2");
eq(D(0, 1, true, 2), -1, "two rules: before the other = stay");
eq(D(0, 1, false, 2), 1, "two rules: after the other");
eq(D(0, 0, false, 1), -1, "single rule");
eq(D(-1, 1, true, 5), -1, "no drag in progress");
eq(D(1, 9, true, 5), -1, "drop target out of range");
eq(D(1, -1, true, 5), -1, "drop target negative");

// ---- focusIndex ---------------------------------------------------------------------------
eq(M.focusIndex(true, 3), 3, "keyboard move focuses the moved row's new index");
eq(M.focusIndex(true, 0), 0, "keyboard move to the top focuses row 0");
eq(M.focusIndex(false, 3), -1, "a drop leaves focus alone");

// ---- the real handlers ----------------------------------------------------------------------
async function flush() { for (let i = 0; i < 6; i++) await Promise.resolve(); }
function handles() { return mountRoot.all("rl-handle"); }
function names() { return mountRoot.all("rl-name").map((e) => e.textContent); }

async function main() {
  rules = ["a", "b", "c", "d"];
  sandbox.window.ncRules.render(mountRoot);
  await flush();
  eq(sent.map((m) => m.type), ["rules_get"], "the list loads first");
  eq(names(), ["a", "b", "c", "d"], "rows drawn");
  eq(handles().length, 4, "one handle per row");

  async function key(i, k) {
    sent.length = 0;
    const ev = handles()[i].fire("keydown", { key: k });
    await flush();
    return ev;
  }
  let ev = await key(1, "ArrowDown");
  eq(sent, [{ type: "rules_move", index: 1, to: 2, expect: "b" }], "ArrowDown sends rules_move");
  eq(ev.prevented, true, "ArrowDown is consumed");
  eq(names(), ["a", "c", "b", "d"], "list redrawn in the new order");
  activeElement = null;
  timers.splice(0).forEach((fn) => fn());
  eq(activeElement === handles()[2], true, "focus lands on the moved row's handle");

  ev = await key(2, "ArrowUp");
  eq(sent, [{ type: "rules_move", index: 2, to: 1, expect: "b" }], "ArrowUp sends rules_move");
  ev = await key(1, "Home");
  eq(sent, [{ type: "rules_move", index: 1, to: 0, expect: "b" }], "Home sends rules_move to 0");
  eq(names(), ["b", "a", "c", "d"], "order after Home");
  ev = await key(0, "End");
  eq(sent, [{ type: "rules_move", index: 0, to: 3, expect: "b" }], "End sends rules_move to the last index");
  eq(names(), ["a", "c", "d", "b"], "order after End");

  for (const [i, k] of [[0, "ArrowUp"], [3, "ArrowDown"], [0, "Home"], [3, "End"], [1, "Enter"], [1, "ArrowLeft"]]) {
    ev = await key(i, k);
    eq([sent.length, ev.prevented], [0, false], "no message and not consumed: " + k + " on row " + i);
  }

  // drag and drop through the real dragstart / dragover / drop handlers
  const rows = mountRoot.all("rl-row");
  rows.forEach((r, i) => { r._rect = { top: i * 40, height: 40 }; });
  function drag(from, over, y) {
    sent.length = 0;
    const rs = mountRoot.all("rl-row");
    rs.forEach((r, i) => { r._rect = { top: i * 40, height: 40 }; });
    handles()[from].fire("dragstart", { dataTransfer: null });
    rs[over].fire("dragover", { clientY: y });
    const ev = rs[over].fire("drop", { clientY: y });
    return ev;
  }
  rules = ["a", "b", "c", "d"];
  sent.length = 0;
  sandbox.window.ncRules.reset();
  sandbox.window.ncRules.render(mountRoot);
  await flush();
  mountRoot.all("rl-row").forEach((r, i) => { r._rect = { top: i * 40, height: 40 }; });
  drag(0, 2, 110); // lower half of row 2 (spans 80-120) -> after it
  await flush();
  eq(sent, [{ type: "rules_move", index: 0, to: 2, expect: "a" }], "drop after row 2 from row 0");
  eq(names(), ["b", "c", "a", "d"], "order after the drop");
  mountRoot.all("rl-row").forEach((r, i) => { r._rect = { top: i * 40, height: 40 }; });
  drag(3, 0, 5); // upper half of row 0 -> before it
  await flush();
  eq(sent, [{ type: "rules_move", index: 3, to: 0, expect: "d" }], "drop before row 0 from row 3");
  mountRoot.all("rl-row").forEach((r, i) => { r._rect = { top: i * 40, height: 40 }; });
  drag(1, 1, 45); // upper half of itself
  await flush();
  eq(sent, [], "drop on itself sends nothing");
  drag(1, 2, 85); // upper half of the next row = right where it is
  await flush();
  eq(sent, [], "drop just below itself (upper half of next) sends nothing");
  timers.length = 0; activeElement = null;
  drag(0, 3, 150);
  await flush();
  timers.splice(0).forEach((fn) => fn());
  eq(activeElement === null, true, "a drop never moves focus");
}

main().then(() => {
  console.log(JSON.stringify({ failures, checks }));
}, (e) => {
  failures.push("driver crashed: " + (e && e.stack || e));
  console.log(JSON.stringify({ failures, checks }));
});
