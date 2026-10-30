// Headless driver for the size reporter in web/shade.js: a permissive DOM stand-in, no browser,
// no display. Usage: node shade_size_driver.js <path to shade.js>;
// prints {"failures": [...], "checks": n}.
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

// Any element: every unknown property is a callable no-op that returns another such stub.
let box = { left: 20, top: 20, right: 420, bottom: 220, width: 400, height: 200 };
const ids = {};
function stub(id) {
  const own = { style: { setProperty() {} }, classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, children: [] };
  const fn = function () { return stub(); };
  return new Proxy(fn, {
    get(_, k) {
      if (k === "getBoundingClientRect") return () => (id === "shade" ? box : { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 });
      if (k in own) return own[k];
      if (k === Symbol.toPrimitive) return () => "";
      return stub();
    },
    set(_, k, v) { own[k] = v; return true; },
  });
}
const document = {
  getElementById: (id) => (ids[id] = ids[id] || stub(id)),
  createElement: () => stub(),
  addEventListener() {},
  head: stub(),
  documentElement: stub(),
};
const posted = [];
let observed = null;
const timers = [];
const sandbox = {
  document,
  window: { webkit: { messageHandlers: { nc: { postMessage: (m) => posted.push(m) } } }, ncReply: { deliver() {} }, ncImage: { deliver() {} } },
  ResizeObserver: function (fn) { this.observe = () => { observed = fn; }; },
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: () => {},
  Array, Map, Math, String, Number, Date, JSON, Object, Promise, console,
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), sandbox);
const nc = sandbox.window.nc;
const sizes = () => posted.filter((m) => m.type === "size");

eq(typeof nc.measure, "function", "window.nc.measure exists");

// The observer reports the first size.
observed();
eq(sizes().map((m) => [m.width, m.height]), [[440, 240]], "first report");

// An unchanged size is not re-sent: this is why a dropped report was never repeated.
observed();
eq(sizes().length, 1, "unchanged size is not re-sent by the observer");

// measure() forgets what was reported and sends again, at once, even when nothing changed.
nc.measure();
eq(sizes().map((m) => [m.width, m.height]), [[440, 240], [440, 240]], "measure re-sends an unchanged size");
eq(timers.length, 0, "measure does not wait for a settle timer");

// Growing is reported at once, shrinking waits for the settle timer.
box = { left: 20, top: 20, right: 420, bottom: 520, width: 400, height: 500 };
observed();
eq(sizes()[2].height, 540, "grow is sent at once");
box = { left: 20, top: 20, right: 420, bottom: 120, width: 400, height: 100 };
observed();
eq(sizes().length, 3, "shrink is held back");
eq(timers.length, 1, "shrink arms the settle timer");
timers[0]();
eq(sizes()[3].height, 140, "shrink is sent after the settle");

// After a shrink-sized measure the fresh size wins immediately, not after 220 ms.
box = { left: 20, top: 20, right: 420, bottom: 70, width: 400, height: 50 };
nc.measure();
eq(sizes()[4].height, 90, "measure reports a smaller size at once");

console.log(JSON.stringify({ failures, checks }));
