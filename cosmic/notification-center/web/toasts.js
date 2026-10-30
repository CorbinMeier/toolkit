// Toast stack. Notification content is untrusted: the DOM is built only with createElement and
// textContent (no HTML-string setters), and every click goes back to Python as a message.
(function () {
  "use strict";

  var stack = document.getElementById("stack");
  var toasts = new Map(); // id -> element
  var ALLOWED = /^(https?:|mailto:)/i;

  function post(msg) {
    try {
      window.webkit.messageHandlers.nc.postMessage(msg);
    } catch (e) { /* not hosted: ignore */ }
  }

  function slug(s) {
    return String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  }

  function addClasses(el, list) {
    list.forEach(function (c) {
      String(c).split(/\s+/).forEach(function (t) { if (t) el.classList.add(t); });
    });
  }

  function div(cls, text) {
    var el = document.createElement("div");
    el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  // Python inline flags such as (?i) are not JS; lift a leading group into RegExp flags.
  function compile(pattern) {
    var flags = "g";
    var m = /^\(\?([ims]+)\)/.exec(pattern);
    if (m) {
      pattern = pattern.slice(m[0].length);
      m[1].split("").forEach(function (f) {
        var js = f === "s" ? "s" : f;
        if (flags.indexOf(js) < 0) flags += js;
      });
    }
    try { return new RegExp(pattern, flags); } catch (e) { return null; }
  }

  function textNodes(root) {
    var out = [];
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) out.push(walker.currentNode);
    return out;
  }

  function highlight(root, re, cls) {
    textNodes(root).forEach(function (node) {
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

  function build(n) {
    var el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("data-app", n.app || "");
    el.setAttribute("data-urgency", n.urgency || "normal");
    el.setAttribute("data-category", n.category || "");
    (n.tags || []).forEach(function (t) { var s = slug(t); if (s) el.classList.add("tag-" + s); });
    addClasses(el, n.classes || []);

    var icon = div("toast-icon");
    if (n.icon) {
      var img = document.createElement("img");
      img.setAttribute("src", n.icon);
      img.setAttribute("alt", "");
      icon.appendChild(img);
    } else {
      icon.appendChild(window.ncAvatar(n.app));
    }
    el.appendChild(icon);

    var content = div("toast-content");
    content.appendChild(div("toast-app", n.app || ""));
    content.appendChild(div("toast-summary", n.summary || ""));
    var body = div("toast-body");
    (n.body || []).forEach(function (run) { body.appendChild(runNode(run)); });
    content.appendChild(body);

    // Image preview (web/image.js): a thumbnail plus Copy image / Copy path. Its buttons stop
    // the click, so they never dismiss the toast or run its default action.
    var pic = window.ncImage.build(n, { post: post, host: "toast" });
    if (pic) content.appendChild(pic);

    var actions = div("toast-actions");
    var hasDefault = false;
    (n.actions || []).forEach(function (a) {
      if (a.key === "default") { hasDefault = true; return; }
      var btn = document.createElement("button");
      btn.className = "toast-action";
      btn.type = "button";
      btn.textContent = a.label;
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        post({ type: "action", id: n.id, key: a.key });
      });
      actions.appendChild(btn);
    });
    if (actions.childNodes.length) content.appendChild(actions);

    // Rule buttons: pressed by id only (the commands never reach the page). Python sets
    // buttons_enabled false when nothing is wired to handle them.
    var rule = div("toast-buttons");
    (n.buttons_enabled ? n.buttons || [] : []).forEach(function (b) {
      var btn = document.createElement("button");
      btn.className = "toast-button";
      btn.type = "button";
      btn.setAttribute("data-kind", b.kind || "run");
      btn.setAttribute("data-builtin", b.builtin || "");
      if (b.minutes) btn.setAttribute("data-minutes", String(b.minutes));
      btn.textContent = b.label;
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        post({ type: "button", id: n.id, button: b.id });
      });
      rule.appendChild(btn);
    });
    if (rule.childNodes.length) content.appendChild(rule);

    // Sender-defined reply actions (web/reply.js). The text box needs keyboard focus: Python
    // switches the surface to ON_DEMAND while one exists, and holds the toast open while it has
    // focus or a confirmation is pending (reply_focus).
    var reply = window.ncReply.build(n, {
      post: post,
      onFocus: function (focused) { post({ type: "reply_focus", id: n.id, focused: focused }); }
    });
    if (reply) content.appendChild(reply);
    el.appendChild(content);

    var close = document.createElement("button");
    close.className = "toast-close";
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", function (ev) {
      ev.stopPropagation();
      post({ type: "closed", id: n.id });
    });
    el.appendChild(close);

    (n.highlights || []).forEach(function (h) {
      var re = compile(String(h.pattern));
      if (!re || !h["class"]) return;
      highlight(el.querySelector(".toast-summary"), re, h["class"]);
      highlight(body, re, h["class"]);
    });

    if (hasDefault) el.classList.add("has-default");
    el.addEventListener("click", function () {
      post(hasDefault
        ? { type: "action", id: n.id, key: "default" }
        : { type: "details", id: n.id });
    });
    ["contextmenu", "auxclick"].forEach(function (t) {
      el.addEventListener(t, function (ev) { ev.preventDefault(); });
    });
    return el;
  }

  function show(n) {
    var el = build(n);
    var old = toasts.get(n.id);
    if (old && old.parentNode === stack) {
      el.classList.add("replaced");
      stack.replaceChild(el, old);
    } else {
      stack.appendChild(el);
    }
    toasts.set(n.id, el);
    report();
  }

  function close(id) {
    var el = toasts.get(id);
    if (!el) return;
    toasts.delete(id);
    window.ncReply.forget(id);
    var done = false;
    function remove() {
      if (done) return;
      done = true;
      if (el.parentNode) el.parentNode.removeChild(el);
      report();
    }
    el.classList.add("leaving");
    el.addEventListener("animationend", remove);
    setTimeout(remove, 600); // fallback when the theme has no exit animation
    if (getComputedStyle(el).animationName === "none") remove();
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

  var lastReport = "";
  function report() {
    var rects = Array.prototype.map.call(stack.children, function (el) {
      var r = el.getBoundingClientRect();
      return [Math.floor(r.left), Math.floor(r.top), Math.ceil(r.width), Math.ceil(r.height)];
    });
    var msg = {
      type: "height",
      width: Math.ceil(stack.offsetWidth),
      height: stack.children.length ? Math.ceil(stack.offsetHeight) : 0,
      rects: rects
    };
    var key = JSON.stringify(msg);
    if (key === lastReport) return;
    lastReport = key;
    post(msg);
  }

  // Hover hold: Python owns the timers; it only needs to know whether the pointer is over the
  // stack. The document-level leave covers the pointer exiting the whole surface.
  var hovering = false;
  function hover(inside) {
    if (inside === hovering) return;
    hovering = inside;
    post({ type: "hover", inside: inside });
  }
  stack.addEventListener("mouseenter", function () { hover(true); });
  stack.addEventListener("mouseleave", function () { hover(false); });
  document.documentElement.addEventListener("mouseleave", function () { hover(false); });

  new ResizeObserver(report).observe(stack);
  window.addEventListener("animationend", function () { report(); }, true);

  window.nc = { show: show, close: close, setUserCss: setUserCss, replyResult: window.ncReply.deliver, imageResult: window.ncImage.deliver };
})();
