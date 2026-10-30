// Notification center. Notification content is untrusted: the DOM is built only with
// createElement and textContent (never an HTML-string setter), and every action goes back to
// Python as a message. Python answers queries through window.nc.reply(req, obj).
(function () {
  "use strict";

  var PAGE = 50;
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

  // ---- small DOM helpers -----------------------------------------------------------------

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function button(label, cls, onclick) {
    var b = el("button", cls || "btn", label);
    b.type = "button";
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

  // The shared hooks for rows and the details page: data-app / data-urgency / data-state /
  // data-category, .tag-<name>, plus the rule classes.
  function hooks(node, n) {
    node.setAttribute("data-app", n.app || "");
    node.setAttribute("data-urgency", n.urgency || "normal");
    node.setAttribute("data-state", n.state || "shown");
    node.setAttribute("data-category", n.category || "");
    (n.tags || []).forEach(function (t) { var s = slug(t); if (s) node.classList.add("tag-" + s); });
    addClasses(node, n.classes);
  }

  var flashTimer = 0;
  function flash(text, isError) {
    var f = $("flash");
    f.textContent = text;
    f.classList.toggle("error", !!isError);
    f.hidden = false;
    clearTimeout(flashTimer);
    flashTimer = setTimeout(function () { f.hidden = true; }, 2200);
  }

  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function dayKey(ts) {
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }
  function dayLabel(ts) {
    var d = new Date(ts * 1000), now = new Date();
    var start = function (x) { return new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime(); };
    var diff = Math.round((start(now) - start(d)) / 86400000);
    if (diff === 0) return "Today";
    if (diff === 1) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  }
  function clock(ts) {
    var d = new Date(ts * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function stamp(ts) {   // a time that also says the day when it is not today
    if (dayKey(ts) === dayKey(Date.now() / 1000)) return clock(ts);
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + clock(ts);
  }
  function full(ts) {
    return ts ? new Date(ts * 1000).toLocaleString() : "";
  }

  function icon(url, cls, app) {
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

  function runNode(run) {
    var node = document.createTextNode(run.text);
    if (run.b) { var b = document.createElement("b"); b.appendChild(node); node = b; }
    if (run.i) { var i = document.createElement("i"); i.appendChild(node); node = i; }
    if (run.u) { var u = document.createElement("u"); u.appendChild(node); node = u; }
    if (run.href && ALLOWED.test(run.href)) {
      var a = document.createElement("a");
      a.setAttribute("href", run.href);
      a.setAttribute("title", run.href);
      a.appendChild(node);
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        post({ type: "link", href: run.href });
      });
      node = a;
    }
    return node;
  }

  // ---- state -----------------------------------------------------------------------------

  var list = {
    items: [],        // loaded rows, newest first
    byId: new Map(),
    hasMore: false,
    loaded: false,    // the first page has arrived
    loading: false,
    gen: 0,           // bumped on every reset so stale replies are dropped
    selected: null,   // id of the keyboard-selected row
    selHead: null     // data-key of the keyboard-selected section header (instead of a row)
  };
  var filters = { q: "", app: "", state: "", tag: "", unread: false };
  // The saved view state (Python keeps it in center.json): overrides of the defaults, which are
  // today expanded, older days collapsed, app groups expanded. While a search or a filter is
  // active every section is open and clicks only touch `transient`, so the saved state is
  // never overwritten and clearing the filter restores it.
  var view = { groupBy: "day", days: {}, groups: {}, apps: {} };
  var transient = new Map();
  var filterSig = "";
  var saveTimer = 0;
  var MAX_KEYS = { days: 500, apps: 500, groups: 2000, text: 500 };
  var current = null;   // the notification shown on the details page
  var depth = 0;        // in-app navigations, so Back knows whether history can go back

  // ---- list view -------------------------------------------------------------------------

  function filtersMatch(n) {
    if (filters.app && n.app !== filters.app) return false;
    if (filters.state && n.state !== filters.state) return false;
    if (filters.tag && (n.tags || []).indexOf(filters.tag) < 0) return false;
    if (filters.unread && !n.unread) return false;
    return true;
  }

  function mergeItems(items) {
    items.forEach(function (n) {
      if (list.byId.has(n.id)) {
        var old = list.byId.get(n.id);
        list.items[list.items.indexOf(old)] = n;
      } else {
        list.items.push(n);
      }
      list.byId.set(n.id, n);
    });
    list.items.sort(function (a, b) { return b.received_at - a.received_at || b.id - a.id; });
  }

  function loadPage(reset) {
    if (reset) {
      list.gen++;
      list.items = [];
      list.byId = new Map();
      list.hasMore = false;
      list.loaded = false;
      list.loading = false;
    }
    if (list.loading) return Promise.resolve();
    list.loading = true;
    var gen = list.gen;
    return ask({
      type: "list", q: filters.q, offset: list.items.length, limit: PAGE,
      app: filters.app, state: filters.state, tag: filters.tag, unread_only: filters.unread
    }).then(function (res) {
      if (gen !== list.gen) return;
      list.loading = false;
      list.loaded = true;
      if (res.error) { flash(res.error, true); return; }
      mergeItems(res.items);
      list.hasMore = res.has_more;
      renderList();
    });
  }

  function loadFacets() {
    return ask({ type: "facets" }).then(function (res) {
      if (res.error) return;
      fillSelect($("f-app"), res.apps, "All apps", filters.app);
      fillSelect($("f-tag"), res.tags, "Any tag", filters.tag);
      updateMoreDot();
    });
  }

  function fillSelect(sel, values, firstLabel, keep) {
    clear(sel);
    var first = el("option", null, firstLabel);
    first.value = "";
    sel.appendChild(first);
    values.forEach(function (v) {
      var o = el("option", null, v);
      o.value = v;
      sel.appendChild(o);
    });
    sel.value = values.indexOf(keep) >= 0 ? keep : "";
  }

  // ---- collapsible sections --------------------------------------------------------------

  function filtering() {
    return !!(filters.q || filters.app || filters.state || filters.tag || filters.unread);
  }

  function isOpen(kind, day, app) {
    if (filtering()) {
      var t = transient.get(sectionKey(kind, day, app));
      return t === undefined ? true : t;
    }
    if (kind === "day") {
      return view.days[day] !== undefined ? view.days[day] : day === dayKey(Date.now() / 1000);
    }
    if (kind === "app") return view.apps[app] !== undefined ? view.apps[app] : true;
    var g = view.groups[day];
    return g && g[app] !== undefined ? g[app] : true;
  }

  function sectionKey(kind, day, app) {
    return kind + ":" + (day || "") + "\u001f" + (app || "");
  }

  function setOpen(kind, day, app, open) {
    if (filtering()) { transient.set(sectionKey(kind, day, app), open); return; }
    if (kind === "day") {
      view.days[day] = open;
    } else if (kind === "app") {
      if (open) delete view.apps[app]; else view.apps[app] = false;   // true is the default
    } else {
      var g = view.groups[day] || (view.groups[day] = {});
      if (open) delete g[app]; else g[app] = false;
      if (!Object.keys(g).length) delete view.groups[day];
    }
    scheduleSave();
  }

  // What is saved is bounded to what Python accepts: newest days and short app names only.
  function expandPayload() {
    var short = function (k) { return k.length <= MAX_KEYS.text; };
    var days = {}, apps = {}, groups = {}, total = 0;
    Object.keys(view.days).sort().reverse().slice(0, MAX_KEYS.days).forEach(function (d) { days[d] = view.days[d]; });
    Object.keys(view.apps).filter(short).slice(0, MAX_KEYS.apps).forEach(function (a) { apps[a] = view.apps[a]; });
    Object.keys(view.groups).sort().reverse().slice(0, MAX_KEYS.days).forEach(function (d) {
      Object.keys(view.groups[d]).filter(short).forEach(function (a) {
        if (total >= MAX_KEYS.groups) return;
        (groups[d] || (groups[d] = {}))[a] = view.groups[d][a];
        total++;
      });
    });
    return { type: "set_expand", days: days, groups: groups, apps: apps };
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { post(expandPayload()); }, 200);
  }

  function loadView() {
    return ask({ type: "center_state" }).then(function (res) {
      if (!res || res.error) return;
      view.groupBy = res.group_by === "app" ? "app" : "day";
      view.days = res.days || {};
      view.groups = res.groups || {};
      view.apps = res.apps || {};
      $("f-group").value = view.groupBy;
    });
  }

  function groupItems(items) {
    var days = [], dayIdx = {};
    items.forEach(function (n) {
      var k = dayKey(n.received_at);
      var day = dayIdx[k];
      if (!day) { day = dayIdx[k] = { key: k, ts: n.received_at, apps: [], appIdx: {}, items: [] }; days.push(day); }
      day.items.push(n);
      var g = day.appIdx[n.app];
      if (!g) { g = day.appIdx[n.app] = { app: n.app, items: [] }; day.apps.push(g); }
      g.items.push(n);
    });
    return days; // items arrive newest first, so days, apps and rows are all newest first
  }

  function groupByApp(items) {
    var apps = [], idx = {};
    items.forEach(function (n) {
      var g = idx[n.app];
      if (!g) { g = idx[n.app] = { app: n.app, items: [] }; apps.push(g); }
      g.items.push(n);
    });
    return apps;
  }

  function countOf(items) { return items.length + (list.hasMore ? "+" : ""); }
  function unreadOf(items) { return items.filter(function (n) { return n.unread; }).length; }

  // A header: chevron + content, a button for the keyboard. `key` ties it to the cursor.
  function headFor(cls, key, open) {
    var head = el("div", cls + " center-head");
    head.setAttribute("role", "button");
    head.setAttribute("tabindex", "0");
    head.setAttribute("aria-expanded", open ? "true" : "false");
    head.setAttribute("data-key", key);
    head.appendChild(el("span", "center-chev", "›"));
    return head;
  }

  // Wraps the rows in the grid box the open/close animation runs on.
  function bodyFor() {
    var body = el("div", "center-body"), inner = el("div", "center-inner");
    body.appendChild(inner);
    return { body: body, inner: inner };
  }

  function dayFor(day) {
    var open = isOpen("day", day.key);
    var sec = el("section", "day center-day" + (open ? "" : " collapsed"));
    sec.setAttribute("data-day", day.key);
    sec.setAttribute("data-kind", "day");
    var head = headFor("day-title", sectionKey("day", day.key), open);
    head.appendChild(el("span", "day-label", dayLabel(day.ts)));
    head.appendChild(el("span", "day-count", day.last && list.hasMore ? day.items.length + "+" : String(day.items.length)));
    var unread = unreadOf(day.items);
    if (unread) head.appendChild(el("span", "day-unread", unread + " unread"));
    sec.appendChild(head);
    var b = bodyFor();
    day.apps.forEach(function (g) { b.inner.appendChild(groupFor(g, "group", day.key, false)); });
    sec.appendChild(b.body);
    return sec;
  }

  // One app's group: inside a day ("group") or across all days ("app").
  function groupFor(g, kind, day, partial) {
    var open = isOpen(kind, day, g.app);
    var sec = el("div", "app-group center-group" + (open ? "" : " collapsed"));
    sec.setAttribute("data-app", g.app);
    sec.setAttribute("data-kind", kind);
    if (day) sec.setAttribute("data-day", day);
    var head = headFor("app-title", sectionKey(kind, day, g.app), open);
    var newest = g.items[0];
    head.appendChild(icon(newest.icon, "group-icon", newest.app));
    head.appendChild(el("span", "app-name", g.app || "(unknown app)"));
    head.appendChild(el("span", "app-count", partial ? g.items.length + "+" : String(g.items.length)));
    var unread = unreadOf(g.items);
    if (unread) head.appendChild(el("span", "group-unread", String(unread)));
    head.appendChild(el("span", "group-summary", newest.summary));   // shown while collapsed
    var t = el("time", "group-time", kind === "app" ? stamp(newest.received_at) : clock(newest.received_at));
    t.setAttribute("title", full(newest.received_at));
    head.appendChild(t);
    sec.appendChild(head);
    var b = bodyFor();
    g.items.forEach(function (n) { b.inner.appendChild(rowFor(n, kind === "app")); });
    sec.appendChild(b.body);
    return sec;
  }

  function renderList() {
    var groups = $("groups");
    var sig = JSON.stringify(filters);
    if (sig !== filterSig) { filterSig = sig; transient.clear(); }
    var refocus = document.activeElement && document.activeElement.classList &&
      document.activeElement.classList.contains("center-head") ? list.selHead : null;
    clear(groups);
    groups.classList.toggle("by-app", view.groupBy === "app");
    if (view.groupBy === "app") {
      groupByApp(list.items).forEach(function (g) {
        groups.appendChild(groupFor(g, "app", "", !!list.hasMore));
      });
    } else {
      var days = groupItems(list.items);
      days.forEach(function (day, i) {
        day.last = i === days.length - 1;
        groups.appendChild(dayFor(day));
      });
    }
    $("list-empty").hidden = list.items.length > 0 || list.loading;
    $("more").hidden = !list.hasMore;
    var unread = list.items.filter(function (n) { return n.unread; }).length;
    $("list-status").textContent = list.items.length + " loaded" + (list.hasMore ? "+" : "") +
      (unread ? " · " + unread + " unread" : "");
    if (list.selected !== null && !list.byId.has(list.selected)) list.selected = null;
    markSelected(false);
    if (refocus) {
      var h = headByKey(refocus);
      if (h) h.focus({ preventScroll: true });
    }
    updateToggleAll();
  }

  function topSections() {
    return Array.prototype.slice.call(document.querySelectorAll(
      view.groupBy === "app" ? "#groups > .center-group" : "#groups > .center-day"));
  }

  function setSection(sec, open, save) {
    sec.classList.toggle("collapsed", !open);
    var head = sec.querySelector(".center-head");
    head.setAttribute("aria-expanded", open ? "true" : "false");
    if (save !== false) setOpen(sec.getAttribute("data-kind"), sec.getAttribute("data-day") || "",
      sec.getAttribute("data-app") || "", open);
  }

  function toggleSection(sec) {
    setSection(sec, sec.classList.contains("collapsed"));
    updateToggleAll();
  }

  // "Collapse all" collapses the top level (days, or apps in app-only mode) and keeps what the
  // groups inside remember; "Expand all" opens everything.
  function toggleAll() {
    var top = topSections();
    var collapse = top.some(function (s) { return !s.classList.contains("collapsed"); });
    if (!collapse && !filtering()) view.groups = {};
    top.forEach(function (s) { setSection(s, !collapse); });
    if (!collapse) {
      Array.prototype.forEach.call(document.querySelectorAll("#groups .center-day .center-group"), function (g) {
        if (g.classList.contains("collapsed")) {
          setSection(g, true, false);
          if (filtering()) setOpen("group", g.getAttribute("data-day"), g.getAttribute("data-app"), true);
        }
      });
    }
    updateToggleAll();
  }

  function updateToggleAll() {
    var top = topSections();
    var btn = $("btn-toggle-all");
    var anyOpen = top.some(function (s) { return !s.classList.contains("collapsed"); });
    btn.textContent = anyOpen ? "Collapse all" : "Expand all";
    btn.setAttribute("data-mode", anyOpen ? "collapse" : "expand");
    btn.disabled = top.length === 0;
  }

  function headByKey(key) {
    var heads = document.querySelectorAll("#groups .center-head");
    for (var i = 0; i < heads.length; i++) if (heads[i].getAttribute("data-key") === key) return heads[i];
    return null;
  }

  // Header clicks (delegated): one listener for every section.
  $("groups").addEventListener("click", function (ev) {
    var head = ev.target.closest && ev.target.closest(".center-head");
    if (!head) return;
    list.selHead = head.getAttribute("data-key");
    list.selected = null;
    toggleSection(head.parentNode);
    markSelected(false);
  });
  $("groups").addEventListener("focusin", function (ev) {
    var head = ev.target.classList && ev.target.classList.contains("center-head") ? ev.target : null;
    if (!head) return;
    list.selHead = head.getAttribute("data-key");
    list.selected = null;
    markSelected(false);
  });

  function rowFor(n, withDay) {
    var row = el("div", "row");
    row.setAttribute("data-id", String(n.id));
    row.setAttribute("tabindex", "-1");
    hooks(row, n);
    if (n.unread) row.classList.add("unread");
    if (n.id === list.selected) row.classList.add("selected");

    row.appendChild(icon(n.icon, "row-icon", n.app));

    var main = el("div", "row-main");
    var head = el("div", "row-head");
    head.appendChild(el("span", "row-app", n.app));
    head.appendChild(el("span", "row-summary", n.summary));
    main.appendChild(head);
    if (n.preview) main.appendChild(el("div", "row-preview", n.preview));
    row.appendChild(main);
    var thumb = window.ncImage.thumb(n);   // web/image.js
    if (thumb) row.appendChild(thumb);

    var meta = el("div", "row-meta");
    var badges = el("span", "row-badges");
    if (n.unread) badges.appendChild(el("span", "badge badge-unread", "unread"));
    if (n.state !== "shown") badges.appendChild(el("span", "badge badge-state badge-" + n.state, n.state.replace("_", " ")));
    if (n.urgency === "critical") badges.appendChild(el("span", "badge badge-critical", "critical"));
    meta.appendChild(badges);
    var t = el("time", "row-time", withDay ? stamp(n.received_at) : clock(n.received_at));
    t.setAttribute("title", full(n.received_at));
    meta.appendChild(t);
    row.appendChild(meta);

    var acts = el("div", "row-actions");
    if (n.unread) {
      var rb = button("✓", "row-btn row-read", function () { markRead([n.id]); });
      rb.setAttribute("title", "Mark read");
      rb.setAttribute("aria-label", "Mark read");
      acts.appendChild(rb);
    }
    var db = button("×", "row-btn row-delete", function () { removeItems([n.id]); });
    db.setAttribute("title", "Delete");
    db.setAttribute("aria-label", "Delete");
    acts.appendChild(db);
    row.appendChild(acts);

    row.addEventListener("click", function () { openDetail(n.id); });
    return row;
  }

  function markSelected(scroll) {
    var rows = document.querySelectorAll("#groups .row, #groups .center-head");
    Array.prototype.forEach.call(rows, function (r) {
      var on = r.classList.contains("row")
        ? r.getAttribute("data-id") === String(list.selected)
        : list.selHead !== null && r.getAttribute("data-key") === list.selHead;
      r.classList.toggle("selected", on);
      if (on && scroll) r.scrollIntoView({ block: "nearest" });
    });
  }

  // Everything the keyboard can land on: section headers and the rows of open sections.
  function navItems() {
    var all = document.querySelectorAll("#groups .center-head, #groups .row");
    return Array.prototype.filter.call(all, function (x) {
      var start = x.classList.contains("center-head") ? x.parentElement.parentElement : x.parentElement;
      return !(start && start.closest(".collapsed"));
    });
  }

  function selectItem(item) {
    if (item.classList.contains("center-head")) {
      list.selHead = item.getAttribute("data-key");
      list.selected = null;
      item.focus({ preventScroll: true });
    } else {
      list.selected = Number(item.getAttribute("data-id"));
      list.selHead = null;
      var a = document.activeElement;
      if (a && a.classList && a.classList.contains("center-head")) a.blur();
    }
    markSelected(true);
  }

  function moveSelection(delta) {
    var items = navItems();
    if (!items.length) return;
    var idx = items.findIndex(function (x) { return x.classList.contains("selected"); });
    idx = idx < 0 ? (delta > 0 ? 0 : items.length - 1) : Math.min(items.length - 1, Math.max(0, idx + delta));
    selectItem(items[idx]);
    if (idx >= items.length - 3 && list.hasMore) loadPage(false);
  }

  // h / l / arrows: collapse or expand the section under the cursor (for a row: its group).
  function foldCursor(open) {
    var el_ = document.querySelector("#groups .center-head.selected, #groups .row.selected");
    if (!el_) return;
    var sec = el_.classList.contains("row") ? el_.closest(".center-group") : el_.parentNode;
    if (!sec) return;
    if (sec.classList.contains("collapsed") === !open) return;
    setSection(sec, open);
    if (el_.classList.contains("row")) selectItem(sec.querySelector(".center-head"));
    updateToggleAll();
  }

  // ---- commands --------------------------------------------------------------------------

  function markRead(ids) {
    post({ type: "mark_read", ids: ids });
    var now = Date.now() / 1000;
    ids.forEach(function (id) {
      var n = list.byId.get(id);
      if (n) n.unread = false;
      if (current && current.id === id) { current.unread = false; if (!current.read_at) current.read_at = now; }
    });
    if (filters.unread) {
      list.items = list.items.filter(function (n) { return n.unread; });
      list.byId = new Map(list.items.map(function (n) { return [n.id, n]; }));
    }
    renderList();
  }

  function removeItems(ids) {
    post({ type: "delete", ids: ids });
    list.items = list.items.filter(function (n) { return ids.indexOf(n.id) < 0; });
    list.byId = new Map(list.items.map(function (n) { return [n.id, n]; }));
    renderList();
    loadFacets();
  }

  function clearHistory() {
    post({ type: "clear" });
    list.gen++;
    list.items = [];
    list.byId = new Map();
    list.hasMore = false;
    list.loaded = true;
    renderList();
    loadFacets();
  }

  // ---- details view ----------------------------------------------------------------------

  function kvRow(table, key, value) {
    var tr = document.createElement("tr");
    tr.appendChild(el("th", null, key));
    var td = document.createElement("td");
    if (value instanceof Node) td.appendChild(value); else td.textContent = value;
    tr.appendChild(td);
    table.appendChild(tr);
  }

  function chips(values, cls) {
    var box = el("span", "chips");
    values.forEach(function (v) { box.appendChild(el("span", cls || "chip", v)); });
    return box;
  }

  function hintText(v) {
    if (typeof v === "string") return v;
    return JSON.stringify(v);
  }

  function renderDetail(n) {
    current = n;
    $("d-notfound").hidden = true;
    $("d-content").hidden = false;
    var art = $("detail");
    art.className = "detail";
    hooks(art, n);
    art.classList.toggle("unread", !!n.unread);

    var head = $("d-head");
    clear(head);
    head.appendChild(icon(n.icon, "detail-icon", n.app));
    var titles = el("div", "detail-titles");
    titles.appendChild(el("div", "detail-app", n.app || "(unknown app)"));
    var h = el("h1", "detail-summary");
    h.textContent = n.summary;
    (n.highlights || []).forEach(function (hl) {
      var re = compile(String(hl.pattern));
      if (re && hl["class"]) highlight(h, re, hl["class"]);
    });
    titles.appendChild(h);
    var line = el("div", "detail-line");
    line.appendChild(el("span", "badge badge-urgency badge-" + n.urgency, n.urgency));
    if (n.state !== "shown") line.appendChild(el("span", "badge badge-state badge-" + n.state, n.state.replace("_", " ")));
    line.appendChild(el("span", "detail-time", full(n.received_at)));
    titles.appendChild(line);
    head.appendChild(titles);

    var body = $("d-body");
    clear(body);
    (n.body || []).forEach(function (run) { body.appendChild(runNode(run)); });
    (n.highlights || []).forEach(function (hl) {
      var re = compile(String(hl.pattern));
      if (re && hl["class"]) highlight(body, re, hl["class"]);
    });
    body.hidden = !(n.body || []).length;

    var shot = $("d-image");
    clear(shot);
    var big = window.ncImage.build(n, { post: post, host: "center", open: true });
    if (big) shot.appendChild(big);
    shot.hidden = !big;

    var tools = $("d-tools");
    clear(tools);
    tools.appendChild(button("Copy body", "btn", function () {
      post({ type: "copy", id: n.id, what: "body" });
      flash("Body copied");
    }));
    tools.appendChild(button("Copy summary", "btn", function () {
      post({ type: "copy", id: n.id, what: "summary" });
      flash("Summary copied");
    }));
    (n.links || []).forEach(function (lk) {
      if (!ALLOWED.test(lk.href)) return;
      var b = button("Open link", "btn btn-link", function () { post({ type: "link", href: lk.href }); });
      b.setAttribute("title", lk.href);
      b.appendChild(el("span", "link-href", lk.href));
      tools.appendChild(b);
    });

    renderActions(n);
    renderButtons(n);
    renderReply(n);
    renderRunlog(extra.runs);

    var meta = $("d-meta");
    clear(meta);
    kvRow(meta, "ID", String(n.id));
    kvRow(meta, "App", n.app);
    if (n.desktop_entry) kvRow(meta, "Desktop entry", n.desktop_entry);
    kvRow(meta, "Urgency", n.urgency);
    kvRow(meta, "Category", n.category || "—");
    kvRow(meta, "State", n.state.replace("_", " "));
    kvRow(meta, "Received", full(n.received_at));
    kvRow(meta, "Read", n.read_at ? full(n.read_at) : "not yet");
    kvRow(meta, "Closed", n.closed_at ? full(n.closed_at) + (n.close_reason ? " (" + n.close_reason + ")" : "") : "still open");
    kvRow(meta, "Timeout", n.timeout_ms === null ? "never" : n.timeout_ms + " ms");
    kvRow(meta, "Tags", (n.tags || []).length ? chips(n.tags) : "—");
    kvRow(meta, "Classes", (n.classes || []).length ? chips(n.classes) : "—");
    kvRow(meta, "Matched rules", (n.matched_rules || []).length ? chips(n.matched_rules) : "—");
    kvRow(meta, "Sound", n.sound || "—");

    var hints = $("d-hints");
    clear(hints);
    var keys = Object.keys(n.hints || {}).sort();
    keys.forEach(function (k) { kvRow(hints, k, hintText(n.hints[k])); });
    if (!keys.length) kvRow(hints, "—", "no hints");

    $("d-mark-read").hidden = !n.unread;
    $("d-dismiss").hidden = !n.open;
  }

  // ---- rule buttons and the run log ------------------------------------------------------

  var extra = { enabled: false, runs: [] };   // from the last "get" reply
  var runTimers = [];

  function renderButtons(n) {
    var box = $("d-buttons");
    clear(box);
    var list = extra.enabled ? (n.buttons || []) : [];
    list.forEach(function (b) {
      var btn = button(b.label, "btn nc-button", function () {
        post({ type: "button", id: n.id, button: b.id });
        flash("Sent: " + b.label);
        if (b.kind === "run") scheduleRunRefresh(n.id);
      });
      btn.setAttribute("data-kind", b.kind || "run");
      btn.setAttribute("data-builtin", b.builtin || "");
      if (b.minutes) {
        btn.setAttribute("data-minutes", String(b.minutes));
        btn.setAttribute("title", "Snooze " + b.minutes + " min");
      }
      box.appendChild(btn);
    });
    $("d-buttons-empty").hidden = list.length > 0;
  }

  // Sender-defined reply (web/reply.js). After a send the answer is recorded a moment later,
  // so the details are fetched again to show "Answered: ..." and the run log row.
  var replyTimers = [];
  function renderReply(n) {
    var box = $("d-reply");
    clear(box);
    var block = window.ncReply.build(n, {
      post: post,
      onSent: function (id) { scheduleReplyRefresh(id); }
    });
    $("d-reply-sec").hidden = !block;
    if (block) box.appendChild(block);
  }

  function scheduleReplyRefresh(id) {
    replyTimers.forEach(clearTimeout);
    replyTimers = [1200, 4000].map(function (ms) {
      return setTimeout(function () {
        ask({ type: "get", id: id }).then(function (res) {
          if (!current || current.id !== id || !res || res.error || !res.notification) return;
          current.reply = res.notification.reply;
          renderReply(current);
          refreshRuns(id);
        });
      }, ms);
    });
  }

  function duration(ms) {
    if (ms === null || ms === undefined) return "—";
    if (ms < 1000) return ms + " ms";
    return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + " s";
  }

  function renderRunlog(runs) {
    var box = $("d-runlog");
    clear(box);
    $("d-runlog-empty").hidden = runs.length > 0;
    if (!runs.length) return;
    var table = el("table", "nc-runlog");
    var head = document.createElement("tr");
    ["Time", "Trigger", "Status", "Duration"].forEach(function (h) { head.appendChild(el("th", null, h)); });
    var thead = document.createElement("thead");
    thead.appendChild(head);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    runs.forEach(function (r) {
      var tr = el("tr", "nc-run");
      tr.setAttribute("data-state", r.state || "");
      tr.setAttribute("data-trigger", r.trigger || "");
      var tail = r.output_tail || "";
      var timeCell = document.createElement("td");
      var when = full(r.started_at);
      var out = null;
      if (tail) {
        var toggle = button(when, "nc-runlog-toggle", function () {
          var open = out.hidden;
          out.hidden = !open;
          toggle.setAttribute("aria-expanded", open ? "true" : "false");
          tr.classList.toggle("open", open);
        });
        toggle.setAttribute("aria-expanded", "false");
        timeCell.appendChild(toggle);
      } else {
        timeCell.textContent = when;
      }
      tr.appendChild(timeCell);
      tr.appendChild(el("td", "nc-run-trigger", r.title || ""));
      tr.appendChild(el("td", "nc-run-status", r.status || ""));
      tr.appendChild(el("td", "nc-run-duration", r.state === "skipped" ? "—" : duration(r.duration_ms)));
      tbody.appendChild(tr);
      if (tail) {
        out = el("tr", "nc-runlog-output");
        out.hidden = true;
        var td = document.createElement("td");
        td.colSpan = 4;
        td.appendChild(el("pre", "nc-run-output", tail));   // textContent only
        out.appendChild(td);
        tbody.appendChild(out);
      }
    });
    table.appendChild(tbody);
    box.appendChild(table);
  }

  function refreshRuns(id) {
    return ask({ type: "runs", id: id }).then(function (res) {
      if (!current || current.id !== id || !res || res.error) return;
      extra.runs = res.runs || [];
      renderRunlog(extra.runs);
    });
  }

  // A run button starts a process; its result lands in the log a moment later.
  function scheduleRunRefresh(id) {
    runTimers.forEach(clearTimeout);
    runTimers = [1200, 4000].map(function (ms) { return setTimeout(function () { refreshRuns(id); }, ms); });
  }

  $("d-runlog-refresh").addEventListener("click", function (ev) {
    ev.preventDefault();
    if (current) refreshRuns(current.id);
  });

  function renderActions(n) {
    var sec = $("d-actions-sec"), box = $("d-actions"), note = $("d-actions-note");
    clear(box);
    note.textContent = "";
    var acts = (n.actions || []);
    sec.hidden = !acts.length;
    acts.forEach(function (a) {
      var label = a.key === "default" ? a.label + " (default)" : a.label;
      var b = button(label, "btn btn-action", function () {
        ask({ type: "action", id: n.id, key: a.key }).then(function (res) {
          if (res && res.ok) {
            flash("Sent: " + a.label);
          } else {
            Array.prototype.forEach.call(box.children, function (x) { x.disabled = true; });
            note.textContent = "sender gone";
          }
        });
      });
      b.setAttribute("data-key", a.key);
      box.appendChild(b);
    });
  }

  function showDetail(id) {
    $("view-list").hidden = true;
    $("view-settings").hidden = true;
    $("view-detail").hidden = false;
    current = null;
    ask({ type: "get", id: id }).then(function (res) {
      if (!res || res.error || !res.notification) {
        $("d-content").hidden = true;
        $("d-notfound").hidden = false;
        $("d-mark-read").hidden = $("d-dismiss").hidden = true;
        $("d-delete").hidden = true;
        $("detail").className = "detail notfound";
        return;
      }
      $("d-delete").hidden = false;
      var n = res.notification;
      extra = { enabled: !!res.buttons_enabled, runs: res.runs || [] };
      var wasUnread = n.unread;
      renderDetail(n);
      $("view-detail").querySelector(".center-scroll").scrollTop = 0;
      if (wasUnread) { markRead([n.id]); renderDetail(n); } // opening a notification reads it
    });
  }

  function showList() {
    $("view-detail").hidden = true;
    $("view-settings").hidden = true;
    $("view-list").hidden = false;
    current = null;
    markSelected(true);
    if (!list.loaded && !list.loading) loadPage(true);
  }

  // ---- router ----------------------------------------------------------------------------

  var SETTINGS_ROUTE = /^#\/settings(?:\/(appearance|behavior|sounds|rules|advanced))?$/;

  function showSettings(tab) {
    $("view-list").hidden = true;
    $("view-detail").hidden = true;
    current = null;
    $("view-settings").hidden = false;
    if (window.ncSettings) window.ncSettings.show(tab);
  }

  function route() {
    var ns = window.ncSettings;
    var sm = SETTINGS_ROUTE.exec(location.hash);
    // Leaving the Settings view with unsaved edits: put the hash back and ask first.
    if (!sm && ns && ns.active() && ns.dirty()) { ns.guard(location.hash); return; }
    if (sm) { showSettings(sm[1] || "appearance"); return; }
    if (ns && ns.active()) ns.leave();
    var m = /^#\/n\/(\d+)$/.exec(location.hash);
    if (m) showDetail(Number(m[1])); else showList();
  }

  function openDetail(id) {
    list.selected = id;
    depth++;
    location.hash = "#/n/" + id;
  }

  function back() {
    if (depth > 0) { depth--; history.back(); } else { location.hash = "#/"; }
  }

  function navigate(hash) {
    if (location.hash === hash) { route(); return; }
    depth++;
    location.hash = hash;
  }

  window.addEventListener("hashchange", route);

  // ---- live updates from Python ----------------------------------------------------------

  function added(n) {
    if (filters.q) { loadPage(true); loadFacets(); return; } // can't evaluate FTS here: re-query
    if (list.loaded && filtersMatch(n)) {
      mergeItems([n]);
      renderList();
    }
    loadFacets();
  }

  function refresh() {
    loadFacets();
    if (current) { showDetail(current.id); return; }
    loadPage(true);
  }

  function setUserCss(text) {
    var style = $("user");
    if (!style) {
      style = document.createElement("style");
      style.id = "user";
      document.head.appendChild(style);
    }
    style.textContent = String(text);
  }

  // What settings.js needs from this file.
  window.ncHost = { ask: ask, post: post, flash: flash, back: back, navigate: navigate };

  window.nc = { reply: reply, added: added, refresh: refresh, navigate: navigate, setUserCss: setUserCss, replyResult: window.ncReply.deliver, imageResult: window.ncImage.deliver };

  // ---- wiring ----------------------------------------------------------------------------

  var searchTimer = 0;
  $("search").addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {
      filters.q = $("search").value;
      loadPage(true);
    }, 220);
  });

  function bindFilter(id, key, isCheck) {
    $(id).addEventListener("change", function () {
      filters[key] = isCheck ? $(id).checked : $(id).value;
      loadPage(true);
    });
  }
  bindFilter("f-app", "app");
  bindFilter("f-state", "state");
  bindFilter("f-tag", "tag");
  bindFilter("f-unread", "unread", true);

  $("btn-read-all").addEventListener("click", function () {
    post({ type: "mark_all_read" });
    list.items.forEach(function (n) { n.unread = false; });
    if (filters.unread) { list.items = []; list.byId = new Map(); }
    renderList();
    flash("All marked read");
  });
  // The "⋯" overflow menu holds the less-used list controls so the toolbar stays on one line.
  function setMenu(open) {
    $("more-menu").hidden = !open;
    $("btn-more-menu").setAttribute("aria-expanded", open ? "true" : "false");
  }
  function updateMoreDot() {
    $("btn-more-menu").classList.toggle("active", !!$("f-tag").value);
  }
  $("btn-more-menu").addEventListener("click", function (ev) {
    ev.stopPropagation();
    setMenu($("more-menu").hidden);
  });
  $("more-menu").addEventListener("click", function (ev) {
    ev.stopPropagation();
    if (ev.target.tagName === "BUTTON") setMenu(false);
  });
  document.addEventListener("click", function () { setMenu(false); });
  $("f-tag").addEventListener("change", updateMoreDot);
  $("btn-settings").addEventListener("click", function () { navigate("#/settings"); });
  $("btn-clear").addEventListener("click", function () { $("confirm-clear").hidden = false; });
  $("confirm-clear-no").addEventListener("click", function () { $("confirm-clear").hidden = true; });
  $("confirm-clear-yes").addEventListener("click", function () {
    $("confirm-clear").hidden = true;
    clearHistory();
    flash("History cleared");
  });
  $("btn-toggle-all").addEventListener("click", toggleAll);
  $("f-group").addEventListener("change", function () {
    view.groupBy = $("f-group").value === "app" ? "app" : "day";
    post({ type: "set_group_by", mode: view.groupBy });
    renderList();
  });
  $("btn-more").addEventListener("click", function () { loadPage(false); });
  $("btn-back").addEventListener("click", back);
  $("d-mark-read").addEventListener("click", function () { if (current) { markRead([current.id]); renderDetail(current); } });
  $("d-dismiss").addEventListener("click", function () {
    if (!current) return;
    var id = current.id;
    ask({ type: "dismiss", id: id }).then(function () {
      return ask({ type: "get", id: id });
    }).then(function (res) {
      if (res && res.notification) { renderDetail(res.notification); }
      var n = list.byId.get(id);
      if (n && res && res.notification) { n.open = res.notification.open; }
    });
  });
  $("d-delete").addEventListener("click", function () {
    if (!current) return;
    removeItems([current.id]);
    back();
  });

  // Infinite scroll: load the next page when the sentinel scrolls near.
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting && list.hasMore && !list.loading) loadPage(false);
    }, { root: $("list-scroll"), rootMargin: "300px" }).observe($("more"));
  }

  // ---- keyboard --------------------------------------------------------------------------

  function typing(t) {
    return t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA") && t.type !== "checkbox";
  }

  document.addEventListener("keydown", function (ev) {
    if (ev.ctrlKey || ev.altKey || ev.metaKey) return;
    var inList = !$("view-list").hidden;
    if (ev.key === "Escape") {
      if (!$("more-menu").hidden) { setMenu(false); return; }
      if (!$("confirm-clear").hidden) { $("confirm-clear").hidden = true; return; }
      if (typing(ev.target)) { ev.target.blur(); return; }
      if (!inList) { back(); ev.preventDefault(); }
      return;
    }
    if (typing(ev.target)) return;
    var onButton = ev.target && ev.target.tagName === "BUTTON";
    if ((ev.key === "Enter" || ev.key === " ") && inList && !onButton && list.selHead !== null) {
      var h = headByKey(list.selHead);
      if (h) { toggleSection(h.parentNode); ev.preventDefault(); }
      return;
    }
    if ((ev.key === "h" || ev.key === "ArrowLeft") && inList) { foldCursor(false); ev.preventDefault(); }
    else if ((ev.key === "l" || ev.key === "ArrowRight") && inList) { foldCursor(true); ev.preventDefault(); }
    else if (ev.key === "/" && inList) { ev.preventDefault(); $("search").focus(); $("search").select(); }
    else if (ev.key === "j" && inList) { moveSelection(1); }
    else if (ev.key === "k" && inList) { moveSelection(-1); }
    else if (ev.key === "Enter" && inList && !onButton && list.selected !== null) { openDetail(list.selected); }
  });

  // ---- start -----------------------------------------------------------------------------

  loadFacets();
  loadView().then(route);
})();
