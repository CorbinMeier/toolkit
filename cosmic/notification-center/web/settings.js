// Settings panel of the notification center (#/settings/<appearance|behavior|sounds|rules|advanced>).
// Built only with createElement / textContent / setAttribute (never an HTML-string setter);
// custom properties are applied with style.setProperty. Everything the page edits is held as a
// draft until Save; the daemon (SettingsService, through the bridge's settings_* requests)
// validates again and answers {ok, errors: {field: message}}.
//
// center.js owns the router and calls window.ncSettings: show(tab), active(), dirty(), guard(hash),
// leave(). It hands us window.ncHost: ask, flash, back, navigate.
(function () {
  "use strict";

  var host = null;
  function H() { return host || (host = window.ncHost); }
  function ask(msg) { return H().ask(msg); }

  var TABS = [
    ["appearance", "Appearance"],
    ["behavior", "Behavior"],
    ["sounds", "Sounds"],
    ["rules", "Rules"],
    ["advanced", "Advanced"]
  ];
  var BEHAVIOR_SECTIONS = [["toasts", "Toasts"], ["shade", "Shade"], ["history", "History"], ["images", "Images"]];
  var SOUND_SECTION = "sound";
  var PRESET_INFO = {
    "default": "The neutral dark look.",
    "crt-green": "Green phosphor on black, after the lock screen.",
    "night-ops-hud": "Teal console panel, after the desktop dashboard."
  };
  // Slider ranges, in px (or % for the avatar values): [min, max, step].
  var RANGES = {
    "--nc-font-size": [8, 32, 1],
    "--nc-border-width": [0, 8, 1],
    "--nc-radius": [0, 40, 1],
    "--nc-width": [240, 640, 5],
    "--nc-gap": [0, 40, 1],
    "--nc-padding": [0, 40, 1],
    "--nc-icon-size": [16, 96, 1],
    "--nc-shadow-room": [0, 60, 1],
    "--nc-avatar-saturation": [0, 100, 1],
    "--nc-avatar-lightness": [0, 100, 1]
  };
  var QUICK_ANIM = {
    "--nc-enter-animation": [["Slide in", "nc-enter var(--nc-anim-duration) ease-out both"], ["None", "none"]],
    "--nc-exit-animation": [["Slide out", "nc-exit var(--nc-anim-duration) ease-in both"], ["None", "none"]]
  };

  // ---- state -----------------------------------------------------------------------------

  var built = false;
  var isActive = false;
  var tab = "appearance";
  var loadState = "idle";       // idle | loading | ready | unavailable | error
  var loadError = "";
  var data = null;              // SettingsService.get()
  var theme = null;             // {saved: {}, draft: {}, errors: {}, general: "", presetShown: ""}
  var style = null;             // {saved, draft, error}
  var config = {};              // section -> {saved, draft, errors, general, restart: []}
  var lastHash = "#/settings/appearance";
  var pendingHash = null;
  var preview = null;           // {root, replay}
  var rows = {};                // variable name -> {reset, err}
  var groupResets = [];

  var ui = {};                  // frames: view, tabs, panel, guard, testResult

  // ---- helpers ---------------------------------------------------------------------------

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function btn(label, cls, onclick, title) {
    var b = el("button", cls || "btn", label);
    b.type = "button";
    if (title) { b.title = title; b.setAttribute("aria-label", title); }
    b.addEventListener("click", function (ev) { ev.stopPropagation(); onclick(ev); });
    return b;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function same(a, b) { return JSON.stringify(sortKeys(a)) === JSON.stringify(sortKeys(b)); }
  function sortKeys(o) {
    var out = {};
    Object.keys(o || {}).sort().forEach(function (k) { out[k] = o[k]; });
    return out;
  }
  function num(n) { return Number(Number(n).toFixed(3)); }
  function errText(res) {
    var e = res && res.errors;
    if (e && typeof e === "object") {
      var parts = Object.keys(e).map(function (k) { return k ? k + ": " + e[k] : e[k]; });
      if (parts.length) return parts.join("; ");
    }
    return (res && res.error) || "failed";
  }

  // ---- dirty / navigation guard -----------------------------------------------------------

  function themeDirty() { return !!theme && !same(theme.draft, theme.saved); }
  function styleDirty() { return !!style && style.draft !== style.saved; }
  function configDirty(section) {
    var c = config[section];
    return !!c && !same(c.draft, c.saved);
  }
  function anyConfigDirty() {
    return Object.keys(config).some(configDirty);
  }
  function rulesDirty() { return !!window.ncRules && window.ncRules.dirty(); }
  function dirty() { return themeDirty() || styleDirty() || anyConfigDirty() || rulesDirty(); }

  function showGuard(hash) {
    pendingHash = hash;
    ui.guard.hidden = false;
  }
  function guard(hash) {
    // The hash already changed; put it back without firing hashchange, then ask.
    try { history.replaceState(null, "", lastHash); } catch (e) { /* ignore */ }
    showGuard(hash);
  }
  function discardAll() {
    if (window.ncRules) { window.ncRules.discard(); window.ncRules.reset(); }
    if (theme) { theme.draft = clone(theme.saved); theme.errors = {}; theme.general = ""; }
    if (style) { style.draft = style.saved; style.error = ""; }
    Object.keys(config).forEach(function (s) {
      config[s].draft = clone(config[s].saved);
      config[s].errors = {};
    });
  }

  // ---- loading ---------------------------------------------------------------------------

  function load() {
    loadState = "loading";
    render();
    return ask({ type: "settings_get" }).then(function (res) {
      if (!res || !res.ok || !res.settings) {
        loadState = res && res.unavailable ? "unavailable" : "error";
        loadError = errText(res);
        render();
        return;
      }
      adopt(res.settings);
      loadState = "ready";
      render();
    });
  }

  function adopt(s) {
    data = s;
    var t = s.theme || {};
    theme = { saved: clone(t.values || {}), draft: clone(t.values || {}), errors: {}, general: "",
      presetShown: t.active_preset || "" };
    var css = s.style_css || "";
    style = { saved: css, draft: css, error: "" };
    config = {};
    Object.keys(s.config || {}).forEach(function (sec) {
      config[sec] = { saved: clone(s.config[sec]), draft: clone(s.config[sec]), errors: {},
        general: "", restart: [] };
    });
  }

  function variables() { return (data && data.theme && data.theme.variables) || []; }
  function effective(v) {
    return Object.prototype.hasOwnProperty.call(theme.draft, v.name) ? theme.draft[v.name] : v.default;
  }

  // ---- frame -----------------------------------------------------------------------------

  function build() {
    if (built) return;
    built = true;
    var view = document.getElementById("view-settings");
    ui.view = view;

    var bar = el("header", "center-bar sp-bar");
    bar.appendChild(btn("← Back", "btn", function () { H().back(); }));
    bar.appendChild(el("span", "center-title", "Settings"));
    ui.tabs = el("nav", "sp-tabs");
    ui.tabs.setAttribute("role", "tablist");
    TABS.forEach(function (t) {
      var b = btn(t[1], "sp-tab", function () { H().navigate("#/settings/" + t[0]); });
      b.setAttribute("role", "tab");
      b.setAttribute("data-tab", t[0]);
      ui.tabs.appendChild(b);
    });
    bar.appendChild(ui.tabs);
    view.appendChild(bar);

    var test = el("div", "sp-testbar");
    test.appendChild(el("span", "sp-testlabel", "Send test notification"));
    [["normal", "Normal"], ["critical", "Critical"], ["buttons", "With buttons"]].forEach(function (k) {
      var b = btn(k[1], "btn btn-action", function () { sendTest(k[0]); });
      b.setAttribute("data-test", k[0]);
      test.appendChild(b);
    });
    ui.testResult = el("span", "sp-testresult");
    ui.testResult.setAttribute("role", "status");
    test.appendChild(ui.testResult);
    view.appendChild(test);

    ui.guard = el("div", "center-confirm sp-guard");
    ui.guard.hidden = true;
    ui.guard.setAttribute("role", "alert");
    ui.guard.appendChild(el("span", null, "You have unsaved changes. Leave and discard them?"));
    ui.guard.appendChild(btn("Discard and leave", "btn btn-danger", function () {
      var to = pendingHash || "#/";
      pendingHash = null;
      ui.guard.hidden = true;
      discardAll();
      H().navigate(to);
    }));
    ui.guard.appendChild(btn("Stay", "btn", function () {
      pendingHash = null;
      ui.guard.hidden = true;
    }));
    view.appendChild(ui.guard);

    var scroll = el("div", "center-scroll sp-scroll");
    ui.panel = el("div", "sp-panel");
    scroll.appendChild(ui.panel);
    view.appendChild(scroll);
  }

  function sendTest(kind) {
    ui.testResult.textContent = "Sending…";
    ui.testResult.classList.remove("error");
    ask({ type: "settings_send_test", kind: kind }).then(function (res) {
      var ok = res && res.ok;
      ui.testResult.classList.toggle("error", !ok);
      ui.testResult.textContent = ok ? "Sent a " + kind + " test notification." : "Could not send: " + errText(res);
    });
  }

  function show(t) {
    build();
    isActive = true;
    var prev = tab;
    tab = TABS.some(function (x) { return x[0] === t; }) ? t : "appearance";
    if (tab !== prev && ui.panel.parentNode) ui.panel.parentNode.scrollTop = 0;
    lastHash = "#/settings/" + tab;
    if (loadState === "idle") load(); else render();
  }

  function leave() {
    isActive = false;
    loadState = "idle";
    if (window.ncRules) window.ncRules.reset();
    pendingHash = null;
    if (ui.guard) ui.guard.hidden = true;
  }

  // ---- render ----------------------------------------------------------------------------

  function render() {
    if (!built) return;
    Array.prototype.forEach.call(ui.tabs.children, function (b) {
      var on = b.getAttribute("data-tab") === tab;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    clear(ui.panel);
    rows = {};
    groupResets = [];
    preview = null;
    if (loadState === "loading" || loadState === "idle") {
      ui.panel.appendChild(el("p", "sp-note", "Loading settings…"));
      return;
    }
    if (loadState === "unavailable") {
      ui.panel.appendChild(el("p", "sp-note sp-unavailable", "Settings unavailable"));
      return;
    }
    if (loadState === "error") {
      var p = el("p", "sp-note sp-error", "Could not load settings: " + loadError);
      ui.panel.appendChild(p);
      ui.panel.appendChild(btn("Retry", "btn", load));
      return;
    }
    if (tab === "appearance") renderAppearance(ui.panel);
    else if (tab === "behavior") renderBehavior(ui.panel);
    else if (tab === "sounds") renderSounds(ui.panel);
    else if (tab === "rules") renderRules(ui.panel);
    else renderAdvanced(ui.panel);
  }

  // ---- generic value widgets ----------------------------------------------------------------

  var probe = null;
  function computedColor(value) {
    if (!probe) {
      probe = el("span");
      probe.hidden = true;
      document.body.appendChild(probe);
    }
    probe.style.color = "";
    probe.style.color = value;
    if (!probe.style.color) return null;
    probe.hidden = false;
    var c = getComputedStyle(probe).color;
    probe.hidden = true;
    var m = /^rgba?\(([^)]+)\)$/.exec(c);
    if (!m) return null;
    var p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function hex2(n) { return ("0" + Math.max(0, Math.min(255, Math.round(n))).toString(16)).slice(-2); }
  function colorHex(value) {
    var c = computedColor(String(value));
    return c ? "#" + hex2(c.r) + hex2(c.g) + hex2(c.b) : "#000000";
  }
  function withAlpha(hex, previous) {
    var prev = computedColor(String(previous));
    if (!prev || prev.a >= 1) return hex;
    var r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
    return "rgba(" + r + ", " + g + ", " + b + ", " + prev.a + ")";
  }

  function colorWidget(value, emit) {
    var box = el("div", "sp-color");
    var sw = el("input", "sp-swatch");
    sw.type = "color";
    sw.value = colorHex(value);
    sw.setAttribute("aria-label", "Pick a color");
    var text = el("input", "sp-text");
    text.type = "text";
    text.value = value;
    text.spellcheck = false;
    text.setAttribute("aria-label", "Color value");
    sw.addEventListener("input", function () {
      var v = withAlpha(sw.value, text.value);
      text.value = v;
      emit(v);
    });
    text.addEventListener("input", function () {
      sw.value = colorHex(text.value);
      emit(text.value.trim());
    });
    box.appendChild(sw);
    box.appendChild(text);
    return box;
  }

  function parseUnit(value, units) {
    var m = /^(-?(?:\d+(?:\.\d+)?|\.\d+))\s*([a-z%]*)$/i.exec(String(value).trim());
    if (!m) return null;
    var unit = m[2] || units[0];
    if (units.indexOf(unit) < 0) return null;
    return { num: Number(m[1]), unit: unit };
  }
  function rangeFor(name, unit, current) {
    var base = RANGES[name] || [0, 100, 1];
    var lo = base[0], hi = base[1], step = base[2];
    if (unit === "rem" || unit === "em") { lo /= 16; hi /= 16; step = 0.05; }
    if (unit === "ms") { lo = 0; hi = 1500; step = 10; }
    if (unit === "s") { lo = 0; hi = 1.5; step = 0.05; }
    if (current > hi) hi = Math.ceil(current);
    if (current < lo) lo = Math.floor(current);
    return [lo, hi, step];
  }

  // A slider + number + unit select for lengths and durations. Falls back to a text field when
  // the value is not a plain number with a unit (calc(), var(), ...).
  function unitWidget(name, value, units, emit) {
    var p = parseUnit(value, units);
    if (!p) return textWidget(value, emit);
    var box = el("div", "sp-slider");
    var range = el("input", "sp-range");
    range.type = "range";
    var number = el("input", "sp-num");
    number.type = "number";
    number.step = "any";
    var unit = el("select", "sp-unit");
    units.forEach(function (u) { var o = el("option", null, u); o.value = u; unit.appendChild(o); });
    function config_(u, cur) {
      var rr = rangeFor(name, u, cur);
      range.min = String(rr[0]);
      range.max = String(rr[1]);
      range.step = String(rr[2]);
    }
    config_(p.unit, p.num);
    range.value = String(p.num);
    number.value = String(p.num);
    unit.value = p.unit;
    function fire() {
      if (number.value === "") return;
      emit(num(number.value) + unit.value);
    }
    range.addEventListener("input", function () { number.value = String(num(range.value)); fire(); });
    number.addEventListener("input", function () { range.value = number.value; fire(); });
    unit.addEventListener("change", function () {
      var cur = Number(number.value) || 0;
      config_(unit.value, cur);
      range.value = String(cur);
      fire();
    });
    box.appendChild(range);
    box.appendChild(number);
    box.appendChild(unit);
    return box;
  }

  function numberWidget(lo, hi, step, value, emit) {
    var box = el("div", "sp-slider sp-slider-plain");
    var range = el("input", "sp-range");
    range.type = "range";
    range.min = String(lo);
    range.max = String(hi);
    range.step = String(step);
    range.value = String(value);
    var number = el("input", "sp-num");
    number.type = "number";
    number.min = String(lo);
    number.max = String(hi);
    number.step = String(step);
    number.value = String(value);
    range.addEventListener("input", function () { number.value = range.value; emit(String(num(range.value))); });
    number.addEventListener("input", function () {
      range.value = number.value;
      if (number.value !== "") emit(String(num(number.value)));
    });
    box.appendChild(range);
    box.appendChild(number);
    return box;
  }

  function textWidget(value, emit, listId) {
    var t = el("input", "sp-text");
    t.type = "text";
    t.value = value;
    t.spellcheck = false;
    if (listId) t.setAttribute("list", listId);
    t.addEventListener("input", function () { emit(t.value); });
    return t;
  }

  function fontWidget(value, emit) {
    var box = el("div", "sp-font");
    var t = textWidget(value, emit);
    box.appendChild(t);
    var id = "sp-fonts";
    if (!document.getElementById(id)) {
      var dl = el("datalist");
      dl.id = id;
      (data.fonts || []).forEach(function (f) { var o = el("option"); o.value = f; dl.appendChild(o); });
      ui.view.appendChild(dl);
    }
    var pick = el("input", "sp-text sp-fontpick");
    pick.type = "text";
    pick.placeholder = "Add a font…";
    pick.setAttribute("list", id);
    pick.setAttribute("aria-label", "Add an installed font to the front of the list");
    pick.addEventListener("change", function () {
      var f = pick.value.trim();
      if (!f) return;
      var name = /[^A-Za-z0-9_-]/.test(f) ? '"' + f.replace(/"/g, "") + '"' : f;
      var rest = t.value.split(",").map(function (s) { return s.trim(); }).filter(function (s) {
        return s && s.replace(/^["']|["']$/g, "") !== f;
      });
      t.value = [name].concat(rest).join(", ");
      pick.value = "";
      emit(t.value);
    });
    box.appendChild(pick);
    return box;
  }

  function animWidget(name, value, emit) {
    var box = el("div", "sp-anim");
    var t = textWidget(value, function (v) { quick.value = ""; emit(v); });
    var quick = el("select", "sp-unit");
    var first = el("option", null, "Quick pick…");
    first.value = "";
    quick.appendChild(first);
    (QUICK_ANIM[name] || []).forEach(function (q) {
      var o = el("option", null, q[0]);
      o.value = q[1];
      quick.appendChild(o);
    });
    quick.addEventListener("change", function () {
      if (!quick.value) return;
      t.value = quick.value;
      emit(t.value);
    });
    box.appendChild(t);
    if (QUICK_ANIM[name]) box.appendChild(quick);
    return box;
  }

  // ---- Appearance ------------------------------------------------------------------------

  function setVar(v, value) {
    if (value === v.default) delete theme.draft[v.name]; else theme.draft[v.name] = value;
    theme.presetShown = "";
    delete theme.errors[v.name];
    applyPreview();
    refreshAppearanceState();
  }

  function resetVars(list) {
    list.forEach(function (v) { delete theme.draft[v.name]; delete theme.errors[v.name]; });
    theme.presetShown = "";
    render();
  }

  function refreshAppearanceState() {
    variables().forEach(function (v) {
      var r = rows[v.name];
      if (!r) return;
      var over = Object.prototype.hasOwnProperty.call(theme.draft, v.name);
      r.reset.disabled = !over;
      r.row.classList.toggle("changed", over);
      var e = theme.errors[v.name];
      r.err.textContent = e || "";
      r.err.hidden = !e;
    });
    groupResets.forEach(function (g) {
      g.btn.disabled = !g.vars.some(function (v) {
        return Object.prototype.hasOwnProperty.call(theme.draft, v.name);
      });
    });
    var d = themeDirty();
    if (ui.themeSave) { ui.themeSave.disabled = !d; ui.themeRevert.disabled = !d; }
    if (ui.themeState) {
      ui.themeState.textContent = d ? "Unsaved changes" : "Saved";
      ui.themeState.classList.toggle("dirty", d);
    }
    if (ui.themeGeneral) {
      ui.themeGeneral.textContent = theme.general || "";
      ui.themeGeneral.hidden = !theme.general;
    }
    Array.prototype.forEach.call(document.querySelectorAll(".sp-preset"), function (c) {
      c.classList.toggle("on", c.getAttribute("data-preset") === theme.presetShown);
    });
  }

  function presetList() {
    var raw = (data.theme && data.theme.presets) || [];
    var out = [];
    if (Array.isArray(raw)) {
      raw.forEach(function (p) {
        if (typeof p === "string") out.push({ name: p, label: labelOf(p) });
        else if (p && p.name) out.push({ name: p.name, label: p.label || labelOf(p.name), description: p.description });
      });
    } else if (raw && typeof raw === "object") {
      Object.keys(raw).forEach(function (k) {
        var v = raw[k];
        out.push({ name: k, label: typeof v === "string" ? v : (v && v.label) || labelOf(k) });
      });
    }
    return out;
  }
  function labelOf(name) {
    return { "default": "Default", "crt-green": "CRT Green", "night-ops-hud": "Night Ops HUD" }[name] || name;
  }

  function applyPreset(name) {
    return ask({ type: "settings_preset", name: name }).then(function (res) {
      if (!res || !res.ok) {
        theme.general = "Preset failed: " + errText(res);
        refreshAppearanceState();
        return;
      }
      var vals = res.values || {};
      theme.draft = {};
      variables().forEach(function (v) {
        if (Object.prototype.hasOwnProperty.call(vals, v.name) && vals[v.name] !== v.default) theme.draft[v.name] = vals[v.name];
      });
      theme.errors = {};
      theme.general = "";
      theme.presetShown = name;
      render();
    });
  }

  function saveTheme() {
    ui.themeSave.disabled = true;
    ask({ type: "settings_save_theme", values: theme.draft }).then(function (res) {
      if (res && res.ok) {
        var t = res.theme || {};
        theme.saved = clone(t.values || theme.draft);
        theme.draft = clone(theme.saved);
        theme.errors = {};
        theme.general = "";
        if (t.active_preset !== undefined) theme.presetShown = t.active_preset || "";
        if (data.theme) { data.theme.values = clone(theme.saved); data.theme.active_preset = theme.presetShown; }
        H().flash("Appearance saved");
      } else {
        var errs = (res && res.errors) || {};
        theme.errors = {};
        theme.general = "";
        Object.keys(errs).forEach(function (k) {
          if (rowsKnown(k)) theme.errors[k] = errs[k]; else theme.general += (theme.general ? "; " : "") + (k ? k + ": " : "") + errs[k];
        });
        if (!Object.keys(errs).length) theme.general = errText(res);
        var firstBad = Object.keys(theme.errors)[0];
        if (firstBad && rows[firstBad] && rows[firstBad].row.scrollIntoView) rows[firstBad].row.scrollIntoView({ block: "center" });
      }
      refreshAppearanceState();
    });
  }
  function rowsKnown(name) { return variables().some(function (v) { return v.name === name; }); }

  function renderAppearance(root) {
    var wrap = el("div", "sp-appearance");
    var left = el("div", "sp-controls");
    var right = el("aside", "sp-previewcol");

    // presets
    var ps = el("section", "sp-section");
    ps.appendChild(el("h3", null, "Presets"));
    var cards = el("div", "sp-presets");
    presetList().forEach(function (p) {
      var c = el("button", "sp-preset");
      c.type = "button";
      c.setAttribute("data-preset", p.name);
      c.appendChild(el("span", "sp-preset-name", p.label));
      c.appendChild(el("span", "sp-preset-desc", p.description || PRESET_INFO[p.name] || ""));
      c.addEventListener("click", function () { applyPreset(p.name); });
      cards.appendChild(c);
    });
    ps.appendChild(cards);
    ps.appendChild(el("p", "sp-hint", "A preset fills the controls and the preview. Nothing is written until you save."));
    left.appendChild(ps);

    // groups
    var groups = [];
    variables().forEach(function (v) {
      if (groups.indexOf(v.group) < 0) groups.push(v.group);
    });
    groups.forEach(function (g) {
      var vars = variables().filter(function (v) { return v.group === g; });
      var sec = el("section", "sp-section sp-group");
      sec.setAttribute("data-group", g);
      var head = el("div", "sp-grouphead");
      head.appendChild(el("h3", null, g));
      var rb = btn("Reset group", "btn btn-small", function () { resetVars(vars); }, "Reset every " + g + " value to its default");
      groupResets.push({ btn: rb, vars: vars });
      head.appendChild(rb);
      sec.appendChild(head);
      vars.forEach(function (v) { sec.appendChild(varRow(v)); });
      left.appendChild(sec);
    });

    // save bar
    var bar = el("div", "sp-savebar");
    ui.themeSave = btn("Save", "btn btn-primary", saveTheme);
    ui.themeRevert = btn("Revert", "btn", function () {
      theme.draft = clone(theme.saved);
      theme.errors = {};
      theme.general = "";
      theme.presetShown = (data.theme && data.theme.active_preset) || "";
      render();
    });
    ui.themeState = el("span", "sp-state");
    ui.themeGeneral = el("span", "sp-fielderror");
    ui.themeGeneral.hidden = true;
    bar.appendChild(ui.themeSave);
    bar.appendChild(ui.themeRevert);
    bar.appendChild(ui.themeState);
    bar.appendChild(ui.themeGeneral);
    left.appendChild(bar);

    right.appendChild(buildPreview());
    wrap.appendChild(left);
    wrap.appendChild(right);
    root.appendChild(wrap);
    applyPreview();
    refreshAppearanceState();
  }

  function varRow(v) {
    var row = el("div", "sp-field");
    row.setAttribute("data-var", v.name);
    var top = el("div", "sp-fieldtop");
    var lab = el("label", "sp-label", v.label);
    top.appendChild(lab);
    top.appendChild(el("code", "sp-var", v.name));
    var reset = btn("↺", "btn btn-small sp-reset", function () { resetVars([v]); }, "Reset " + v.label + " to its default");
    top.appendChild(reset);
    row.appendChild(top);

    var cur = effective(v);
    var emit = function (value) { setVar(v, value); };
    var w;
    if (v.type === "color") w = colorWidget(cur, emit);
    else if (v.type === "length") w = unitWidget(v.name, cur, ["px", "rem", "em", "%"], emit);
    else if (v.type === "duration") w = unitWidget(v.name, cur, ["ms", "s"], emit);
    else if (v.type === "number") {
      var lo = v.lo !== null && v.lo !== undefined ? v.lo : 0;
      var hi = v.hi !== null && v.hi !== undefined ? v.hi : 1;
      w = numberWidget(lo, hi, hi - lo <= 2 ? 0.01 : 1, Number(cur), emit);
    } else if (v.type === "font") w = fontWidget(cur, emit);
    else if (v.type === "animation") w = animWidget(v.name, cur, emit);
    else w = textWidget(cur, emit);
    row.appendChild(w);
    var firstInput = w.querySelector ? w.querySelector("input, select") : null;
    if (firstInput) { firstInput.id = "sp-" + v.name.slice(2); lab.setAttribute("for", firstInput.id); }

    var defl = el("div", "sp-default", "Default: " + v.default);
    row.appendChild(defl);
    var err = el("div", "sp-fielderror");
    err.setAttribute("role", "alert");
    err.hidden = true;
    row.appendChild(err);
    rows[v.name] = { row: row, reset: reset, err: err };
    return row;
  }

  // ---- live preview ----------------------------------------------------------------------

  function sampleToast(o) {
    var t = el("div", "toast");
    t.setAttribute("data-app", o.app);
    t.setAttribute("data-urgency", o.urgency);
    t.setAttribute("data-category", o.category || "");
    var icon = el("div", "toast-icon");
    icon.appendChild(window.ncAvatar(o.app));
    t.appendChild(icon);
    var c = el("div", "toast-content");
    c.appendChild(el("div", "toast-app", o.app));
    c.appendChild(el("div", "toast-summary", o.summary));
    var body = el("div", "toast-body");
    o.body.forEach(function (part) {
      if (part.mark) {
        var m = el("mark", "urgent", part.text);
        body.appendChild(m);
      } else {
        body.appendChild(document.createTextNode(part.text));
      }
    });
    c.appendChild(body);
    if (o.buttons) {
      var row = el("div", "toast-buttons");
      o.buttons.forEach(function (b) {
        var x = el("button", "toast-button", b.label);
        x.type = "button";
        x.setAttribute("data-kind", b.kind);
        if (b.builtin) { x.setAttribute("data-builtin", b.builtin); x.setAttribute("data-minutes", "10"); }
        row.appendChild(x);
      });
      c.appendChild(row);
    }
    t.appendChild(c);
    var close = el("button", "toast-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    t.appendChild(close);
    return t;
  }

  function sampleGroup() {
    var wrap = el("div", "shade sp-shade");
    var groups = el("div", "shade-groups");
    var sec = el("section", "shade-group expanded has-unread");
    sec.setAttribute("data-app", "Mail");
    sec.setAttribute("data-urgency", "normal");
    var head = el("div", "shade-group-head");
    var gi = el("div", "shade-group-icon");
    gi.appendChild(window.ncAvatar("Mail"));
    head.appendChild(gi);
    var main = el("div", "shade-group-main");
    var top = el("div", "shade-group-top");
    top.appendChild(el("span", "shade-group-name", "Mail"));
    top.appendChild(el("span", "shade-badge", "2"));
    top.appendChild(el("span", "shade-spacer"));
    top.appendChild(el("span", "shade-time", "2m"));
    top.appendChild(el("span", "shade-chevron", "›"));
    main.appendChild(top);
    head.appendChild(main);
    sec.appendChild(head);
    var itemsWrap = el("div", "shade-items-wrap");
    var items = el("div", "shade-items");
    [["Weekly report", "Numbers are in.", "normal", true], ["Build failed", "main is red again.", "critical", false]].forEach(function (d) {
      var it = el("article", "shade-item" + (d[3] ? " unread" : ""));
      it.setAttribute("data-urgency", d[2]);
      it.setAttribute("data-app", "Mail");
      var h = el("div", "shade-item-head");
      var ic = el("span", "shade-item-icon");
      ic.appendChild(window.ncAvatar("Mail"));
      h.appendChild(ic);
      h.appendChild(el("div", "shade-item-summary", d[0]));
      h.appendChild(el("span", "shade-time", d[3] ? "2m" : "1h"));
      var x = el("button", "shade-close", "×");
      x.type = "button";
      x.setAttribute("aria-label", "Dismiss");
      h.appendChild(x);
      it.appendChild(h);
      it.appendChild(el("div", "shade-item-body", d[1]));
      items.appendChild(it);
    });
    itemsWrap.appendChild(items);
    sec.appendChild(itemsWrap);
    groups.appendChild(sec);
    wrap.appendChild(groups);
    return wrap;
  }

  function buildPreview() {
    var box = el("section", "sp-section sp-previewbox");
    var head = el("div", "sp-grouphead");
    head.appendChild(el("h3", null, "Live preview"));
    head.appendChild(btn("Replay animation", "btn btn-small", function () { fillPreview(); }));
    box.appendChild(head);
    var root = el("div", "sp-preview");
    root.setAttribute("aria-label", "Preview of a notification with your edited values");
    box.appendChild(root);
    preview = { root: root };
    fillPreview();
    return box;
  }

  function fillPreview() {
    var root = preview.root;
    clear(root);
    root.appendChild(el("div", "sp-preview-label", "Toast"));
    var stack = el("div", "stack sp-stack");
    stack.appendChild(sampleToast({
      app: "Calendar", urgency: "normal", category: "reminder", summary: "Meeting at 10:00",
      body: [{ text: "Standup in room 4. Bring the " }, { text: "release notes", mark: true }, { text: "." }]
    }));
    root.appendChild(stack);
    root.appendChild(el("div", "sp-preview-label", "Critical, with buttons"));
    var stack2 = el("div", "stack sp-stack");
    stack2.appendChild(sampleToast({
      app: "CI Runner", urgency: "critical", category: "build", summary: "Build failed on main",
      body: [{ text: "Deploy blocked: 3 checks failing. " }, { text: "URGENT", mark: true }, { text: " review needed." }],
      buttons: [{ label: "Snooze 10 min", kind: "builtin", builtin: "snooze" }, { label: "Open log", kind: "run" }]
    }));
    root.appendChild(stack2);
    root.appendChild(el("div", "sp-preview-label", "Shade"));
    root.appendChild(sampleGroup());
    applyPreview();
  }

  function applyPreview() {
    if (!preview || !theme) return;
    variables().forEach(function (v) { preview.root.style.setProperty(v.name, effective(v)); });
  }

  // ---- config forms (Behavior, Sounds) --------------------------------------------------------

  function metaFor(section) { return (data.config_meta && data.config_meta[section]) || {}; }

  function cfgSetDraft(section, key, value) {
    var c = config[section];
    c.draft[key] = value;
    delete c.errors[key];
    refreshConfigState(section);
  }

  function configControl(section, key, m) {
    var c = config[section];
    var cur = c.draft[key];
    var type = m.type;
    if (type === "bool") {
      var cb = el("input", "sp-check");
      cb.type = "checkbox";
      cb.checked = !!cur;
      cb.addEventListener("change", function () { cfgSetDraft(section, key, cb.checked); });
      return cb;
    }
    if (type === "enum") {
      var sel = el("select", "sp-select");
      (m.choices || []).forEach(function (ch) {
        var o = el("option", null, String(ch));
        o.value = String(ch);
        sel.appendChild(o);
      });
      sel.value = cur === undefined || cur === null ? "" : String(cur);
      sel.addEventListener("change", function () { cfgSetDraft(section, key, sel.value); });
      return sel;
    }
    if (type === "volume") {
      var lo = 0, hi = 100;
      return numberWidget(lo, hi, 1, cur === undefined || cur === null ? 100 : cur, function (v) {
        cfgSetDraft(section, key, Math.round(Number(v)));
      });
    }
    if (type === "int" || type === "timeout") {
      var n = el("input", "sp-num sp-num-wide");
      if (type === "int") {
        n.type = "number";
        if (m.min !== undefined && m.min !== null) n.min = String(m.min);
        if (m.max !== undefined && m.max !== null) n.max = String(m.max);
        n.step = "1";
        n.addEventListener("input", function () {
          cfgSetDraft(section, key, n.value === "" ? null : Number(n.value));
        });
      } else {
        n.type = "text";
        n.addEventListener("input", function () {
          var t = n.value.trim();
          cfgSetDraft(section, key, /^\d+$/.test(t) ? Number(t) : t);
        });
      }
      n.value = cur === undefined || cur === null ? "" : String(cur);
      return n;
    }
    // path_or_theme and anything unknown: a text field
    var t = el("input", "sp-text");
    t.type = "text";
    t.spellcheck = false;
    t.value = cur === undefined || cur === null ? "" : String(cur);
    if (type === "path_or_theme") {
      t.setAttribute("list", "sp-sound-themes");
      t.placeholder = "Theme event name, or a file path (empty: system default)";
    }
    t.addEventListener("input", function () { cfgSetDraft(section, key, t.value === "" ? null : t.value); });
    return t;
  }

  function soundThemesList() {
    if (document.getElementById("sp-sound-themes")) return;
    var dl = el("datalist");
    dl.id = "sp-sound-themes";
    (data.sound_themes || []).forEach(function (s) { var o = el("option"); o.value = s; dl.appendChild(o); });
    ui.view.appendChild(dl);
  }

  var cfgRows = {};
  function configField(section, key, m) {
    var c = config[section];
    var row = el("div", "sp-field sp-cfg" + (m.type === "path_or_theme" ? " sp-cfg-wide" : ""));
    row.setAttribute("data-key", key);
    var top = el("div", "sp-fieldtop");
    var lab = el("label", "sp-label", m.label || key);
    top.appendChild(lab);
    top.appendChild(el("code", "sp-var", section + "." + key));
    var badge = el("span", "sp-badge");
    badge.hidden = true;
    top.appendChild(badge);
    row.appendChild(top);
    var line = el("div", "sp-cfgline");
    var ctl = configControl(section, key, m);
    line.appendChild(ctl);
    var first = ctl.matches && ctl.matches("input, select") ? ctl : ctl.querySelector("input, select");
    if (first) { first.id = "sp-cfg-" + section + "-" + key; lab.setAttribute("for", first.id); }
    var playResult = null;
    if (m.type === "path_or_theme") {
      playResult = el("div", "sp-playresult");
      playResult.setAttribute("role", "status");
      line.appendChild(btn("▶ Play", "btn btn-action sp-play", function () {
        playSound(section, key, playResult);
      }, "Play this sound"));
    }
    row.appendChild(line);
    if (m.help) row.appendChild(el("div", "sp-help", m.help));
    if (playResult) row.appendChild(playResult);
    var err = el("div", "sp-fielderror");
    err.setAttribute("role", "alert");
    err.hidden = true;
    row.appendChild(err);
    cfgRows[section + "." + key] = { badge: badge, err: err, meta: m, row: row };
    return row;
  }

  function playSound(section, key, out) {
    var c = config[section];
    var volume = 100;
    var sc = config[SOUND_SECTION];
    if (sc && typeof sc.draft.master_volume === "number") volume = sc.draft.master_volume;
    var spec = c.draft[key];
    out.classList.remove("error");
    out.textContent = "Playing…";
    ask({ type: "settings_preview_sound", spec: spec === null || spec === undefined ? "" : String(spec), volume: volume })
      .then(function (res) {
        if (res && res.ok) out.textContent = "Played: " + (res.resolved || "system default");
        else {
          out.classList.add("error");
          out.textContent = "Could not play: " + (res && (res.error || errText(res)) || "failed");
        }
      });
  }

  function refreshConfigState(section) {
    var c = config[section];
    var metas = metaFor(section);
    Object.keys(metas).forEach(function (key) {
      var r = cfgRows[section + "." + key];
      if (!r) return;
      var e = c.errors[key];
      r.err.textContent = e || "";
      r.err.hidden = !e;
      var changed = c.saved[key] !== c.draft[key];
      r.row.classList.toggle("changed", changed);
      if (c.restart.indexOf(key) >= 0) {
        r.badge.textContent = "restart required";
        r.badge.className = "sp-badge restart";
        r.badge.hidden = false;
      } else if (metas[key].restart) {
        r.badge.textContent = "applies after restart";
        r.badge.className = "sp-badge later";
        r.badge.hidden = false;
      } else {
        r.badge.hidden = true;
      }
    });
    var s = ui["cfg-" + section];
    if (s) {
      var d = configDirty(section);
      s.save.disabled = !d;
      s.revert.disabled = !d;
      s.state.textContent = d ? "Unsaved changes" : "";
      s.state.classList.toggle("dirty", d);
      s.general.textContent = c.general || "";
      s.general.hidden = !c.general;
      s.note.hidden = !c.restart.length;
    }
  }

  function saveConfig(section) {
    var c = config[section];
    var changes = {};
    Object.keys(c.draft).forEach(function (k) {
      if (c.saved[k] !== c.draft[k]) changes[k] = c.draft[k];
    });
    ask({ type: "settings_save_config", section: section, values: changes }).then(function (res) {
      if (res && res.ok) {
        c.errors = {};
        c.general = "";
        c.restart = res.restart_required || [];
        if (res.restart_command) ui["cfg-" + section].cmd.textContent = res.restart_command;
        return ask({ type: "settings_get" }).then(function (g) {
          if (g && g.ok && g.settings) {
            data.config = g.settings.config;
            var fresh = (g.settings.config || {})[section] || c.draft;
            c.saved = clone(fresh);
            c.draft = clone(fresh);
          } else {
            c.saved = clone(c.draft);
          }
          H().flash("Saved");
          rebuildIfActive();
        });
      }
      var errs = (res && res.errors) || {};
      c.errors = {};
      c.general = "";
      Object.keys(errs).forEach(function (k) {
        if (metaFor(section)[k]) c.errors[k] = errs[k]; else c.general += (c.general ? "; " : "") + (k ? k + ": " : "") + errs[k];
      });
      if (!Object.keys(errs).length) c.general = errText(res);
      rebuildIfActive();
    });
  }
  function rebuildIfActive() { if (isActive) render(); }

  function configSection(root, section, title) {
    var metas = metaFor(section);
    var sec = el("section", "sp-section");
    sec.setAttribute("data-section", section);
    sec.appendChild(el("h3", null, title));
    var keys = Object.keys(metas);
    if (!keys.length || !config[section]) {
      sec.appendChild(el("p", "sp-note", "No settings available here."));
      root.appendChild(sec);
      return;
    }
    keys.forEach(function (k) { sec.appendChild(configField(section, k, metas[k])); });
    var bar = el("div", "sp-savebar");
    var s = { save: null, revert: null, state: el("span", "sp-state"), general: el("span", "sp-fielderror"), note: null };
    s.save = btn("Save", "btn btn-primary", function () { saveConfig(section); });
    s.revert = btn("Revert", "btn", function () {
      config[section].draft = clone(config[section].saved);
      config[section].errors = {};
      config[section].general = "";
      render();
    });
    s.general.hidden = true;
    bar.appendChild(s.save);
    bar.appendChild(s.revert);
    bar.appendChild(s.state);
    bar.appendChild(s.general);
    sec.appendChild(bar);
    s.note = el("div", "sp-restart");
    s.note.hidden = true;
    s.note.appendChild(document.createTextNode("Some saved values need a restart to apply. Restart the notification daemon with "));
    s.cmd = el("code", null, "bin/notifyd --restart");
    s.note.appendChild(s.cmd);
    s.note.appendChild(document.createTextNode(" (about a second; the panel is not affected)."));
    sec.appendChild(s.note);
    ui["cfg-" + section] = s;
    root.appendChild(sec);
    refreshConfigState(section);
  }

  function renderBehavior(root) {
    cfgRows = {};
    soundThemesList();
    var wrap = el("div", "sp-forms");
    BEHAVIOR_SECTIONS.forEach(function (s) { configSection(wrap, s[0], s[1]); });
    root.appendChild(wrap);
  }

  function renderSounds(root) {
    cfgRows = {};
    soundThemesList();
    var wrap = el("div", "sp-forms");
    configSection(wrap, SOUND_SECTION, "Sound");
    wrap.appendChild(el("p", "sp-hint", "Per-app and per-rule sounds are set in the shade (gear on an app) and in rules.toml."));
    root.appendChild(wrap);
  }

  // ---- Rules (the editor lives in rules.js) -----------------------------------------------

  function renderRules(root) {
    soundThemesList();
    if (!window.ncRules) { root.appendChild(el("p", "sp-note sp-error", "The rules editor did not load.")); return; }
    window.ncRules.render(root);
  }

  window.ncSettingsKit = {
    el: el, btn: btn, ask: ask, errText: errText,
    flash: function (m) { H().flash(m); },
    soundThemes: function () { return (data && data.sound_themes) || []; },
    soundThemesList: function () { if (data) soundThemesList(); },
    onRulesTab: function () { return isActive && tab === "rules" && loadState === "ready"; }
  };

  // ---- Advanced --------------------------------------------------------------------------

  function refreshStyleState() {
    var d = styleDirty();
    ui.styleSave.disabled = !d;
    ui.styleRevert.disabled = !d;
    ui.styleState.textContent = d ? "Unsaved changes" : "";
    ui.styleState.classList.toggle("dirty", d);
    ui.styleErr.textContent = style.error || "";
    ui.styleErr.hidden = !style.error;
  }

  function renderAdvanced(root) {
    var sec = el("section", "sp-section sp-advanced");
    sec.appendChild(el("h3", null, "style.css"));
    var p = el("p", "sp-hint");
    p.appendChild(document.createTextNode("Hand-written CSS that loads after the Appearance values and wins over them. The list of CSS hooks is in "));
    p.appendChild(el("code", null, "docs/theming.md"));
    p.appendChild(document.createTextNode(". Saved changes apply to open toasts and this window right away."));
    sec.appendChild(p);
    var ta = el("textarea", "sp-css");
    ta.id = "sp-css";
    ta.spellcheck = false;
    ta.setAttribute("aria-label", "style.css");
    ta.setAttribute("wrap", "off");
    ta.value = style.draft;
    ta.addEventListener("input", function () { style.draft = ta.value; style.error = ""; refreshStyleState(); });
    ta.addEventListener("keydown", function (ev) {
      if (ev.key === "Tab" && !ev.shiftKey && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
        ev.preventDefault();
        var ok = false;
        try { ok = document.execCommand("insertText", false, "  "); } catch (e) { ok = false; }
        if (!ok) {
          ta.setRangeText("  ", ta.selectionStart, ta.selectionEnd, "end");
          ta.dispatchEvent(new Event("input"));
        }
      }
    });
    sec.appendChild(ta);
    sec.appendChild(el("p", "sp-hint", "Tab inserts two spaces. Press Esc, then Tab, to move focus out."));
    var bar = el("div", "sp-savebar");
    ui.styleSave = btn("Save", "btn btn-primary", function () {
      ask({ type: "settings_save_style", text: style.draft }).then(function (res) {
        if (res && res.ok) {
          style.saved = style.draft;
          style.error = "";
          data.style_css = style.saved;
          H().flash("style.css saved");
        } else {
          style.error = errText(res);
        }
        refreshStyleState();
      });
    });
    ui.styleRevert = btn("Revert", "btn", function () {
      style.draft = style.saved;
      style.error = "";
      ta.value = style.saved;
      refreshStyleState();
    });
    ui.styleState = el("span", "sp-state");
    ui.styleErr = el("span", "sp-fielderror");
    ui.styleErr.hidden = true;
    bar.appendChild(ui.styleSave);
    bar.appendChild(ui.styleRevert);
    bar.appendChild(ui.styleState);
    bar.appendChild(ui.styleErr);
    sec.appendChild(bar);
    root.appendChild(sec);
    refreshStyleState();
  }

  window.ncSettings = {
    show: show,
    leave: leave,
    active: function () { return isActive; },
    dirty: dirty,
    guard: guard
  };
})();
