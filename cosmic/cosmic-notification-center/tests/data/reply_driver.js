// Headless driver for web/reply.js: a minimal DOM stand-in, no browser, no display.
// Usage: node reply_driver.js <path to reply.js>; prints {"failures": [...], "checks": n}.
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
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parent = null;
    this.attrs = {};
    this.listeners = {};
    this.classSet = new Set();
    this._text = "";
    this.hidden = false;
    this.disabled = false;
    this.readOnly = false;
    this.value = "";
    this.title = "";
    this.type = "";
    this.rows = 0;
    const self = this;
    this.classList = {
      add: (c) => self.classSet.add(c),
      remove: (c) => self.classSet.delete(c),
      contains: (c) => self.classSet.has(c),
      toggle: (c, on) => (on === undefined ? !self.classSet.has(c) : on) ? self.classSet.add(c) : self.classSet.delete(c),
    };
  }
  set className(v) { this.classSet = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return Array.from(this.classSet).join(" "); }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  get isConnected() { let e = this; while (e.parent) e = e.parent; return e === docRoot; }
  focus() { if (activeElement !== this) { activeElement = this; this.fire("focus", {}); } }
  blur() { if (activeElement === this) { activeElement = null; this.fire("blur", {}); } }
  setSelectionRange() {}
  fire(type, ev) {
    ev = Object.assign({ stopped: false, prevented: false,
      stopPropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; } }, ev);
    (this.listeners[type] || []).forEach((fn) => fn(ev));
    return ev;
  }
  all(sel) { // class selector only
    const out = [];
    const walk = (e) => { e.children.forEach((c) => { if (c.classSet.has(sel)) out.push(c); walk(c); }); };
    walk(this);
    return out;
  }
  one(sel) { return this.all(sel)[0] || null; }
}
const docRoot = new El("body");
const document = {
  createElement: (t) => new El(t),
  get activeElement() { return activeElement; },
};
const sandbox = {
  document,
  window: {},
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: () => {},
  Array, Map, Math, String, Number, Date, JSON, Object, console,
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), sandbox);
const R = sandbox.window.ncReply;

function reply(over) {
  return Object.assign({
    choices: [{ id: "yes", label: "Yes" }, { id: "no", label: "No" }],
    text: { placeholder: "Say…", max: 10 },
    host: "127.0.0.1:4320", method: "POST", allowed: true, reason: null,
    confirm: false, answered: null, error: null,
  }, over || {});
}
let nextId = 100;
function mount(over, opts) {
  const id = nextId++;
  const posted = [], focus = [], sent = [];
  const root = R.build({ id: id, reply: reply(over) }, Object.assign({
    post: (m) => posted.push(m), onFocus: (f) => focus.push(f), onSent: (i) => sent.push(i),
  }, opts || {}));
  if (root) docRoot.appendChild(root);
  return { id, root, posted, focus, sent };
}
const status = (m) => m.root.one("nc-reply-status");
const vis = (e) => e && !e.hidden;

// ---- describe: the rendering data ------------------------------------------------------
const AT = 1704117720; // 2024-01-01 14:02:00 UTC (the test sets TZ=UTC)
eq(R.describe(reply({ answered: { choice: "yes", at: AT, status: 200 } })).status, "Answered: Yes · 14:02 (200)", "answered choice by label");
eq(R.describe(reply({ answered: { choice: "zzz", at: AT, status: 200 } })).status, "Answered: zzz · 14:02 (200)", "answered choice id fallback");
eq(R.describe(reply({ answered: { text: "hello\n  world", at: AT, status: 204 } })).status, "Answered: hello world · 14:02 (204)", "answered text collapsed");
eq(R.describe(reply({ answered: { text: "x".repeat(60), at: AT, status: 200 } })).status.length < 70, true, "long answer truncated");
eq(R.describe(reply({ answered: { choice: "yes", status: 500, at: AT } })).kind, "error", "500 is an error kind");
eq(R.describe(reply({ answered: { choice: "yes", status: 200, at: AT } })).kind, "answered", "200 is answered");
eq(R.describe(reply({ answered: { choice: "yes", status: 200, at: AT } })).inputs, false, "answered hides inputs");
eq(R.describe(reply({ answered: { choice: "yes", at: AT } })).status, "Answered: Yes · 14:02", "no status: no parens");
eq(R.describe(reply({ allowed: false, reason: "host not allowed" })), { inputs: true, disabled: true, busy: false, confirm: false, confirmText: "", status: "host not allowed", kind: "disabled" }, "not allowed");
eq(R.describe(reply({ allowed: false })).status, "Not allowed", "not allowed default text");
eq(R.describe(reply(), { state: "sent" }).status, "Sent ✓", "sent");
eq(R.describe(reply(), { state: "refused", reason: "rate limited" }).status, "Refused: rate limited", "refused");
eq(R.describe(reply(), { state: "refused", reason: "rate limited" }).inputs, true, "refused keeps inputs for a retry");
eq(R.describe(reply(), { state: "confirm" }).confirmText, "Confirm sending to 127.0.0.1:4320?", "confirm text");
eq(R.describe(reply({ error: "HTTP 500" })).status, "Error: HTTP 500", "error");
eq(R.describe(reply(), { state: "sending" }).busy, true, "sending");
eq(R.describe(reply({ answered: { choice: "yes", at: AT, status: 200 } }), { state: "confirm" }).confirm, false, "answered beats a stale confirm");
eq(R.describe(reply()).kind, "", "idle has no status");

// ---- applyResult ----------------------------------------------------------------------------
let d = { state: "sending", text: "abc", token: "", reason: "" };
eq(R.applyResult(d, { kind: "text", status: "sent" }), true, "sent returns true");
eq([d.state, d.text], ["sent", ""], "sent clears the draft");
d = { state: "sending", text: "abc", token: "", reason: "" };
R.applyResult(d, { kind: "text", status: "pending_confirm", token: "t_1" });
eq([d.state, d.token, d.text], ["confirm", "t_1", "abc"], "pending_confirm keeps the text");
R.applyResult(d, { kind: "text", status: "pending_confirm" });
eq(d.state, "refused", "pending_confirm without a token is a refusal");
R.applyResult(d, { kind: "choice", status: "refused", reason: "nope" });
eq([d.state, d.reason], ["refused", "nope"], "refused");
d.state = "idle";
R.applyResult(d, { kind: "cancel", status: "cancelled" });
eq(d.state, "idle", "cancel result ignored");

// ---- building ---------------------------------------------------------------------------
eq(R.build({ id: 1, reply: null }, { post() {} }), null, "no reply: nothing");
eq(R.build({ id: 1 }, { post() {} }), null, "no reply key: nothing");
eq(R.build({ id: 1, reply: reply({ choices: [], text: null }) }, { post() {} }), null, "empty reply: nothing");
eq(R.build({ id: 1, reply: reply() }, {}), null, "no post fn: nothing");
eq(vis(R.build({ id: 9, reply: reply({ choices: [], text: null, error: "boom" }) }, { post() {} })), true, "error-only reply still shows");

let m = mount();
eq(m.root.className, "nc-reply", "root class");
eq(m.root.getAttribute("data-state"), "idle", "idle state");
eq(m.root.all("nc-reply-choice").map((b) => b.textContent), ["Yes", "No"], "choice buttons");
eq(m.root.all("nc-reply-choice").map((b) => b.getAttribute("data-choice")), ["yes", "no"], "choice ids");
eq(m.root.one("nc-reply-choice").title, "Sends POST to 127.0.0.1:4320", "tooltip on choices");
eq(m.root.one("nc-reply-send").title, "Sends POST to 127.0.0.1:4320", "tooltip on send");
eq(m.root.one("nc-reply-host").textContent, "→ 127.0.0.1:4320", "host label");
const ta = m.root.one("nc-reply-text");
eq(ta.tagName, "TEXTAREA", "textarea");
eq(ta.getAttribute("placeholder"), "Say…", "placeholder");
eq(m.root.one("nc-reply-count").textContent, "0/10", "counter");
eq(m.root.one("nc-reply-send").disabled, true, "send disabled when empty");
eq(vis(m.root.one("nc-reply-confirm")), false, "confirm bar hidden");
eq(vis(status(m)), false, "no status at rest");

ta.value = "hello";
ta.fire("input", {});
eq(m.root.one("nc-reply-count").textContent, "5/10", "counter follows input");
eq(m.root.one("nc-reply-send").disabled, false, "send enabled");
ta.value = "hello world!";
ta.fire("input", {});
eq(m.root.one("nc-reply-count").classList.contains("over"), true, "counter over");
eq(m.root.one("nc-reply-send").disabled, true, "send disabled over the limit");
let ev = ta.fire("keydown", { key: "Enter" });
eq(m.posted, [], "Enter over the limit sends nothing");
eq(ev.stopped, true, "keydown never reaches page shortcuts");
ta.value = "😀😀😀😀😀😀😀😀😀😀"; // 10 code points, 20 UTF-16 units
ta.fire("input", {});
eq(m.root.one("nc-reply-count").textContent, "10/10", "counts code points like Python");
eq(m.root.one("nc-reply-send").disabled, false, "10 code points fit max 10");

ta.value = "hi\nthere";
ta.fire("input", {});
ev = ta.fire("keydown", { key: "Enter", shiftKey: true });
eq([m.posted.length, ev.prevented], [0, false], "Shift+Enter is a newline");
ev = ta.fire("keydown", { key: "Enter", isComposing: true });
eq(m.posted.length, 0, "Enter during IME composition sends nothing");
ev = ta.fire("keydown", { key: "Enter" });
eq(ev.prevented, true, "Enter is consumed");
eq(m.posted, [{ type: "reply_text", id: m.id, text: "hi\nthere" }], "Enter sends the text");
eq(m.root.getAttribute("data-state"), "busy", "busy while sending");
eq(status(m).textContent, "Sending…", "sending text");
eq(ta.readOnly, true, "box read-only while sending");
ta.fire("keydown", { key: "Enter" });
eq(m.posted.length, 1, "no double send while busy");

R.deliver({ id: m.id, kind: "text", status: "pending_confirm", token: "tok-1" });
eq(m.root.getAttribute("data-state"), "confirm", "confirm state");
eq(vis(m.root.one("nc-reply-confirm")), true, "confirm bar shown");
eq(m.root.one("nc-reply-confirm-text").textContent, "Confirm sending to 127.0.0.1:4320?", "confirm prompt");
eq(vis(m.root.one("nc-reply-compose")), false, "inputs hidden while confirming");
m.root.one("nc-reply-confirm-yes").fire("click", {});
eq(m.posted[1], { type: "reply_confirm", id: m.id, token: "tok-1" }, "confirm posts the token");
R.deliver({ id: m.id, kind: "confirm", status: "sent" });
eq(status(m).textContent, "Sent ✓", "sent after confirm");
eq(m.root.getAttribute("data-state"), "sent", "sent state");
eq(m.sent, [m.id], "onSent fired once");
eq(ta.value, "", "box cleared after a send");
eq(vis(m.root.one("nc-reply-compose")), false, "inputs hidden after sending");

m = mount();
m.root.all("nc-reply-choice")[1].fire("click", {});
eq(m.posted, [{ type: "reply_choice", id: m.id, choice: "no" }], "choice click posts");
eq(m.root.all("nc-reply-choice").every((b) => b.disabled), true, "choices disabled while sending");
R.deliver({ id: m.id, kind: "choice", status: "pending_confirm", token: "abc" });
m.root.one("nc-reply-confirm-no").fire("click", {});
eq(m.posted[1], { type: "reply_cancel", id: m.id, token: "abc" }, "cancel posts the token");
eq(m.root.getAttribute("data-state"), "idle", "cancel returns to idle");
eq(vis(m.root.one("nc-reply-choices")), true, "choices back after cancel");
m.root.all("nc-reply-choice")[0].fire("click", {});
R.deliver({ id: m.id, kind: "choice", status: "refused", reason: "rate limited" });
eq(status(m).textContent, "Refused: rate limited", "refusal shown");
eq(m.root.getAttribute("data-state"), "refused", "refused state");
eq(m.root.all("nc-reply-choice").every((b) => !b.disabled), true, "retry possible after a refusal");
eq(m.sent, [], "refusal is not a send");

m = mount({ allowed: false, reason: "host not allowed" });
eq(m.root.getAttribute("data-state"), "disabled", "disabled state");
eq(m.root.all("nc-reply-choice").every((b) => b.disabled), true, "choices disabled");
eq(m.root.one("nc-reply-text").disabled, true, "box disabled");
eq(m.root.one("nc-reply-send").disabled, true, "send disabled");
eq(status(m).textContent, "host not allowed", "reason shown");
m.root.all("nc-reply-choice")[0].fire("click", {});
m.root.one("nc-reply-text").fire("keydown", { key: "Enter" });
eq(m.posted, [], "nothing is posted while disabled");

m = mount({ answered: { choice: "yes", at: AT, status: 200 } });
eq(status(m).textContent, "Answered: Yes · 14:02 (200)", "answered line");
eq(vis(m.root.one("nc-reply-choices")), false, "answered hides choices");
eq(vis(m.root.one("nc-reply-compose")), false, "answered hides the box");
eq(vis(m.root.one("nc-reply-host")), false, "answered hides the host label");

m = mount({ error: "HTTP 500" });
eq(status(m).textContent, "Error: HTTP 500", "error line");
eq(vis(m.root.one("nc-reply-choices")), true, "error keeps inputs");

// focus reporting and the rebuild guard
m = mount();
const ta2 = m.root.one("nc-reply-text");
ta2.focus();
eq(m.focus, [true], "focus reported");
ta2.blur();
eq(m.focus, [true, false], "blur reported");
ta2.focus();
docRoot.children.splice(docRoot.children.indexOf(m.root), 1);
m.root.parent = null;
ta2.blur();
eq(m.focus, [true, false, true], "a blur caused by the page replacing the block is not reported");

// a rebuild keeps the draft, the pending confirmation and the caret
m = mount();
const first = m.root;
first.one("nc-reply-text").value = "draft text";
first.one("nc-reply-text").fire("input", {});
first.one("nc-reply-text").focus();
const again = R.build({ id: m.id, reply: reply() }, { post: (x) => m.posted.push(x), onFocus: () => {}, onSent: () => {} });
docRoot.appendChild(again);
docRoot.children.splice(docRoot.children.indexOf(first), 1);
first.parent = null;
eq(again.one("nc-reply-text").value, "draft text", "draft survives a rebuild");
timers.splice(0).forEach((fn) => fn());
eq(activeElement === again.one("nc-reply-text"), true, "focus returns to the rebuilt box");
R.deliver({ id: m.id, kind: "text", status: "pending_confirm", token: "t9" });
const third = R.build({ id: m.id, reply: reply() }, { post() {}, onFocus() {}, onSent() {} });
eq(third.getAttribute("data-state"), "confirm", "pending confirmation survives a rebuild");
const fourth = R.build({ id: m.id, reply: reply({ answered: { choice: "yes", at: AT, status: 200 } }) }, { post() {}, onFocus() {}, onSent() {} });
eq(fourth.getAttribute("data-state"), "answered", "answered beats the local state");

// a result for a block that is gone is kept for the next build
const gone = nextId++;
R.deliver({ id: gone, kind: "choice", status: "sent" });
const back = R.build({ id: gone, reply: reply() }, { post() {} });
eq(back.getAttribute("data-state"), "sent", "late result applied to the next build");
R.forget(gone);
eq(R.build({ id: gone, reply: reply() }, { post() {} }).getAttribute("data-state"), "idle", "forget clears the local state");
R.deliver({ id: "x" });
R.deliver(null);

// clicks inside the block never reach the toast / item handlers
m = mount();
eq(m.root.fire("click", {}).stopped, true, "block click does not bubble");
eq(m.root.fire("contextmenu", {}).prevented, true, "no context menu");

// the safety net when Python never answers
m = mount();
m.root.all("nc-reply-choice")[0].fire("click", {});
timers.splice(0).forEach((fn) => fn());
eq(status(m).textContent, "Refused: no answer", "unanswered send times out");

console.log(JSON.stringify({ failures, checks }));
