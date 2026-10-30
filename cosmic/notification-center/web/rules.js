// Rules tab of the Settings panel (#/settings/rules): a visual editor for rules.toml.
// Loaded after settings.js, which owns the tab frame and hands us window.ncSettingsKit
// ({el, btn, clear, clone, ask, flash, errText, soundThemes}). Built only with createElement /
// textContent / setAttribute (never an HTML-string setter).
//
// The daemon (SettingsService, through the bridge's rules_* requests) is the only writer and the
// only validator: it edits the file block by block (comments and untouched rules survive) and
// answers {ok, rules, error, editable, path} or {ok: false, errors: {field path: message}}. The
// page keeps a draft of the rule being edited until Save. Views inside the tab: list, edit, source.
//
// window.ncRules: render(root), dirty(), discard(), reset().
// window.ncRulesMove: the reorder rules as pure functions (no DOM), tested under node.
(function () {
  "use strict";

  var K = null;
  function kit() { return K || (K = window.ncSettingsKit); }
  function el(tag, cls, text) { return kit().el(tag, cls, text); }
  function btn(label, cls, fn, title) { return kit().btn(label, cls, fn, title); }
  function ask(msg) { return kit().ask(msg); }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  var BUILTINS = [
    ["details", "Open the details page"],
    ["copy-body", "Copy the body"],
    ["copy-summary", "Copy the summary"],
    ["open-link", "Open the first link"],
    ["mark-read", "Mark read (closes)"],
    ["dismiss", "Dismiss (closes)"],
    ["snooze", "Snooze (closes)"]
  ];
  var MAX_BUTTONS = 6;
  var URGENCIES = ["low", "normal", "critical"];

  // Help texts, from docs/rules.md.
  var HELP = {
    name: "Non-empty and unique. Shown in history as the matched rule.",
    enabled: "Off skips the rule; it is still checked for errors.",
    match_mode: "all: every match field below must match. any: one is enough. It applies only to the match fields this rule sets. A rule with no match fields matches every notification.",
    app: "The app name, exact, case-insensitive.",
    app_regex: "A regex searched (not anchored) in the app name. Use ^ and $ to anchor, and (?i) at the start to ignore case. Python re syntax.",
    summary: "A regex searched anywhere in the summary (first 10,000 characters). (?i) at the start ignores case.",
    body: "A regex searched anywhere in the raw body (first 10,000 characters). (?i) at the start ignores case.",
    urgency: "The notification's urgency. Tick none to match any urgency.",
    category: "The category hint, exact and case-sensitive. Separate several with commas.",
    hint: "Each named hint's value, as text, must match its regex. A missing hint is no match, and every row must match. A boolean is True or False.",
    hint_present: "All of these hints must exist, whatever their value. Separate with commas.",
    block: "Not shown and silent, but still recorded (state blocked).",
    store_only: "Recorded silently, no toast (state store_only). If one rule sets both, block wins; across rules the later rule wins.",
    sound: "A file path (~ is expanded; mp3, wav, ogg, flac), a sound-theme name, or none for silence. Overrides the sender's sound hints.",
    volume: "Sound volume, 0 to 100. Works with or without a sound.",
    timeout: "Toast lifetime in milliseconds, or never to keep it until dismissed. Overrides the app's timeout and the defaults in Behavior. A toast never closes while the pointer is over the stack.",
    set_urgency: "Overrides the urgency (so data-urgency and later rules see it). The timeout is not recomputed: set a timeout too if you want one.",
    icon: "An icon name, file path or ~ path replacing the app's icon. It also outranks an image-path hint.",
    "class": "Extra CSS classes on the toast: letters, digits, _ and -, not starting with a digit or -. Separate with commas or spaces.",
    highlight: "Wraps text matching the pattern in <mark class=\"...\">. The toast applies it with JavaScript: only a leading (?i), (?s) or (?m) is translated. Avoid named groups (?P<n>), \\A, \\Z and other inline flags.",
    tags: "Your own metadata: stored, searchable, and exposed to CSS as .tag-<name>. CSS-safe identifiers, separated by commas or spaces.",
    dnd_bypass: "Show and sound this notification even in Do Not Disturb.",
    stop: "If this rule matched, skip every later rule.",
    run: "A shell string runs through /bin/sh -c. An argument list runs directly, with no shell: one argument per line.",
    run_when: "arrive runs on every match, including blocked and store-only notifications. shown runs only for notifications that are displayed.",
    cooldown: "Minimum seconds between runs of this rule, so a chatty app cannot spam it. 0 means none.",
    run_timeout: "After this many seconds (1 to 3600) the process is sent TERM, then KILL.",
    buttons: "Up to 6 buttons on the toast and the details page. Each has a label (1 to 40 characters) and either a command or a built-in action.",
    button_close: "Whether pressing the button closes the toast. The default depends on the built-in: details, copy and open-link stay open; mark-read, dismiss and snooze close. Commands close by default."
  };

  // ---- state -----------------------------------------------------------------------------

  var S = {
    root: null,
    loaded: false,
    loading: false,
    loadError: "",
    rules: [],
    error: null,        // the file's own problem (validation or layout), shown on the list
    editable: true,
    path: "",
    view: "list",       // list | edit | source
    busy: false,
    notice: "",         // a short failure line on the list
    confirmDelete: -1,
    discard: null,      // a pending "leave without saving" action
    edit: null,         // {index (null = new), orig, name, form, saved (JSON), errors, general}
    source: null,       // {text, saved, error, line}
    test: null,         // the sample notification and the last result
    rx: {}              // regex indicator sequence numbers
  };

  function reset() {
    S.loaded = false;
    S.loading = false;
    S.view = "list";
    S.edit = null;
    S.source = null;
    S.discard = null;
    S.confirmDelete = -1;
    S.notice = "";
  }

  function dirty() {
    if (S.view === "edit" && S.edit) return JSON.stringify(S.edit.form) !== S.edit.saved;
    if (S.view === "source" && S.source) return S.source.text !== S.source.saved;
    return false;
  }

  function discard() {
    S.discard = null;
    if (S.edit) S.edit.form = JSON.parse(S.edit.saved);
    if (S.source) S.source.text = S.source.saved;
  }

  // ---- data <-> form ---------------------------------------------------------------------

  function toList(v) {
    if (v === undefined || v === null) return [];
    return Array.isArray(v) ? v.slice() : [v];
  }
  function words(s) { return String(s || "").split(/[\s,]+/).filter(Boolean); }
  function commas(s) { return String(s || "").split(",").map(function (x) { return x.trim(); }).filter(Boolean); }
  function numOrStr(s) {
    var t = String(s).trim();
    return /^-?\d+$/.test(t) ? Number(t) : t;
  }
  function has(o, k) { return !!o && Object.prototype.hasOwnProperty.call(o, k); }

  var KNOWN = ["name", "enabled", "match_mode", "app", "app_regex", "summary", "body", "urgency",
    "category", "hint", "hint_present", "block", "store_only", "sound", "volume", "timeout",
    "set_urgency", "icon", "class", "highlight", "tags", "dnd_bypass", "stop", "run", "run_when",
    "cooldown", "run_timeout", "buttons"];

  function cmdFields(v) {
    if (Array.isArray(v)) return { run_mode: "argv", run_shell: "", run_argv: v.join("\n") };
    return { run_mode: "shell", run_shell: typeof v === "string" ? v : "", run_argv: "" };
  }

  function blankButton() {
    return { label: "", kind: "builtin", builtin: "details", minutes: "15", run_mode: "shell",
      run_shell: "", run_argv: "", close: "default" };
  }

  function fromData(d) {
    d = d || {};
    var f = {
      name: typeof d.name === "string" ? d.name : "",
      enabled: d.enabled !== false,
      match_mode: d.match_mode === "any" ? "any" : "all",
      app: d.app || "", app_regex: d.app_regex || "", summary: d.summary || "", body: d.body || "",
      urgency: { low: false, normal: false, critical: false },
      category: toList(d.category).join(", "),
      hints: [], hint_present: toList(d.hint_present).join(", "),
      block: d.block === true, store_only: d.store_only === true,
      sound: d.sound || "", volume: has(d, "volume") ? String(d.volume) : "",
      timeout_mode: "default", timeout_ms: "",
      set_urgency: d.set_urgency || "", icon: d.icon || "",
      classes: toList(d["class"]).join(", "), tags: toList(d.tags).join(", "),
      highlights: [], dnd_bypass: d.dnd_bypass === true, stop: d.stop === true,
      run_mode: "none", run_shell: "", run_argv: "", run_when: d.run_when || "arrive",
      cooldown: has(d, "cooldown") ? String(d.cooldown) : "",
      run_timeout: has(d, "run_timeout") ? String(d.run_timeout) : "",
      buttons: [], extra: {}
    };
    toList(d.urgency).forEach(function (u) { if (f.urgency.hasOwnProperty(String(u).toLowerCase())) f.urgency[String(u).toLowerCase()] = true; });
    if (d.hint && typeof d.hint === "object") {
      Object.keys(d.hint).forEach(function (k) { f.hints.push({ k: k, v: String(d.hint[k]) }); });
    }
    if (d.timeout === "never") f.timeout_mode = "never";
    else if (has(d, "timeout")) { f.timeout_mode = "ms"; f.timeout_ms = String(d.timeout); }
    toList(d.highlight).forEach(function (h) {
      if (h && typeof h === "object") f.highlights.push({ pattern: String(h.pattern || ""), cls: String(h["class"] || "") });
    });
    if (has(d, "run")) Object.assign(f, cmdFields(d.run));
    toList(d.buttons).forEach(function (b) {
      if (!b || typeof b !== "object") return;
      var nb = blankButton();
      nb.label = String(b.label || "");
      if (b.builtin) { nb.kind = "builtin"; nb.builtin = String(b.builtin); } else { nb.kind = "run"; Object.assign(nb, cmdFields(b.run)); }
      if (has(b, "minutes")) nb.minutes = String(b.minutes);
      if (has(b, "close")) nb.close = b.close === true ? "yes" : "no";
      f.buttons.push(nb);
    });
    Object.keys(d).forEach(function (k) { if (KNOWN.indexOf(k) < 0) f.extra[k] = d[k]; });
    return f;
  }

  function nonEmpty(s) { return String(s).trim() !== ""; }

  function cmdValue(mode, shell, argv) {
    if (mode === "argv") return String(argv).split("\n").map(function (l) { return l.replace(/\r$/, ""); }).filter(nonEmpty);
    return shell;
  }

  function norm(key, v) {
    if (key === "urgency") return toList(v).map(function (x) { return String(x).toLowerCase(); }).sort();
    if (key === "category" || key === "class" || key === "highlight") return toList(v);
    return v;
  }

  function toData(f, orig) {
    var d = {};
    Object.keys(f.extra || {}).forEach(function (k) { d[k] = f.extra[k]; });
    d.name = f.name;
    if (f.enabled === false || has(orig, "enabled")) d.enabled = f.enabled;
    if (f.match_mode === "any" || has(orig, "match_mode")) d.match_mode = f.match_mode;
    ["app", "app_regex", "summary", "body", "icon"].forEach(function (k) { if (nonEmpty(f[k])) d[k] = f[k]; });
    var u = URGENCIES.filter(function (x) { return f.urgency[x]; });
    if (u.length) d.urgency = u.length === 1 ? u[0] : u;
    var cats = commas(f.category);
    if (cats.length) d.category = cats.length === 1 ? cats[0] : cats;
    var hint = {};
    f.hints.forEach(function (h) { if (nonEmpty(h.k)) hint[h.k.trim()] = h.v; });
    if (Object.keys(hint).length) d.hint = hint;
    var hp = words(f.hint_present);
    if (hp.length) d.hint_present = hp;
    ["block", "store_only", "dnd_bypass", "stop"].forEach(function (k) {
      if (f[k] || has(orig, k)) d[k] = f[k];
    });
    if (nonEmpty(f.sound)) d.sound = f.sound;
    if (nonEmpty(f.volume)) d.volume = numOrStr(f.volume);
    if (f.timeout_mode === "never") d.timeout = "never";
    else if (f.timeout_mode === "ms") d.timeout = numOrStr(f.timeout_ms);
    if (f.set_urgency) d.set_urgency = f.set_urgency;
    var cls = words(f.classes);
    if (cls.length) d["class"] = cls.length === 1 ? cls[0] : cls;
    var hl = f.highlights.filter(function (h) { return nonEmpty(h.pattern) || nonEmpty(h.cls); })
      .map(function (h) { return { pattern: h.pattern, "class": h.cls }; });
    if (hl.length) d.highlight = hl;
    var tags = words(f.tags);
    if (tags.length) d.tags = tags;
    if (f.run_mode !== "none") {
      d.run = cmdValue(f.run_mode, f.run_shell, f.run_argv);
      if (f.run_when === "shown" || has(orig, "run_when")) d.run_when = f.run_when;
      if (nonEmpty(f.cooldown)) d.cooldown = numOrStr(f.cooldown);
      if (nonEmpty(f.run_timeout)) d.run_timeout = numOrStr(f.run_timeout);
    }
    var bs = f.buttons.map(function (b) {
      var o = { label: b.label };
      if (b.kind === "builtin") {
        o.builtin = b.builtin;
        if (b.builtin === "snooze") o.minutes = numOrStr(b.minutes);
      } else {
        o.run = cmdValue(b.run_mode, b.run_shell, b.run_argv);
      }
      if (b.close === "yes") o.close = true;
      else if (b.close === "no") o.close = false;
      return o;
    });
    if (bs.length) d.buttons = bs;
    // keep the file's own spelling where it means the same (urgency = "low" vs ["low"])
    Object.keys(d).forEach(function (k) {
      if (has(orig, k) && JSON.stringify(norm(k, d[k])) === JSON.stringify(norm(k, orig[k]))) d[k] = orig[k];
    });
    return d;
  }

  function uniqueName(base) {
    var names = {};
    S.rules.forEach(function (r) { names[r.name] = true; });
    if (!names[base]) return base;
    for (var i = 2; i < 1000; i++) if (!names[base + " " + i]) return base + " " + i;
    return base;
  }

  // ---- requests --------------------------------------------------------------------------

  function adopt(res) {
    S.rules = res.rules || [];
    S.error = res.error || null;
    S.editable = res.editable !== false;
    S.path = res.path || S.path;
    S.loaded = true;
    S.confirmDelete = -1;
  }

  function load() {
    if (S.loading) return;
    S.loading = true;
    S.loadError = "";
    ask({ type: "rules_get" }).then(function (res) {
      S.loading = false;
      if (res && res.ok) adopt(res);
      else S.loadError = res && res.unavailable ? "Settings unavailable" : kit().errText(res);
      draw();
    });
  }

  // Run one write request; on success adopt the fresh list.
  function write(msg, done) {
    if (S.busy) return;
    S.busy = true;
    S.notice = "";
    ask(msg).then(function (res) {
      S.busy = false;
      if (res && res.ok) adopt(res);
      if (done) done(res);
      draw();
    });
  }

  function failNotice(res) {
    S.notice = kit().errText(res);
    load_soon();
  }
  function load_soon() { S.loading = false; S.loaded = false; }

  // ---- frame: dispatch by view -----------------------------------------------------------

  function render(root) {
    S.root = root;
    draw();
  }

  function draw() {
    if (!S.root || !S.root.isConnected || !kit().onRulesTab()) return;
    S.rx = {};
    var scroller = S.root.parentNode;
    var top = scroller ? scroller.scrollTop : 0;
    clear(S.root);
    var wrap = el("div", "rl");
    S.root.appendChild(wrap);
    if (!S.loaded) {
      if (S.loadError) {
        wrap.appendChild(el("p", "sp-note sp-error", "Could not load the rules: " + S.loadError));
        wrap.appendChild(btn("Retry", "btn", load));
      } else {
        wrap.appendChild(el("p", "sp-note", "Loading rules…"));
        load();
      }
      return;
    }
    if (S.discard) wrap.appendChild(discardBar());
    if (S.view === "edit" && S.edit) drawEdit(wrap);
    else if (S.view === "source" && S.source) drawSource(wrap);
    else drawList(wrap);
    if (scroller) scroller.scrollTop = top;
  }

  function discardBar() {
    var bar = el("div", "center-confirm rl-discard");
    bar.setAttribute("role", "alert");
    bar.appendChild(el("span", null, "You have unsaved changes. Leave and discard them?"));
    bar.appendChild(btn("Discard", "btn btn-danger", function () {
      var fn = S.discard;
      S.discard = null;
      fn();
    }));
    bar.appendChild(btn("Keep editing", "btn", function () { S.discard = null; draw(); }));
    return bar;
  }

  // Run `fn` now, or after the user agrees to drop the unsaved edits.
  function leaving(fn) {
    if (!dirty()) { fn(); return; }
    S.discard = fn;
    draw();
  }

  // ---- list ------------------------------------------------------------------------------

  function drawList(wrap) {
    var head = el("div", "rl-head");
    var title = el("div", "rl-title");
    title.appendChild(el("h3", null, "Rules"));
    title.appendChild(el("span", "rl-path", S.path));
    head.appendChild(title);
    head.appendChild(el("span", "sp-spacer"));
    head.appendChild(btn("Add rule", "btn btn-primary", function () { openEditor(null); }));
    head.appendChild(btn("Edit as text", "btn", function () { openSource(); }));
    wrap.appendChild(head);
    wrap.appendChild(el("p", "sp-hint rl-intro",
      "Rules run top to bottom and every enabled rule that matches applies; a later rule overrides an earlier one for single-value actions. Drag a handle, or focus it and press the arrow keys, to reorder. Saving applies at once."));
    if (S.error) {
      var eb = el("div", "rl-problem");
      eb.setAttribute("role", "alert");
      eb.appendChild(el("strong", null, S.editable ? "The saved file has a problem, the previous good rules stay active: " : "The saved file cannot be listed: "));
      eb.appendChild(document.createTextNode(S.error));
      eb.appendChild(btn("Edit as text", "btn btn-small", function () { openSource(); }));
      wrap.appendChild(eb);
    }
    if (S.notice) {
      var nb = el("div", "rl-problem");
      nb.setAttribute("role", "alert");
      nb.textContent = S.notice;
      wrap.appendChild(nb);
    }
    if (!S.rules.length) {
      wrap.appendChild(el("p", "sp-note", S.editable ? "No rules yet. Add one, or copy examples/rules.toml." : ""));
      return;
    }
    var list = el("ol", "rl-list");
    list.setAttribute("aria-label", "Rules, in the order they run");
    S.rules.forEach(function (r, i) { list.appendChild(listRow(r, i, list)); });
    wrap.appendChild(list);
  }

  function slotFromEvent(ev, list) {
    var rows = list.children;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i].getBoundingClientRect();
      if (ev.clientY < r.top + r.height / 2) return i;
    }
    return rows.length;
  }

  var dragFrom = -1;

  // ---- reorder rules (pure; exported as window.ncRulesMove) -------------------------------

  // Where a key on a handle at `index` moves the rule, or -1 for "do nothing" (not a move key,
  // already there, or out of range).
  function targetIndex(key, index, count) {
    var to = key === "ArrowUp" ? index - 1 : key === "ArrowDown" ? index + 1 : key === "Home" ? 0 : key === "End" ? count - 1 : -1;
    if (to < 0 || to >= count || to === index) return -1;
    return to;
  }

  // Dropping rule `from` on row `over`, on its upper half (before) or lower half: the index the
  // rule ends up at, or -1 for no change. The insertion slot is `over` or `over + 1`; removing
  // the dragged row first shifts a slot below it up by one.
  function dropIndex(from, over, before, count) {
    if (from < 0 || from >= count || over < 0 || over >= count) return -1;
    var slot = before ? over : over + 1;
    var to = slot > from ? slot - 1 : slot;
    return to === from ? -1 : to;
  }

  // The row index whose handle takes focus after a move: keyboard moves follow the rule, a
  // drop leaves focus alone (-1).
  function focusIndex(keepFocus, to) { return keepFocus ? to : -1; }

  window.ncRulesMove = { targetIndex: targetIndex, dropIndex: dropIndex, focusIndex: focusIndex };

  function listRow(r, i, list) {
    var li = el("li", "rl-row" + (r.enabled ? "" : " off"));
    li.setAttribute("data-index", String(i));
    var handle = el("button", "rl-handle", "⠿");
    handle.type = "button";
    handle.setAttribute("aria-label", "Move rule " + r.name + ". Press the up or down arrow key.");
    handle.title = "Drag to reorder, or press the up and down arrow keys";
    handle.draggable = true;
    handle.addEventListener("dragstart", function (ev) {
      dragFrom = i;
      li.classList.add("dragging");
      try { ev.dataTransfer.setData("text/plain", String(i)); ev.dataTransfer.effectAllowed = "move"; ev.dataTransfer.setDragImage(li, 10, 10); } catch (e) { /* ignore */ }
    });
    handle.addEventListener("dragend", function () {
      dragFrom = -1;
      li.classList.remove("dragging");
      Array.prototype.forEach.call(list.children, function (c) { c.classList.remove("drop-before", "drop-after"); });
    });
    handle.addEventListener("keydown", function (ev) {
      var to = targetIndex(ev.key, i, S.rules.length);
      if (to < 0) return;
      ev.preventDefault();
      moveRule(i, to, true);
    });
    li.appendChild(handle);

    var tog = el("input", "sp-check rl-toggle");
    tog.type = "checkbox";
    tog.checked = r.enabled;
    tog.setAttribute("aria-label", "Enabled: " + r.name);
    tog.title = r.enabled ? "Enabled (click to disable)" : "Disabled (click to enable)";
    tog.addEventListener("change", function () {
      write({ type: "rules_set_enabled", index: i, on: tog.checked, expect: r.name }, function (res) {
        if (!res || !res.ok) failNotice(res);
      });
    });
    li.appendChild(tog);

    var text = el("div", "rl-text");
    var nm = el("div", "rl-name", r.name || "(unnamed)");
    if (!r.enabled) nm.appendChild(el("span", "rl-offtag", "disabled"));
    text.appendChild(nm);
    text.appendChild(el("div", "rl-summary", r.summary));
    li.appendChild(text);

    var acts = el("div", "rl-actions");
    if (S.confirmDelete === i) {
      acts.appendChild(el("span", "rl-askdel", "Delete this rule?"));
      acts.appendChild(btn("Delete", "btn btn-danger btn-small", function () {
        S.confirmDelete = -1;
        write({ type: "rules_delete", index: i, expect: r.name }, function (res) {
          if (!res || !res.ok) failNotice(res);
        });
      }));
      acts.appendChild(btn("Cancel", "btn btn-small", function () { S.confirmDelete = -1; draw(); }));
    } else {
      acts.appendChild(btn("Edit", "btn btn-small", function () { openEditor(i); }));
      acts.appendChild(btn("Duplicate", "btn btn-small", function () { duplicate(r, i); }));
      acts.appendChild(btn("Delete", "btn btn-small btn-danger", function () { S.confirmDelete = i; draw(); }));
    }
    li.appendChild(acts);

    li.addEventListener("dragover", function (ev) {
      if (dragFrom < 0) return;
      ev.preventDefault();
      var before = slotFromEvent(ev, list) <= i;
      li.classList.toggle("drop-before", before);
      li.classList.toggle("drop-after", !before);
    });
    li.addEventListener("dragleave", function () { li.classList.remove("drop-before", "drop-after"); });
    li.addEventListener("drop", function (ev) {
      if (dragFrom < 0) return;
      ev.preventDefault();
      var slot = slotFromEvent(ev, list);
      var from = dragFrom;
      dragFrom = -1;
      var to = dropIndex(from, i, slot <= i, S.rules.length);
      if (to >= 0) moveRule(from, to, false);
      else draw();
    });
    return li;
  }

  function moveRule(from, to, keepFocus) {
    var name = S.rules[from].name;
    write({ type: "rules_move", index: from, to: to, expect: name }, function (res) {
      if (!res || !res.ok) { failNotice(res); return; }
      var fi = focusIndex(keepFocus, to);
      if (fi >= 0) {
        window.setTimeout(function () {
          var h = S.root && S.root.querySelector(".rl-row[data-index='" + fi + "'] .rl-handle");
          if (h) h.focus();
        }, 0);
      }
    });
  }

  function duplicate(r, i) {
    var data = clone(r.data);
    data.name = uniqueName(r.name + " copy");
    write({ type: "rules_add", data: data, position: i + 1 }, function (res) {
      if (!res || !res.ok) failNotice(res);
    });
  }

  // ---- editor ----------------------------------------------------------------------------

  function openEditor(index) {
    var orig = index === null ? null : S.rules[index].data;
    var form = fromData(orig || { name: uniqueName("new rule") });
    S.edit = { index: index, orig: orig, name: orig ? orig.name : "", form: form,
      saved: JSON.stringify(form), errors: {}, targets: {} };
    S.view = "edit";
    S.discard = null;
    draw();
  }

  function closeEditor() {
    S.edit = null;
    S.view = "list";
    S.discard = null;
    S.notice = "";
    load_refresh();
  }

  function load_refresh() {
    ask({ type: "rules_get" }).then(function (res) {
      if (res && res.ok) adopt(res);
      draw();
    });
    draw();
  }

  function helpButton(key, box) {
    var b = el("button", "rl-helpbtn", "?");
    b.type = "button";
    b.title = HELP[key];
    b.setAttribute("aria-label", "Help: " + HELP[key]);
    var text = null;
    b.addEventListener("click", function (ev) {
      ev.preventDefault();
      if (text) { text.hidden = !text.hidden; return; }
      text = el("div", "sp-help rl-helptext", HELP[key]);
      box.appendChild(text);
    });
    return b;
  }

  // One labelled row. `path` is the field path the daemon reports errors under.
  function field(label, helpKey, path, control, opts) {
    opts = opts || {};
    var row = el("div", "sp-field rl-field" + (opts.cls ? " " + opts.cls : ""));
    if (path) row.setAttribute("data-path", path);
    var top = el("div", "sp-fieldtop");
    var lab = el("label", "sp-label", label);
    top.appendChild(lab);
    if (path) top.appendChild(el("code", "sp-var", path));
    if (helpKey) top.appendChild(helpButton(helpKey, row));
    row.appendChild(top);
    var line = el("div", "rl-line");
    line.appendChild(control);
    if (opts.after) line.appendChild(opts.after);
    row.appendChild(line);
    var err = el("div", "sp-fielderror");
    err.setAttribute("role", "alert");
    err.hidden = true;
    row.appendChild(err);
    if (path) S.edit.targets[path] = err;
    var first = control.matches && control.matches("input, select, textarea") ? control : control.querySelector("input, select, textarea");
    if (first) {
      first.id = "rl-" + (path || label).replace(/[^A-Za-z0-9_-]/g, "-");
      lab.setAttribute("for", first.id);
    }
    return row;
  }

  function touched(path) {
    var t = S.edit && S.edit.targets[path];
    if (t) { t.hidden = true; t.textContent = ""; }
    if (S.edit) delete S.edit.errors[path];
  }

  function textInput(obj, key, path, opts) {
    opts = opts || {};
    var t = el("input", "sp-text");
    t.type = "text";
    t.spellcheck = false;
    t.value = obj[key];
    if (opts.placeholder) t.placeholder = opts.placeholder;
    if (opts.list) t.setAttribute("list", opts.list);
    t.addEventListener("input", function () {
      obj[key] = t.value;
      if (path) touched(path);
      if (opts.onInput) opts.onInput(t.value);
      syncState();
    });
    return t;
  }

  function areaInput(obj, key, path, rows, ph) {
    var t = el("textarea", "sp-text rl-area");
    t.rows = rows;
    t.spellcheck = false;
    t.setAttribute("wrap", "off");
    t.value = obj[key];
    if (ph) t.placeholder = ph;
    t.addEventListener("input", function () { obj[key] = t.value; if (path) touched(path); syncState(); });
    return t;
  }

  function checkInput(obj, key, label, path) {
    var wrap = el("label", "rl-check");
    var c = el("input", "sp-check");
    c.type = "checkbox";
    c.checked = !!obj[key];
    c.addEventListener("change", function () { obj[key] = c.checked; if (path) touched(path); syncState(); });
    wrap.appendChild(c);
    wrap.appendChild(el("span", null, label));
    return wrap;
  }

  function selectInput(obj, key, choices, path, onChange) {
    var s = el("select", "sp-select");
    choices.forEach(function (c) {
      var o = el("option", null, c[1]);
      o.value = c[0];
      s.appendChild(o);
    });
    s.value = obj[key];
    s.addEventListener("change", function () {
      obj[key] = s.value;
      if (path) touched(path);
      syncState();
      if (onChange) onChange();
    });
    return s;
  }

  function numInput(obj, key, path, ph, width) {
    var t = el("input", "sp-num " + (width || ""));
    t.type = "text";
    t.setAttribute("inputmode", "numeric");
    t.value = obj[key];
    if (ph) t.placeholder = ph;
    t.addEventListener("input", function () { obj[key] = t.value; if (path) touched(path); syncState(); });
    return t;
  }

  // A "regex valid" indicator that asks the daemon (Python re, the engine that matches), so the
  // answer is the real one. kind: app_regex | summary | body | hint | highlight.
  var rxCache = {};
  function rxIndicator(kind, input, getKey) {
    var out = el("span", "rl-rx");
    out.setAttribute("role", "status");
    var timer = null;
    var id = String(Math.random());
    function setState(cls, text) { out.className = "rl-rx " + cls; out.textContent = text; }
    function check() {
      var v = input.value;
      if (!v) { setState("", ""); return; }
      var data = { name: "x" };
      if (kind === "hint") { data.hint = {}; data.hint[getKey() || "k"] = v; }
      else if (kind === "highlight") data.highlight = { pattern: v, "class": "x" };
      else data[kind] = v;
      var mine = (S.rx[id] = (S.rx[id] || 0) + 1);
      var ck = JSON.stringify(data);
      if (rxCache[ck]) { setState(rxCache[ck][0], rxCache[ck][1]); return; }
      setState("checking", "checking…");
      ask({ type: "rules_test", data: data, sample: {} }).then(function (res) {
        var st = ["", ""];
        if (res && res.ok) {
          st = kind === "highlight" && !jsOk(v)
            ? ["warn", "valid regex, but the toast highlighter (JavaScript) cannot read it"]
            : ["ok", "regex valid"];
        } else if (res && res.errors) {
          var msg = Object.keys(res.errors).map(function (k) { return res.errors[k]; })[0] || "invalid";
          st = ["bad", "invalid: " + msg.replace(/^`[^`]*` /, "")];
        }
        rxCache[ck] = st;
        if (S.rx[id] !== mine || !out.isConnected) return;
        setState(st[0], st[1]);
      });
    }
    input.addEventListener("input", function () {
      window.clearTimeout(timer);
      setState("", "");
      timer = window.setTimeout(check, 350);
    });
    if (input.value) window.setTimeout(check, 0);
    return out;
  }

  function jsOk(pattern) {
    var m = /^\(\?([ims]+)\)/.exec(pattern);
    var flags = "";
    if (m) { pattern = pattern.slice(m[0].length); flags = m[1]; }
    try { new RegExp(pattern, flags); return true; } catch (e) { return false; }
  }

  function regexField(label, key, form, path) {
    var t = textInput(form, key, path, { placeholder: "regex" });
    return field(label, key, path, t, { after: rxIndicator(key, t) });
  }

  function section(title, id) {
    var s = el("section", "sp-section rl-section");
    s.setAttribute("data-section", id);
    s.appendChild(el("h3", null, title));
    return s;
  }

  var stateEls = null;
  function syncState() {
    if (!stateEls || !S.edit) return;
    var d = dirty();
    stateEls.save.disabled = !d && S.edit.index !== null;
    stateEls.revert.disabled = !d;
    stateEls.state.textContent = d ? "Unsaved changes" : "";
    stateEls.state.classList.toggle("dirty", d);
  }

  function drawEdit(wrap) {
    var ed = S.edit;
    var f = ed.form;
    ed.targets = {};
    var bar = el("div", "rl-editbar");
    bar.appendChild(btn("← Rules", "btn", function () { leaving(closeEditor); }));
    bar.appendChild(el("h3", "rl-edittitle", ed.index === null ? "New rule" : "Edit rule"));
    bar.appendChild(el("span", "sp-spacer"));
    stateEls = { state: el("span", "sp-state"), save: null, revert: null };
    stateEls.save = btn("Save", "btn btn-primary", saveEdit);
    stateEls.revert = btn("Revert", "btn", function () {
      ed.form = JSON.parse(ed.saved);
      ed.errors = {};
      ed.general = "";
      draw();
    });
    bar.appendChild(stateEls.state);
    bar.appendChild(stateEls.revert);
    bar.appendChild(stateEls.save);
    bar.appendChild(btn("Edit as text", "btn", function () { leaving(openSource); }));
    wrap.appendChild(bar);
    ed.generalEl = el("div", "rl-problem");
    ed.generalEl.setAttribute("role", "alert");
    ed.generalEl.hidden = true;
    wrap.appendChild(ed.generalEl);

    var form = el("div", "rl-form");

    // --- general
    var sg = section("General", "general");
    sg.appendChild(field("Name", "name", "name", textInput(f, "name", "name", { placeholder: "a short unique name" })));
    sg.appendChild(field("Enabled", "enabled", "enabled", checkInput(f, "enabled", "This rule is on", "enabled")));
    form.appendChild(sg);

    // --- match
    var sm = section("Match", "match");
    sm.appendChild(el("p", "sp-hint", "A rule with no match fields matches every notification."));
    sm.appendChild(field("Match mode", "match_mode", "match_mode",
      selectInput(f, "match_mode", [["all", "all: every field below must match"], ["any", "any: one field is enough"]], "match_mode")));
    sm.appendChild(field("App (exact)", "app", "app", textInput(f, "app", "app", { placeholder: "Example App" })));
    sm.appendChild(regexField("App regex", "app_regex", f, "app_regex"));
    sm.appendChild(regexField("Summary regex", "summary", f, "summary"));
    sm.appendChild(regexField("Body regex", "body", f, "body"));
    var urg = el("div", "rl-checks");
    URGENCIES.forEach(function (u) {
      var l = el("label", "rl-check");
      var c = el("input", "sp-check");
      c.type = "checkbox";
      c.checked = f.urgency[u];
      c.addEventListener("change", function () { f.urgency[u] = c.checked; touched("urgency"); syncState(); });
      l.appendChild(c);
      l.appendChild(el("span", null, u));
      urg.appendChild(l);
    });
    sm.appendChild(field("Urgency", "urgency", "urgency", urg));
    sm.appendChild(field("Category", "category", "category", textInput(f, "category", "category", { placeholder: "email, im" })));
    sm.appendChild(hintTable(f));
    sm.appendChild(field("Hint present", "hint_present", "hint_present", textInput(f, "hint_present", "hint_present", { placeholder: "x-nc-url, x-other" })));
    form.appendChild(sm);

    // --- actions
    var sa = section("Actions", "actions");
    var flags = el("div", "rl-checks");
    flags.appendChild(checkInput(f, "block", "Block (hide and silence, still recorded)", "block"));
    flags.appendChild(checkInput(f, "store_only", "Store only (no toast)", "store_only"));
    sa.appendChild(field("Visibility", "block", "block", flags));
    sa.appendChild(soundField(f));
    sa.appendChild(field("Volume", "volume", "volume", numInput(f, "volume", "volume", "0-100")));
    sa.appendChild(timeoutField(f));
    sa.appendChild(field("Set urgency", "set_urgency", "set_urgency",
      selectInput(f, "set_urgency", [["", "(leave as is)"], ["low", "low"], ["normal", "normal"], ["critical", "critical"]], "set_urgency")));
    sa.appendChild(field("Icon", "icon", "icon", textInput(f, "icon", "icon", { placeholder: "icon name or ~/path.png" })));
    sa.appendChild(field("CSS classes", "class", "class", textInput(f, "classes", "class", { placeholder: "urgent, pinned" })));
    sa.appendChild(highlightTable(f));
    sa.appendChild(field("Tags", "tags", "tags", textInput(f, "tags", "tags", { placeholder: "ci, failed" })));
    var more = el("div", "rl-checks");
    more.appendChild(checkInput(f, "dnd_bypass", "Bypass Do Not Disturb", "dnd_bypass"));
    more.appendChild(checkInput(f, "stop", "Stop: skip all later rules", "stop"));
    sa.appendChild(field("Other", "stop", "stop", more));
    form.appendChild(sa);

    // --- scripts
    form.appendChild(scriptSection(f));

    // --- buttons
    form.appendChild(buttonSection(f));

    wrap.appendChild(form);
    wrap.appendChild(testSection());
    showErrors();
    syncState();
  }

  function soundField(f) {
    var box = el("div", "rl-soundline");
    var t = textInput(f, "sound", "sound", { placeholder: "file path, theme name, or none", list: "sp-sound-themes" });
    box.appendChild(t);
    var result = el("div", "sp-playresult");
    result.setAttribute("role", "status");
    var play = btn("▶ Play", "btn btn-action sp-play", function () {
      result.classList.remove("error");
      result.textContent = "Playing…";
      var vol = /^\d+$/.test(f.volume.trim()) ? Math.min(100, Number(f.volume)) : 100;
      ask({ type: "settings_preview_sound", spec: f.sound, volume: vol }).then(function (res) {
        if (res && res.ok) result.textContent = "Played: " + (res.resolved || "system default");
        else { result.classList.add("error"); result.textContent = "Could not play: " + ((res && (res.error || kit().errText(res))) || "failed"); }
      });
    }, "Play this sound");
    var row = field("Sound", "sound", "sound", box, { after: play });
    row.appendChild(result);
    ensureThemeList();
    return row;
  }

  function ensureThemeList() {
    if (document.getElementById("sp-sound-themes")) return;
    kit().soundThemesList();
  }

  function timeoutField(f) {
    var box = el("div", "rl-timeout");
    var ms = numInput(f, "timeout_ms", "timeout", "ms", "sp-num-wide");
    var sel = selectInput(f, "timeout_mode", [["default", "default for the urgency"], ["ms", "milliseconds"], ["never", "never (stays until dismissed)"]], "timeout", function () {
      ms.hidden = f.timeout_mode !== "ms";
    });
    ms.hidden = f.timeout_mode !== "ms";
    box.appendChild(sel);
    box.appendChild(ms);
    return field("Timeout", "timeout", "timeout", box);
  }

  function hintTable(f) {
    var row = el("div", "sp-field rl-field");
    row.setAttribute("data-path", "hint");
    var top = el("div", "sp-fieldtop");
    top.appendChild(el("span", "sp-label", "Hints"));
    top.appendChild(el("code", "sp-var", "hint"));
    top.appendChild(helpButton("hint", row));
    row.appendChild(top);
    var rows = el("div", "rl-rows");
    f.hints.forEach(function (h, i) {
      var line = el("div", "rl-rowline");
      var k = textInput(h, "k", "hint", { placeholder: "hint key, e.g. x-status" });
      k.setAttribute("aria-label", "Hint key");
      var v = textInput(h, "v", "hint", { placeholder: "regex for its value" });
      v.setAttribute("aria-label", "Hint value regex");
      line.appendChild(k);
      line.appendChild(v);
      line.appendChild(rxIndicator("hint", v, function () { return h.k.trim(); }));
      line.appendChild(btn("✕", "btn btn-small rl-x", function () { f.hints.splice(i, 1); touched("hint"); draw(); }, "Remove this hint"));
      rows.appendChild(line);
    });
    row.appendChild(rows);
    row.appendChild(btn("+ Add hint", "btn btn-small", function () { f.hints.push({ k: "", v: "" }); draw(); }));
    var err = el("div", "sp-fielderror");
    err.setAttribute("role", "alert");
    err.hidden = true;
    row.appendChild(err);
    S.edit.targets.hint = err;
    return row;
  }

  function highlightTable(f) {
    var row = el("div", "sp-field rl-field");
    row.setAttribute("data-path", "highlight");
    var top = el("div", "sp-fieldtop");
    top.appendChild(el("span", "sp-label", "Highlights"));
    top.appendChild(el("code", "sp-var", "highlight"));
    top.appendChild(helpButton("highlight", row));
    row.appendChild(top);
    var rows = el("div", "rl-rows");
    f.highlights.forEach(function (h, i) {
      var line = el("div", "rl-rowline");
      var p = textInput(h, "pattern", "highlight", { placeholder: "pattern, e.g. (?i)\\bURGENT\\b" });
      p.setAttribute("aria-label", "Highlight pattern");
      var c = textInput(h, "cls", "highlight", { placeholder: "class" });
      c.setAttribute("aria-label", "Highlight class");
      c.classList.add("rl-short");
      line.appendChild(p);
      line.appendChild(c);
      line.appendChild(rxIndicator("highlight", p));
      line.appendChild(btn("✕", "btn btn-small rl-x", function () { f.highlights.splice(i, 1); touched("highlight"); draw(); }, "Remove this highlight"));
      rows.appendChild(line);
    });
    row.appendChild(rows);
    row.appendChild(btn("+ Add highlight", "btn btn-small", function () { f.highlights.push({ pattern: "", cls: "" }); draw(); }));
    var err = el("div", "sp-fielderror");
    err.setAttribute("role", "alert");
    err.hidden = true;
    row.appendChild(err);
    S.edit.targets.highlight = err;
    return row;
  }

  function commandControl(obj, prefix, path, modeKey, shellKey, argvKey) {
    var box = el("div", "rl-cmd");
    var mode = selectInput(obj, modeKey, [["shell", "Shell string (sh -c)"], ["argv", "Argument list (no shell)"]], path, function () { draw(); });
    box.appendChild(mode);
    if (obj[modeKey] === "argv") {
      box.appendChild(areaInput(obj, argvKey, path, 3, "one argument per line:\nsh\n-c\nprintf '%s\\n' \"$NC_SUMMARY\" >> ~/log"));
    } else {
      box.appendChild(textInput(obj, shellKey, path, { placeholder: "printf '%s\\n' \"$NC_SUMMARY\" >> ~/log" }));
    }
    return box;
  }

  function scriptSection(f) {
    var s = section("Script", "script");
    var warn = el("div", "rl-warn");
    warn.setAttribute("role", "note");
    warn.appendChild(el("strong", null, "Notification text arrives only in environment variables. "));
    warn.appendChild(document.createTextNode("A command reads NC_ID, NC_APP, NC_SUMMARY, NC_BODY, NC_URGENCY, NC_CATEGORY, NC_TAGS, NC_HINTS_JSON, NC_RULE (and NC_BUTTON for a button). Never paste notification text into the command itself: anyone who can send a notification controls it, so that would be command injection. Write a fixed command and read the variables inside it, quoted: \"$NC_SUMMARY\"."));
    s.appendChild(warn);
    var enable = checkInput({ v: f.run_mode !== "none" }, "v", "Run a command when this rule matches", null);
    enable.querySelector("input").addEventListener("change", function (ev) {
      f.run_mode = ev.target.checked ? (f.run_argv ? "argv" : "shell") : "none";
      draw();
    });
    s.appendChild(field("Command", "run", null, enable));
    if (f.run_mode !== "none") {
      s.appendChild(field("Command line", "run", "run", commandControl(f, "run", "run", "run_mode", "run_shell", "run_argv")));
      s.appendChild(field("Run when", "run_when", "run_when", selectInput(f, "run_when", [["arrive", "arrive: on every match"], ["shown", "shown: only if displayed"]], "run_when")));
      s.appendChild(field("Cooldown (seconds)", "cooldown", "cooldown", numInput(f, "cooldown", "cooldown", "0")));
      s.appendChild(field("Time limit (seconds)", "run_timeout", "run_timeout", numInput(f, "run_timeout", "run_timeout", "30")));
    }
    return s;
  }

  function buttonSection(f) {
    var s = section("Buttons", "buttons");
    S.edit.targets.buttons = el("div", "sp-fielderror");
    S.edit.targets.buttons.setAttribute("role", "alert");
    S.edit.targets.buttons.hidden = true;
    var intro = el("div", "rl-introline");
    intro.appendChild(el("span", "sp-hint", f.buttons.length + " of " + MAX_BUTTONS + " buttons. "));
    intro.appendChild(helpButton("buttons", s));
    s.appendChild(intro);
    f.buttons.forEach(function (b, i) { s.appendChild(buttonRow(f, b, i)); });
    var add = btn("+ Add button", "btn btn-small", function () { f.buttons.push(blankButton()); draw(); });
    add.disabled = f.buttons.length >= MAX_BUTTONS;
    s.appendChild(add);
    s.appendChild(S.edit.targets.buttons);
    return s;
  }

  function buttonRow(f, b, i) {
    var p = "buttons[" + i + "]";
    var card = el("div", "rl-button");
    card.setAttribute("data-path", p);
    var head = el("div", "rl-buttonhead");
    head.appendChild(el("strong", null, "Button " + (i + 1)));
    head.appendChild(el("span", "sp-spacer"));
    var up = btn("▲", "btn btn-small", function () { var t = f.buttons[i - 1]; f.buttons[i - 1] = b; f.buttons[i] = t; draw(); }, "Move this button up");
    up.disabled = i === 0;
    var down = btn("▼", "btn btn-small", function () { var t = f.buttons[i + 1]; f.buttons[i + 1] = b; f.buttons[i] = t; draw(); }, "Move this button down");
    down.disabled = i === f.buttons.length - 1;
    head.appendChild(up);
    head.appendChild(down);
    head.appendChild(btn("✕", "btn btn-small rl-x", function () { f.buttons.splice(i, 1); draw(); }, "Remove this button"));
    card.appendChild(head);
    card.appendChild(field("Label", null, p + ".label", textInput(b, "label", p + ".label", { placeholder: "1 to 40 characters" })));
    card.appendChild(field("Does", null, p, selectInput(b, "kind", [["builtin", "A built-in action"], ["run", "Run a command"]], p, function () { draw(); })));
    if (b.kind === "builtin") {
      card.appendChild(field("Action", null, p + ".builtin", selectInput(b, "builtin", BUILTINS, p + ".builtin", function () { draw(); })));
      if (b.builtin === "snooze") card.appendChild(field("Minutes", null, p + ".minutes", numInput(b, "minutes", p + ".minutes", "1-10080")));
    } else {
      card.appendChild(field("Command", null, p + ".run", commandControl(b, p, p + ".run", "run_mode", "run_shell", "run_argv"), { cls: "rl-wide" }));
    }
    var closeRow = field("Closes the toast", "button_close", p + ".close", selectInput(b, "close", [["default", "default for this action"], ["yes", "yes"], ["no", "no"]], p + ".close"));
    card.appendChild(closeRow);
    return card;
  }

  // ---- errors ----------------------------------------------------------------------------

  function showErrors() {
    var ed = S.edit;
    if (!ed) return;
    var general = [];
    Object.keys(ed.errors).forEach(function (path) {
      var msg = ed.errors[path];
      var t = ed.targets[path];
      if (!t) {
        var base = path.replace(/\.[^.\[]*$/, "");
        t = ed.targets[base] || ed.targets[path.split(/[.\[]/)[0]];
      }
      if (t) {
        t.textContent = msg;
        t.hidden = false;
      } else {
        general.push(path ? path + ": " + msg : msg);
      }
    });
    ed.generalEl.textContent = general.join("; ");
    ed.generalEl.hidden = !general.length;
    var first = S.root && S.root.querySelector(".sp-fielderror:not([hidden])");
    if (first && first.scrollIntoView) first.scrollIntoView({ block: "center" });
  }

  function saveEdit() {
    var ed = S.edit;
    var data = toData(ed.form, ed.orig);
    ed.errors = {};
    var msg = ed.index === null
      ? { type: "rules_add", data: data }
      : { type: "rules_update", index: ed.index, data: data, expect: ed.name };
    write(msg, function (res) {
      if (res && res.ok) {
        var i = -1;
        S.rules.forEach(function (r, n) { if (r.name === data.name) i = n; });
        ed.index = i < 0 ? ed.index : i;
        ed.orig = i < 0 ? data : S.rules[i].data;
        ed.name = data.name;
        ed.saved = JSON.stringify(ed.form);
        kit().flash("Rule saved");
      } else {
        ed.errors = (res && res.errors) || { "": (res && res.error) || "failed" };
        if (res && res.unavailable) ed.errors = { "": "Settings unavailable" };
      }
    });
  }

  // ---- test panel ------------------------------------------------------------------------

  function testState() {
    if (!S.test) {
      S.test = { app: "Example App", summary: "Build failed: 2 errors", body: "main is red again.",
        urgency: "normal", category: "", hints: [], touched: false, result: null, error: "", asked: false, busy: false };
    }
    return S.test;
  }

  function testSection() {
    var t = testState();
    var s = section("Test this rule", "test");
    s.appendChild(el("p", "sp-hint", "Runs your unsaved draft against a sample notification without running scripts, playing sounds or saving anything."));
    var grid = el("div", "rl-sample");
    grid.appendChild(field("App", null, null, textInput(t, "app", null, { onInput: function () { t.touched = true; } })));
    grid.appendChild(field("Summary", null, null, textInput(t, "summary", null, { onInput: function () { t.touched = true; } })));
    grid.appendChild(field("Body", null, null, areaInput(t, "body", null, 3)));
    grid.appendChild(field("Urgency", null, null, selectInput(t, "urgency", [["low", "low"], ["normal", "normal"], ["critical", "critical"]], null)));
    grid.appendChild(field("Category", null, null, textInput(t, "category", null)));
    var hints = el("div", "rl-rows");
    t.hints.forEach(function (h, i) {
      var line = el("div", "rl-rowline");
      var k = textInput(h, "k", null, { placeholder: "key" });
      var v = textInput(h, "v", null, { placeholder: "value" });
      line.appendChild(k);
      line.appendChild(v);
      line.appendChild(btn("✕", "btn btn-small rl-x", function () { t.hints.splice(i, 1); draw(); }, "Remove this hint"));
      hints.appendChild(line);
    });
    var hrow = field("Hints", null, null, hints);
    hrow.appendChild(btn("+ Add hint", "btn btn-small", function () { t.hints.push({ k: "", v: "" }); draw(); }));
    grid.appendChild(hrow);
    s.appendChild(grid);
    var bar = el("div", "sp-savebar");
    var run = btn("Test this rule", "btn btn-primary", runTest);
    run.disabled = t.busy;
    bar.appendChild(run);
    bar.appendChild(btn("Use the latest notification", "btn", function () { fetchLatest(true); }));
    s.appendChild(bar);
    if (t.error) {
      var e = el("div", "rl-problem", t.error);
      e.setAttribute("role", "alert");
      s.appendChild(e);
    }
    if (t.result) s.appendChild(resultView(t.result));
    if (!t.asked) fetchLatest(false);
    return s;
  }

  function fetchLatest(force) {
    var t = testState();
    t.asked = true;
    if (t.touched && !force) return;
    ask({ type: "list", limit: 1 }).then(function (res) {
      var item = res && res.items && res.items[0];
      if (!item) return null;
      return ask({ type: "get", id: item.id });
    }).then(function (res) {
      var n = res && res.notification;
      if (!n) return;
      t.app = n.app || "";
      t.summary = n.summary || "";
      t.body = (n.body || []).map(function (r) { return r.text; }).join("");
      t.urgency = n.urgency || "normal";
      t.category = n.category || "";
      t.hints = [];
      var h = n.hints || {};
      Object.keys(h).slice(0, 50).forEach(function (k) {
        var v = h[k];
        t.hints.push({ k: k, v: String(typeof v === "object" ? JSON.stringify(v) : v).slice(0, 4000) });
      });
      t.touched = false;
      if (S.view === "edit") draw();
    });
  }

  function sampleOf(t) {
    var hints = {};
    t.hints.forEach(function (h) { if (nonEmpty(h.k)) hints[h.k.trim()] = h.v; });
    return { app: t.app, summary: t.summary, body: t.body, urgency: t.urgency, category: t.category, hints: hints };
  }

  function runTest() {
    var t = testState();
    var ed = S.edit;
    t.error = "";
    t.busy = true;
    var msg = { type: "rules_test", data: toData(ed.form, ed.orig), sample: sampleOf(t) };
    if (ed.index !== null) msg.index = ed.index;
    ask(msg).then(function (res) {
      t.busy = false;
      if (res && res.ok) {
        t.result = res;
      } else {
        t.result = null;
        var e = (res && res.errors) || {};
        var keys = Object.keys(e);
        t.error = keys.length ? "The draft is not valid: " + keys.map(function (k) { return k ? k + ": " + e[k] : e[k]; }).join("; ") : "The test failed.";
        ed.errors = e;
      }
      draw();
    });
  }

  function previewToast(p) {
    var wrap = el("div", "sp-preview rl-preview");
    var stack = el("div", "stack sp-stack");
    var t = el("div", "toast");
    t.setAttribute("data-app", p.app || "");
    t.setAttribute("data-urgency", p.urgency || "normal");
    t.setAttribute("data-category", p.category || "");
    (p.tags || []).forEach(function (x) { t.classList.add("tag-" + String(x).toLowerCase().replace(/[^a-z0-9_-]/g, "-")); });
    (p.classes || []).forEach(function (c) { t.classList.add(c); });
    var icon = el("div", "toast-icon");
    icon.appendChild(window.ncAvatar(p.app || "?"));
    t.appendChild(icon);
    var c = el("div", "toast-content");
    c.appendChild(el("div", "toast-app", p.app || ""));
    var sum = el("div", "toast-summary", p.summary || "");
    c.appendChild(sum);
    var body = el("div", "toast-body");
    (p.body || []).forEach(function (r) { body.appendChild(document.createTextNode(r.text)); });
    c.appendChild(body);
    (p.highlights || []).forEach(function (h) {
      var re = compileHl(h.pattern);
      if (re && h["class"]) { highlight(sum, re, h["class"]); highlight(body, re, h["class"]); }
    });
    if ((p.buttons || []).length) {
      var row = el("div", "toast-buttons");
      p.buttons.forEach(function (b) {
        var x = el("button", "toast-button", b.label);
        x.type = "button";
        x.setAttribute("data-kind", b.kind || "run");
        x.setAttribute("data-builtin", b.builtin || "");
        if (b.minutes) x.setAttribute("data-minutes", String(b.minutes));
        row.appendChild(x);
      });
      c.appendChild(row);
    }
    t.appendChild(c);
    var close = el("button", "toast-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    t.appendChild(close);
    stack.appendChild(t);
    wrap.appendChild(stack);
    return wrap;
  }

  function compileHl(pattern) {
    var flags = "g";
    var m = /^\(\?([ims]+)\)/.exec(String(pattern));
    pattern = String(pattern);
    if (m) {
      pattern = pattern.slice(m[0].length);
      m[1].split("").forEach(function (f) { if (flags.indexOf(f) < 0) flags += f; });
    }
    try { return new RegExp(pattern, flags); } catch (e) { return null; }
  }

  function highlight(root, re, cls) {
    var nodes = [];
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(function (node) {
      var text = node.nodeValue;
      var frag = document.createDocumentFragment();
      var last = 0, hit = false, m;
      re.lastIndex = 0;
      while ((m = re.exec(text)) !== null) {
        if (m[0] === "") { re.lastIndex++; continue; }
        hit = true;
        if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        var mark = document.createElement("mark");
        mark.className = cls;
        mark.textContent = m[0];
        frag.appendChild(mark);
        last = m.index + m[0].length;
      }
      if (!hit) return;
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });
  }

  function facts(p) {
    var dl = el("dl", "rl-facts");
    function add(k, v) { dl.appendChild(el("dt", null, k)); dl.appendChild(el("dd", null, v)); }
    add("State", p.state + (p.dnd_bypass ? ", bypasses Do Not Disturb" : ""));
    add("Urgency", p.urgency);
    add("Sound", p.sound === null || p.sound === undefined ? "(default)" : p.sound + (p.volume !== null && p.volume !== undefined ? " at volume " + p.volume : ""));
    add("Timeout", p.timeout_ms === null ? "never" : p.timeout_ms + " ms");
    add("Classes", (p.classes || []).join(", ") || "none");
    add("Tags", (p.tags || []).join(", ") || "none");
    add("Matched rules", (p.matched_rules || []).join(", ") || "none");
    add("Would run", (p.runs || []).map(function (r) { return r.rule + " (" + r.when + ")"; }).join(", ") || "nothing");
    return dl;
  }

  function resultView(res) {
    var box = el("div", "rl-result");
    box.setAttribute("aria-live", "polite");
    var badge = el("div", "rl-verdict " + (res.matched ? "yes" : "no"), res.matched ? "Matched: yes" : "Matched: no (this rule does nothing to this sample)");
    box.appendChild(badge);
    var r = res.result || {};
    var cols = el("div", "rl-resultcols");
    var one = el("div", "rl-resultcol");
    one.appendChild(el("h4", null, "After this rule alone"));
    if (r.rule) { one.appendChild(previewToast(r.rule)); one.appendChild(facts(r.rule)); }
    cols.appendChild(one);
    var full = el("div", "rl-resultcol");
    full.appendChild(el("h4", null, "After the full ruleset"));
    if (r.full) {
      full.appendChild(previewToast(r.full));
      full.appendChild(facts(r.full));
    } else {
      full.appendChild(el("p", "sp-hint", r.full_error ? "Not available: " + r.full_error : "Not available."));
    }
    cols.appendChild(full);
    box.appendChild(cols);
    return box;
  }

  // ---- raw source ------------------------------------------------------------------------

  function openSource() {
    S.discard = null;
    ask({ type: "rules_get_source" }).then(function (res) {
      if (!(res && res.ok)) { S.notice = kit().errText(res); S.view = "list"; draw(); return; }
      S.source = { text: res.text, saved: res.text, error: "", line: 0 };
      S.view = "source";
      draw();
    });
  }

  function closeSource() {
    S.source = null;
    S.view = "list";
    S.discard = null;
    load_refresh();
  }

  function drawSource(wrap) {
    var src = S.source;
    var bar = el("div", "rl-editbar");
    bar.appendChild(btn("← Rules", "btn", function () { leaving(closeSource); }));
    bar.appendChild(el("h3", "rl-edittitle", "Edit as text"));
    bar.appendChild(el("span", "rl-path", S.path));
    bar.appendChild(el("span", "sp-spacer"));
    var state = el("span", "sp-state");
    var save = btn("Save", "btn btn-primary", function () { saveSource(ta); });
    var revert = btn("Revert", "btn", function () { src.text = src.saved; src.error = ""; src.line = 0; draw(); });
    bar.appendChild(state);
    bar.appendChild(revert);
    bar.appendChild(save);
    wrap.appendChild(bar);
    wrap.appendChild(el("p", "sp-hint", "The whole rules.toml. Saving checks every rule exactly as the daemon will; a file with an error is not written."));
    var err = el("div", "rl-problem rl-sourceerr");
    err.setAttribute("role", "alert");
    err.hidden = !src.error;
    err.textContent = src.error;
    wrap.appendChild(err);
    var ta = el("textarea", "sp-css rl-source");
    ta.id = "rl-source";
    ta.spellcheck = false;
    ta.setAttribute("wrap", "off");
    ta.setAttribute("aria-label", "rules.toml");
    ta.value = src.text;
    function sync() {
      var d = src.text !== src.saved;
      save.disabled = !d;
      revert.disabled = !d;
      state.textContent = d ? "Unsaved changes" : "";
      state.classList.toggle("dirty", d);
    }
    ta.addEventListener("input", function () { src.text = ta.value; sync(); });
    ta.addEventListener("keydown", function (ev) {
      if (ev.key === "Tab" && !ev.shiftKey && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
        ev.preventDefault();
        var ok = false;
        try { ok = document.execCommand("insertText", false, "  "); } catch (e) { ok = false; }
        if (!ok) { ta.setRangeText("  ", ta.selectionStart, ta.selectionEnd, "end"); ta.dispatchEvent(new Event("input")); }
      }
    });
    wrap.appendChild(ta);
    wrap.appendChild(el("p", "sp-hint", "Tab inserts two spaces. Press Esc, then Tab, to move focus out."));
    sync();
    if (src.line) window.setTimeout(function () { gotoLine(ta, src.line); }, 0);
  }

  function gotoLine(ta, line) {
    var lines = ta.value.split("\n");
    var start = 0;
    for (var i = 0; i < line - 1 && i < lines.length; i++) start += lines[i].length + 1;
    var end = start + (lines[line - 1] || "").length;
    ta.focus();
    ta.setSelectionRange(start, end);
    var lh = parseFloat(getComputedStyle(ta).lineHeight) || 18;
    ta.scrollTop = Math.max(0, (line - 4) * lh);
  }

  function saveSource(ta) {
    var src = S.source;
    ask({ type: "rules_save_source", text: src.text }).then(function (res) {
      if (res && res.ok) {
        adopt(res);
        src.saved = src.text;
        src.error = "";
        src.line = 0;
        kit().flash("rules.toml saved");
        closeSource();
        return;
      }
      var e = (res && res.errors) || {};
      var msg = e.source || Object.keys(e).map(function (k) { return e[k]; }).join("; ") || "failed";
      var m = /line (\d+)/i.exec(msg);
      src.line = m ? Number(m[1]) : 0;
      var lines = src.text.split("\n");
      // a rule error names the rule's header line: point at the key it complains about instead
      var key = /`([A-Za-z_]+)[`\[.]/.exec(msg);
      if (src.line && key && /^\s*\[\[\s*rule\s*\]\]/.test(lines[src.line - 1] || "")) {
        var kre = new RegExp("^\\s*" + key[1] + "\\s*=");
        for (var n = src.line; n < lines.length && !/^\s*\[\[\s*rule\s*\]\]/.test(lines[n]); n++) {
          if (kre.test(lines[n])) { src.line = n + 1; break; }
        }
      }
      src.error = msg + (src.line && lines[src.line - 1] !== undefined ? "\nline " + src.line + ": " + lines[src.line - 1].slice(0, 160) : "");
      draw();
      if (src.line) { var area = document.getElementById("rl-source"); if (area) gotoLine(area, src.line); }
    });
  }

  window.ncRules = { render: render, dirty: dirty, discard: discard, reset: reset };
})();
