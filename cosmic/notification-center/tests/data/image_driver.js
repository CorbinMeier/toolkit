// Runs web/image.js under node with a stand-in DOM: node image_driver.js <path to image.js>.
// Prints one JSON line {checks, failures}. Exit 0 always (the Python test reads the failures).
"use strict";
const fs = require("fs");
const vm = require("vm");

class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.parentNode = null; this.attrs = {};
    this.listeners = {}; this.className = ""; this._text = ""; this.type = "";
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === root; }
  fire(t) {
    const ev = { stopped: false, prevented: false,
      stopPropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; } };
    (this.listeners[t] || []).forEach((f) => f(ev));
    return ev;
  }
  find(cls) {
    if ((" " + this.className + " ").includes(" " + cls + " ")) return this;
    for (const c of this.children) { const r = c.find(cls); if (r) return r; }
    return null;
  }
  all(cls, out = []) {
    if ((" " + this.className + " ").includes(" " + cls + " ")) out.push(this);
    this.children.forEach((c) => c.all(cls, out));
    return out;
  }
}
const root = new Node("root");
const timers = [];
const window = {};
const sandbox = {
  window, document: { createElement: (t) => new Node(t) },
  setTimeout: (f) => { timers.push(f); return timers.length; },
  clearTimeout: () => {},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), sandbox);

let checks = 0;
const failures = [];
function check(name, ok) { checks++; if (!ok) failures.push(name); }

const URI = "nc-image://img/7/1a-2b";
const n = { id: 7, image: { uri: URI, mime: "image/png", name: "shot.png" } };
const sent = [];
const post = (m) => sent.push(m);

check("no image -> null", window.ncImage.build({ id: 1, image: null }, { post }) === null);
check("missing image key -> null", window.ncImage.build({ id: 1 }, { post }) === null);
for (const bad of ["file:///etc/passwd", "http://x/y.png", "nc-image://img/x/1", "javascript:1", 5, ""]) {
  check("bad uri refused: " + bad, window.ncImage.build({ id: 1, image: { uri: bad } }, { post }) === null);
  check("bad thumb refused: " + bad, window.ncImage.thumb({ id: 1, image: { uri: bad } }) === null);
}

const box = window.ncImage.build(n, { post, host: "toast" });
root.appendChild(box);
check("block class + host", box.className === "nc-image" && box.getAttribute("data-host") === "toast");
const pic = box.find("nc-image-preview");
check("preview src is the nc-image uri", pic.getAttribute("src") === URI);
check("preview not openable in a toast", pic.getAttribute("data-open") === null);
const btns = box.all("nc-image-button");
check("two buttons", btns.length === 2);
check("labels", btns[0].textContent === "Copy image" && btns[1].textContent === "Copy path");
check("kinds", btns[0].getAttribute("data-kind") === "copy" && btns[1].getAttribute("data-kind") === "path");

let ev = btns[0].fire("click");
check("copy posts image_copy", JSON.stringify(sent[0]) === JSON.stringify({ type: "image_copy", id: 7 }));
check("copy click is stopped (no dismiss, no default action)", ev.stopped);
ev = btns[1].fire("click");
check("path posts image_copy_path", JSON.stringify(sent[1]) === JSON.stringify({ type: "image_copy_path", id: 7 }));
check("path click is stopped", ev.stopped);
check("context menu is blocked", btns[0].fire("contextmenu").prevented);
check("a toast preview click posts nothing itself", (pic.fire("click"), sent.length === 2));

window.ncImage.deliver({ id: 7, kind: "image_copy", ok: true });
check("feedback: Copied", btns[0].textContent === "Copied" && btns[0].getAttribute("data-state") === "done");
check("other button untouched", btns[1].textContent === "Copy path");
window.ncImage.deliver({ id: 7, kind: "image_copy_path", ok: false });
check("feedback: Failed", btns[1].textContent === "Failed" && btns[1].getAttribute("data-state") === "failed");
timers.forEach((f) => f());
check("feedback resets", btns[0].textContent === "Copy image" && btns[0].getAttribute("data-state") === null);
check("feedback resets (path)", btns[1].textContent === "Copy path");
window.ncImage.deliver({ id: 8, kind: "image_copy", ok: true });
check("another id is ignored", btns[0].textContent === "Copy image");
for (const junk of [null, 5, "x", {}, { id: 7, kind: "image_open", ok: true }, { id: 7, kind: "nope" }]) {
  window.ncImage.deliver(junk);
}
check("junk results are ignored", btns[0].textContent === "Copy image" && btns[1].textContent === "Copy path");

const big = window.ncImage.build(n, { post, host: "center", open: true });
root.appendChild(big);
const bigPic = big.find("nc-image-preview");
check("center preview is openable", bigPic.getAttribute("data-open") === "true");
ev = bigPic.fire("click");
check("click posts image_open", JSON.stringify(sent[sent.length - 1]) === JSON.stringify({ type: "image_open", id: 7 }) && ev.stopped);

const gone = window.ncImage.build(n, { post, host: "shade" });
root.appendChild(gone);
gone.find("nc-image-preview").fire("error");
check("a missing file removes the block silently", gone.parentNode === null && !root.children.includes(gone));

const th = window.ncImage.thumb(n);
root.appendChild(th);
check("thumbnail", th.className === "nc-image-thumb" && th.find("nc-image-thumb-img").getAttribute("src") === URI);
th.find("nc-image-thumb-img").fire("error");
check("a broken thumbnail goes away", th.parentNode === null);

console.log(JSON.stringify({ checks, failures }));
