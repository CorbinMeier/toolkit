// Dashboard shade. Notification content is untrusted: the DOM is built only with createElement
// and textContent (never an HTML-string setter), links go back to Python, and every action is a
// message. Python answers queries through window.nc.reply(req, obj) and pushes with
// window.nc.refresh() / setDnd(on) / setUserCss(text) / setLimits({maxHeight, width}) / measure().
(function () {
  "use strict";

  var ALLOWED = /^(https?:|mailto:)/i;

  function $(id) { return document.getElementById(id); }

  // ---- bridge ----------------------------------------------------------------------------

  var pending = new Map();
  var seq = 0;

  function post(msg) {
    try {
      window.webkit.messageHandlers.nc.postMessage(msg);
    } catch (e) { /* not hosted: ignore */ }
  }

  function ask(msg) {
    return new Promise(function (resolve) {
      msg.req = ++seq;
      pending.set(msg.req, resolve);
      post(msg);
    });
  }

  function reply(req, data) {
    var r = pending.get(req);
    if (!r) return;
    pending.delete(req);
    r(data);
  }

  // ---- DOM helpers -----------------------------------------------------------------------

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function button(label, cls, onclick, title) {
    var b = el("button", cls || "btn", label);
    b.type = "button";
    if (title) { b.title = title; b.setAttribute("aria-label", title); }
    b.addEventListener("click", function (ev) { ev.stopPropagation(); onclick(ev); });
    return b;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function slug(s) {
    return String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  }

  function addClasses(node, list) {
    (list || []).forEach(function (c) {
      String(c).split(/\s+/).forEach(function (t) { if (t) node.classList.add(t); });
    });
  }

  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  function when(ts) {
    var secs = Math.max(0, Date.now() / 1000 - ts);
    if (secs < 60) return "now";
    if (secs < 3600) return Math.floor(secs / 60) + "m";
    var d = new Date(ts * 1000), now = new Date();
    if (secs < 86400 && d.getDate() === now.getDate()) return pad(d.getHours()) + ":" + pad(d.getMinutes());
    if (secs < 86400) return Math.floor(secs / 3600) + "h";
    if (secs < 7 * 86400) return d.toLocaleDateString(undefined, { weekday: "short" });
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function iconBox(url, cls, app) {
    var box = el("span", cls);
    if (url) {
      var img = document.createElement("img");
      img.setAttribute("src", url);
      img.setAttribute("alt", "");
      box.appendChild(img);
    } else {
      box.appendChild(window.ncAvatar(app));
    }
    return box;
  }

  // ---- body runs (sanitized by Python; text nodes only) -----------------------------------

  function compile(pattern) {
    var flags = "g";
    var m = /^\(\?([ims]+)\)/.exec(pattern);
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
        addClasses(mark, [cls]);
        mark.textContent = m[0];
        frag.appendChild(mark);
        last = m.index + m[0].length;
      }
      if (!hit) return;
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });
  }

  function runNode(run, links) {
    var node = document.createTextNode(run.text);
    if (run.b) { var b = document.createElement("b"); b.appendChild(node); node = b; }
    if (run.i) { var i = document.createElement("i"); i.appendChild(node); node = i; }
    if (run.u) { var u = document.createElement("u"); u.appendChild(node); node = u; }
    if (links && run.href && ALLOWED.test(run.href)) {
      var a = document.createElement("a");
      a.setAttribute("href", run.href);
      a.setAttribute("title", run.href);
      a.appendChild(node);
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        post({ type: "link", href: run.href });
      });
      node = a;
    }
    return node;
  }

  function fillBody(box, runs, links) {
    (runs || []).forEach(function (run) { box.appendChild(runNode(run, links)); });
  }

  function applyHighlights(n, roots) {
    (n.highlights || []).forEach(function (h) {
      var re = compile(String(h.pattern));
      if (!re || !h["class"]) return;
      roots.forEach(function (root) { if (root) highlight(root, re, h["class"]); });
    });
  }

  function hooks(node, n) {
    node.setAttribute("data-urgency", n.urgency || "normal");
    node.setAttribute("data-state", n.state || "shown");
    node.setAttribute("data-category", n.category || "");
    (n.tags || []).forEach(function (t) { var s = slug(t); if (s) node.classList.add("tag-" + s); });
    addClasses(node, n.classes);
  }

  // ---- state -----------------------------------------------------------------------------

  var data = { groups: [], unread: 0, total: 0, dnd: false, buttons_enabled: false };
  var settings = new Map();   // app -> { loading, draft, error, saving } for open ⚙ cards
  var confirmOpen = false;

  // ---- rendering -------------------------------------------------------------------------

  function render() {
    var scroll = $("scroll");
    var top = scroll.scrollTop;
    var groups = $("groups");
    clear(groups);
    data.groups.forEach(function (g) { groups.appendChild(buildGroup(g)); });
    $("empty").hidden = data.groups.length > 0;
    $("unread").textContent = data.unread ? data.unread + " unread" : "All read";
    $("btn-clear").disabled = data.total === 0;
    renderDnd();
    if (data.total === 0) closeConfirm();
    else if (confirmOpen) $("confirm-text").textContent = "Dismiss all " + data.total + " notifications?";
    scroll.scrollTop = top;
    report();
  }

  function renderDnd() {
    var b = $("dnd");
    b.setAttribute("aria-checked", data.dnd ? "true" : "false");
    b.classList.toggle("on", !!data.dnd);
  }

  function ids(items) { return items.map(function (n) { return n.id; }); }

  function buildGroup(g) {
    var sec = el("section", "shade-group");
    sec.setAttribute("data-app", g.app);
    if (g.expanded) sec.classList.add("expanded");
    if (g.unread) sec.classList.add("has-unread");
    var newest = g.items[0];
    hooks(sec, newest);
    sec.removeAttribute("data-state");  // a group has no state of its own

    var head = el("div", "shade-group-head");
    head.setAttribute("role", "button");
    head.tabIndex = 0;
    head.setAttribute("aria-expanded", g.expanded ? "true" : "false");
    head.appendChild(iconBox(g.icon, "shade-group-icon", g.app));

    var main = el("div", "shade-group-main");
    var top = el("div", "shade-group-top");
    top.appendChild(el("span", "shade-group-name", g.app || "Unknown"));
    top.appendChild(el("span", "shade-badge", String(g.count)));
    top.appendChild(el("span", "shade-spacer"));
    var time = el("span", "shade-time", when(g.newest_at));
    top.appendChild(time);
    top.appendChild(el("span", "shade-chevron", "›"));
    main.appendChild(top);

    var peek = el("div", "shade-peek");
    var sum = el("div", "shade-peek-summary", newest.summary);
    var body = el("div", "shade-peek-body");
    fillBody(body, newest.body, false);
    peek.appendChild(sum);
    peek.appendChild(body);
    applyHighlights(newest, [sum, body]);
    var peekButtons = buildButtons(newest, "shade-buttons shade-peek-buttons");
    if (peekButtons) peek.appendChild(peekButtons);
    main.appendChild(peek);

    var tools = el("div", "shade-group-tools");
    tools.appendChild(button("Mark read", "btn btn-small", function () {
      post({ type: "mark_read_app", app: g.app });
    }, "Mark all read for " + g.app));
    tools.appendChild(button("Dismiss all", "btn btn-small", function () {
      post({ type: "dismiss_app", app: g.app });
    }, "Dismiss all for " + g.app));
    var gear = button("⚙", "btn btn-small shade-gear", function () { toggleSettings(g.app); },
      "Settings for " + g.app);
    gear.setAttribute("aria-expanded", settings.has(g.app) ? "true" : "false");
    tools.appendChild(gear);
    main.appendChild(tools);
    head.appendChild(main);

    function toggle() {
      var open = !sec.classList.contains("expanded");
      sec.classList.toggle("expanded", open);
      head.setAttribute("aria-expanded", open ? "true" : "false");
      post({ type: "expand", app: g.app, expanded: open });
    }
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", function (ev) {
      if (ev.target === head && (ev.key === "Enter" || ev.key === " ")) {
        ev.preventDefault();
        toggle();
      }
    });
    sec.appendChild(head);

    if (settings.has(g.app)) sec.appendChild(buildSettings(g.app));

    var wrap = el("div", "shade-items-wrap");
    var list = el("div", "shade-items");
    g.items.forEach(function (n) { list.appendChild(buildItem(n)); });
    wrap.appendChild(list);
    sec.appendChild(wrap);
    return sec;
  }

  // Rule buttons (pressed by id; the commands never reach the page). Hidden while Python has
  // nothing wired to handle them (buttons_enabled false).
  function buildButtons(n, cls) {
    var row = el("div", cls);
    (data.buttons_enabled ? n.buttons || [] : []).forEach(function (b) {
      var btn = button(b.label, "shade-button", function () {
        post({ type: "button", id: n.id, button: b.id });
      });
      btn.setAttribute("data-kind", b.kind || "run");
      btn.setAttribute("data-builtin", b.builtin || "");
      if (b.minutes) btn.setAttribute("data-minutes", String(b.minutes));
      row.appendChild(btn);
    });
    return row.childNodes.length ? row : null;
  }

  function buildItem(n) {
    var item = el("article", "shade-item");
    item.setAttribute("data-id", String(n.id));
    hooks(item, n);
    if (n.unread) item.classList.add("unread");
    if (n.expired) item.classList.add("expired");

    var head = el("div", "shade-item-head");
    head.appendChild(iconBox(n.icon, "shade-item-icon", n.app));
    var sum = el("div", "shade-item-summary", n.summary);
    head.appendChild(sum);
    var time = el("span", "shade-time", when(n.received_at));
    head.appendChild(time);
    head.appendChild(button("×", "shade-close", function () {
      post({ type: "dismiss", ids: [n.id] });
    }, "Dismiss"));
    item.appendChild(head);

    var body = el("div", "shade-item-body");
    fillBody(body, n.body, true);
    item.appendChild(body);
    applyHighlights(n, [sum, body]);

    var pic = window.ncImage.build(n, { post: post, host: "shade" });   // web/image.js
    if (pic) item.appendChild(pic);

    if ((n.actions || []).length) {
      var actions = el("div", "shade-actions");
      n.actions.forEach(function (a) {
        var b = button(a.label || a.key, "shade-action", function () {
          ask({ type: "action", id: n.id, key: a.key }).then(function (res) {
            b.classList.toggle("failed", !res || !res.ok);
          });
        });
        actions.appendChild(b);
      });
      item.appendChild(actions);
    }

    var rule = buildButtons(n, "shade-buttons");
    if (rule) item.appendChild(rule);

    var reply = window.ncReply.build(n, { post: post });   // Python sends reply only when wired
    if (reply) item.appendChild(reply);

    item.addEventListener("click", function () { post({ type: "details", id: n.id }); });
    ["contextmenu", "auxclick"].forEach(function (t) {
      item.addEventListener(t, function (ev) { ev.preventDefault(); });
    });
    return item;
  }

  // ---- per-app settings card --------------------------------------------------------------

  var TOGGLES = [
    ["muted", "Mute", "Record only: no toast, no sound"],
    ["silent", "Silent", "Show toasts without a sound"],
    ["sticky", "Sticky", "Toasts stay until dismissed"],
    ["blocked", "Block", "Hide entirely (kept in history)"],
    ["dnd_bypass", "Bypass Do not disturb", "Show and sound during Do not disturb"]
  ];

  function toggleSettings(app) {
    if (settings.has(app)) {
      settings.delete(app);
      render();
      return;
    }
    var st = { loading: true, draft: null, error: "", saving: false };
    settings.set(app, st);
    render();
    ask({ type: "prefs_get", app: app }).then(function (res) {
      if (settings.get(app) !== st) return;
      st.loading = false;
      if (!res || res.error || !res.prefs) {
        st.error = (res && res.error) || "Could not load settings";
        st.draft = {};
      } else {
        st.draft = Object.assign({}, res.prefs);
      }
      render();
    });
  }

  function buildSettings(app) {
    var st = settings.get(app);
    var card = el("div", "shade-settings");
    card.addEventListener("click", function (ev) { ev.stopPropagation(); });
    card.appendChild(el("div", "shade-settings-title", "Settings for " + app));
    if (st.loading) {
      card.appendChild(el("div", "shade-settings-loading", "Loading…"));
      return card;
    }
    var d = st.draft;

    TOGGLES.forEach(function (t) {
      var row = el("label", "shade-toggle");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!d[t[0]];
      cb.addEventListener("change", function () { d[t[0]] = cb.checked; });
      row.appendChild(cb);
      row.appendChild(el("span", "shade-toggle-name", t[1]));
      row.appendChild(el("span", "shade-toggle-hint", t[2]));
      card.appendChild(row);
    });

    var soundRow = el("label", "shade-field");
    soundRow.appendChild(el("span", "shade-field-name", "Sound (file path or theme name)"));
    var sound = document.createElement("input");
    sound.type = "text";
    sound.className = "shade-input";
    sound.placeholder = "default";
    sound.spellcheck = false;
    sound.value = d.sound || "";
    sound.addEventListener("input", function () { d.sound = sound.value; });
    soundRow.appendChild(sound);
    card.appendChild(soundRow);

    var iconRow = el("div", "shade-field shade-icon-field");
    iconRow.appendChild(el("span", "shade-field-name", "Icon (file path or theme name)"));
    var iconLine = el("span", "shade-icon-line");
    var preview = el("span", "shade-icon-preview");
    var iconIn = document.createElement("input");
    iconIn.type = "text";
    iconIn.className = "shade-input";
    iconIn.placeholder = "automatic";
    iconIn.setAttribute("aria-label", "Icon");
    iconIn.spellcheck = false;
    iconIn.value = d.icon || "";
    var iconErr = el("div", "shade-icon-error", "");
    iconErr.hidden = true;
    var previewSeq = 0;
    function showPreview() {
      var seq = ++previewSeq;
      var value = iconIn.value.trim();
      preview.textContent = "";
      iconErr.hidden = true;
      if (!value) { preview.appendChild(window.ncAvatar(app)); return; }
      ask({ type: "icon_preview", value: value }).then(function (res) {
        if (seq !== previewSeq) return;
        preview.textContent = "";
        if (res && res.uri) {
          var img = document.createElement("img");
          img.setAttribute("src", res.uri);
          img.setAttribute("alt", "");
          preview.appendChild(img);
        } else {
          preview.appendChild(window.ncAvatar(app));
          iconErr.textContent = (res && res.error) || "";
          iconErr.hidden = !iconErr.textContent;
        }
      });
    }
    var previewTimer = 0;
    iconIn.addEventListener("input", function () {
      d.icon = iconIn.value;
      clearTimeout(previewTimer);
      previewTimer = setTimeout(showPreview, 200);
    });
    var iconClear = button("Clear", "btn btn-small", function () {
      iconIn.value = "";
      d.icon = null;
      showPreview();
    }, "Use the automatic icon");
    iconLine.appendChild(preview);
    iconLine.appendChild(iconIn);
    iconLine.appendChild(iconClear);
    iconRow.appendChild(iconLine);
    iconRow.appendChild(iconErr);
    card.appendChild(iconRow);
    showPreview();

    var volRow = el("div", "shade-field shade-volume");
    var custom = document.createElement("input");
    custom.type = "checkbox";
    custom.checked = d.volume !== null && d.volume !== undefined;
    var range = document.createElement("input");
    range.type = "range";
    range.min = "0";
    range.max = "100";
    range.step = "1";
    range.value = String(custom.checked ? d.volume : 100);
    range.disabled = !custom.checked;
    var readout = el("span", "shade-volume-value", custom.checked ? range.value + "%" : "default");
    custom.addEventListener("change", function () {
      range.disabled = !custom.checked;
      d.volume = custom.checked ? parseInt(range.value, 10) : null;
      readout.textContent = custom.checked ? range.value + "%" : "default";
    });
    range.addEventListener("input", function () {
      d.volume = parseInt(range.value, 10);
      readout.textContent = range.value + "%";
    });
    var vname = el("label", "shade-field-name");
    vname.appendChild(custom);
    vname.appendChild(document.createTextNode(" Volume"));
    volRow.appendChild(vname);
    volRow.appendChild(range);
    volRow.appendChild(readout);
    card.appendChild(volRow);

    var err = el("div", "shade-error", st.error);
    err.hidden = !st.error;
    card.appendChild(err);

    var foot = el("div", "shade-settings-foot");
    var save = button(st.saving ? "Saving…" : "Save", "btn btn-primary", function () {
      st.saving = true;
      st.error = "";
      var prefs = {
        muted: !!d.muted, silent: !!d.silent, sticky: !!d.sticky, blocked: !!d.blocked,
        dnd_bypass: !!d.dnd_bypass,
        sound: (d.sound || "").trim() || null,
        icon: (d.icon || "").trim() || null,
        volume: (d.volume === undefined) ? null : d.volume
      };
      render();
      ask({ type: "prefs_set", app: app, prefs: prefs }).then(function (res) {
        st.saving = false;
        if (res && res.ok) {
          settings.delete(app);
        } else {
          st.error = (res && res.error) || "Could not save settings";
        }
        render();
      });
    });
    save.disabled = st.saving;
    foot.appendChild(save);
    foot.appendChild(button("Cancel", "btn", function () {
      settings.delete(app);
      render();
    }));
    card.appendChild(foot);
    return card;
  }

  // ---- top bar -----------------------------------------------------------------------------

  function closeConfirm() {
    confirmOpen = false;
    $("confirm").hidden = true;
  }

  $("dnd").addEventListener("click", function () {
    data.dnd = !data.dnd;
    renderDnd();
    post({ type: "dnd", on: data.dnd });
  });
  $("btn-clear").addEventListener("click", function () {
    if (!data.total) return;
    confirmOpen = true;
    $("confirm-text").textContent = "Dismiss all " + data.total + " notifications?";
    $("confirm").hidden = false;
    report();
  });
  $("confirm-yes").addEventListener("click", function () {
    closeConfirm();
    post({ type: "clear_all" });
    report();
  });
  $("confirm-no").addEventListener("click", function () { closeConfirm(); report(); });
  $("btn-center").addEventListener("click", function () { post({ type: "open_center" }); });

  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    ev.preventDefault();
    if (confirmOpen) { closeConfirm(); report(); return; }
    if (settings.size) { settings.clear(); render(); return; }
    post({ type: "close" });
  });
  document.addEventListener("contextmenu", function (ev) { ev.preventDefault(); });

  // ---- loading and size reports -------------------------------------------------------------

  function load() {
    return ask({ type: "state" }).then(function (res) {
      if (!res || res.error) return;
      data = res;
      render();
      post({ type: "perf", phase: "rows" }); // the daemon times the open from this
    });
  }

  var lastW = 0, lastH = 0, settle = 0;
  function send(w, h, r) {
    lastW = w;
    lastH = h;
    post({
      type: "size", width: w, height: h,
      room: Math.round(r.left),
      rect: [Math.round(r.left), Math.round(r.top), Math.ceil(r.width), Math.ceil(r.height)]
    });
  }
  // Grow at once (so content is never clipped), shrink after the animation settles.
  function report() {
    var shade = $("shade");
    var r = shade.getBoundingClientRect();
    var w = Math.ceil(r.right + r.left), h = Math.ceil(r.bottom + r.top);
    clearTimeout(settle);
    if (h > lastH || w !== lastW) { send(w, h, r); return; }
    if (h === lastH) return;
    settle = setTimeout(function () {
      var r2 = shade.getBoundingClientRect();
      send(Math.ceil(r2.right + r2.left), Math.ceil(r2.bottom + r2.top), r2);
    }, 220);
  }
  new ResizeObserver(report).observe($("shade"));

  // Python calls this on every open, once the surface is mapped: a size reported while the page
  // was hidden (prewarm) is dropped there, and the observer will not repeat an unchanged size.
  function measure() {
    lastW = 0;
    lastH = 0;
    report();
  }

  function setUserCss(text) {
    var style = document.getElementById("user");
    if (!style) {
      style = document.createElement("style");
      style.id = "user";
      document.head.appendChild(style);
    }
    style.textContent = String(text);
    report();
  }

  function setLimits(lim) {
    var root = document.documentElement.style;
    if (lim && lim.maxHeight > 0) root.setProperty("--shade-max-h", Math.floor(lim.maxHeight) + "px");
    if (lim && lim.width > 0) root.setProperty("--nc-shade-width", Math.floor(lim.width) + "px");
    report();
  }

  function setDnd(on) {
    data.dnd = !!on;
    renderDnd();
  }

  window.nc = { reply: reply, refresh: load, setDnd: setDnd, setUserCss: setUserCss, setLimits: setLimits, measure: measure, replyResult: window.ncReply.deliver, imageResult: window.ncImage.deliver };
  load();
})();
