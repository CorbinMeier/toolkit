/* Sender-defined reply actions, shared by the toast, shade and center pages.
   window.ncReply.build(n, opts) -> <div class="nc-reply"> | null
     n.reply     the cleaned reply from Python (choices, text, host, method, allowed, reason,
                 confirm, answered, error); it never holds the URL, body or a token
     opts.post   fn(msg): sends {type: "reply_choice"|"reply_text"|"reply_confirm"|"reply_cancel"}
     opts.onFocus  fn(bool) (toasts): the text box gained or lost focus
     opts.onSent   fn(id): a send went out (center: refetch the details a moment later)
   window.ncReply.deliver(res)   Python's outcome {id, kind, status, token?, reason?}
   window.ncReply.forget(id)     drop the local state of a toast that closed
   window.ncReply.describe(reply, local)  pure: what the block shows (see tests/test_reply_js.py)
   DOM is built with createElement/textContent only. Local state per notification id (`drafts`)
   survives the page rebuilding the block (the shade and center re-render on every refresh), so a
   half-typed answer, a pending confirmation and "Sent" are not lost; once Python reports
   `answered`, that wins. */
(function () {
  "use strict";

  var HARD_MAX = 4000;
  var DRAFT_CAP = 200;
  var SEND_TIMEOUT_MS = 15000;
  var drafts = new Map(); // id -> { state, text, token, reason, focused }
  var live = new Map();   // id -> the newest controller built for it

  function cpLen(s) { return Array.from(s).length; }   // code points, as Python's len()

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function clock(at) {
    var d = new Date(at * 1000);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  function draftFor(id) {
    var d = drafts.get(id);
    if (!d) {
      if (drafts.size >= DRAFT_CAP) drafts.delete(drafts.keys().next().value);
      d = { state: "", text: "", token: "", reason: "", focused: false };
      drafts.set(id, d);
    }
    return d;
  }

  function maxOf(reply) {
    var m = reply && reply.text && reply.text.max;
    return typeof m === "number" && m > 0 ? Math.min(Math.floor(m), HARD_MAX) : HARD_MAX;
  }

  function answeredText(reply) {
    var a = reply.answered;
    var what = "reply";
    if (typeof a.choice === "string") {
      what = a.choice;
      (reply.choices || []).forEach(function (c) { if (c.id === a.choice && c.label) what = c.label; });
    } else if (typeof a.text === "string") {
      what = a.text.replace(/\s+/g, " ").trim();
      if (what.length > 40) what = what.slice(0, 39) + "…";
    }
    var out = "Answered: " + what;
    if (typeof a.at === "number") out += " · " + clock(a.at);
    if (a.status !== undefined && a.status !== null && a.status !== "") out += " (" + a.status + ")";
    return out;
  }

  function answerFailed(a) {
    var s = a.status;
    if (typeof s === "number") return s < 200 || s >= 300;
    return typeof s === "string" && /^(fail|error|refus)/i.test(s);
  }

  // What the block shows. Priority: answered > pending confirmation > just sent > not allowed >
  // sending > refused > error.
  function describe(reply, local) {
    local = local || {};
    var v = { inputs: true, disabled: false, busy: false, confirm: false, confirmText: "", status: "", kind: "" };
    if (reply.answered) {
      v.inputs = false;
      v.status = answeredText(reply);
      v.kind = answerFailed(reply.answered) ? "error" : "answered";
      return v;
    }
    if (local.state === "confirm") {
      v.inputs = false;
      v.confirm = true;
      v.confirmText = "Confirm sending to " + (reply.host || "the sender") + "?";
      return v;
    }
    if (local.state === "sent") {
      v.inputs = false;
      v.status = "Sent ✓";
      v.kind = "sent";
      return v;
    }
    if (!reply.allowed) {
      v.disabled = true;
      v.status = reply.reason || "Not allowed";
      v.kind = "disabled";
      return v;
    }
    if (local.state === "sending") {
      v.busy = true;
      v.status = "Sending…";
      v.kind = "busy";
      return v;
    }
    if (local.state === "refused") {
      v.status = "Refused: " + (local.reason || "not sent");
      v.kind = "refused";
      return v;
    }
    if (reply.error) {
      v.status = "Error: " + reply.error;
      v.kind = "error";
    }
    return v;
  }

  // Apply Python's outcome to a draft. Returns true when a send went out ("Sent").
  function applyResult(draft, res) {
    if (!res || res.kind === "cancel") return false;
    if (res.status === "sent") {
      draft.state = "sent";
      draft.text = "";
      draft.token = "";
      return true;
    }
    if (res.status === "pending_confirm" && typeof res.token === "string" && res.token) {
      draft.state = "confirm";
      draft.token = res.token;
    } else {
      draft.state = "refused";
      draft.reason = typeof res.reason === "string" ? res.reason : "";
      draft.token = "";
    }
    return false;
  }

  function node(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function button(cls, text, onClick) {
    var b = node("button", cls, text);
    b.type = "button";
    b.addEventListener("click", function (ev) { ev.stopPropagation(); onClick(); });
    return b;
  }

  function build(n, opts) {
    opts = opts || {};
    var reply = n && n.reply;
    if (!reply || typeof reply !== "object" || typeof opts.post !== "function") return null;
    var id = n.id;
    var choices = Array.isArray(reply.choices) ? reply.choices : [];
    var hasText = !!(reply.text && typeof reply.text === "object");
    if (!choices.length && !hasText && !reply.answered && !reply.error) return null;
    var max = maxOf(reply);
    var draft = draftFor(id);
    if (reply.answered) { draft.state = ""; draft.token = ""; }
    var prev = live.get(id);
    var hadFocus = !!(prev && prev.ta && document.activeElement === prev.ta);
    var tip = "Sends " + (reply.method || "POST") + " to " + (reply.host || "the sender");

    var root = node("div", "nc-reply");
    root.setAttribute("data-host", reply.host || "");
    root.addEventListener("click", function (ev) { ev.stopPropagation(); });
    ["contextmenu", "auxclick"].forEach(function (t) {
      root.addEventListener(t, function (ev) { ev.preventDefault(); ev.stopPropagation(); });
    });

    var choicesEl = node("div", "nc-reply-choices");
    var choiceBtns = choices.map(function (c) {
      var b = button("nc-reply-choice", c.label || c.id, function () {
        beginSend({ type: "reply_choice", id: id, choice: c.id });
      });
      b.setAttribute("data-choice", c.id);
      b.title = tip;
      choicesEl.appendChild(b);
      return b;
    });
    root.appendChild(choicesEl);

    var ta = null, sendBtn = null, count = null;
    var compose = node("div", "nc-reply-compose");
    if (hasText) {
      ta = node("textarea", "nc-reply-text");
      ta.rows = 2;
      ta.setAttribute("placeholder", reply.text.placeholder || "Reply…");
      ta.setAttribute("aria-label", reply.text.placeholder || "Reply");
      ta.value = draft.text;
      ta.addEventListener("input", function () { draft.text = ta.value; update(); });
      ta.addEventListener("keydown", function (ev) {
        ev.stopPropagation();   // typing never reaches the page's shortcuts
        if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
          ev.preventDefault();
          sendText();
        } else if (ev.key === "Escape") {
          ta.blur();
        }
      });
      ["keyup", "keypress"].forEach(function (t) {
        ta.addEventListener(t, function (ev) { ev.stopPropagation(); });
      });
      ta.addEventListener("focus", function () {
        draft.focused = true;
        if (opts.onFocus) opts.onFocus(true);
      });
      ta.addEventListener("blur", function () {
        if (!root.isConnected) return;   // the page is replacing this block, not the user leaving
        draft.focused = false;
        if (opts.onFocus) opts.onFocus(false);
      });
      compose.appendChild(ta);
      var row = node("div", "nc-reply-compose-row");
      count = node("span", "nc-reply-count");
      row.appendChild(count);
      sendBtn = button("nc-reply-send", "Send", sendText);
      sendBtn.title = tip;
      row.appendChild(sendBtn);
      compose.appendChild(row);
    }
    root.appendChild(compose);

    var confirmEl = node("div", "nc-reply-confirm");
    var confirmText = node("span", "nc-reply-confirm-text");
    confirmEl.appendChild(confirmText);
    confirmEl.appendChild(button("nc-reply-confirm-yes", "Send", function () {
      if (!draft.token) return;
      beginSend({ type: "reply_confirm", id: id, token: draft.token });
    }));
    confirmEl.appendChild(button("nc-reply-confirm-no", "Cancel", function () {
      var token = draft.token;
      draft.state = "";
      draft.token = "";
      clearTimeout(ctl.timer);
      if (token) opts.post({ type: "reply_cancel", id: id, token: token });
      update();
    }));
    root.appendChild(confirmEl);

    var statusEl = node("div", "nc-reply-status");
    statusEl.setAttribute("role", "status");
    root.appendChild(statusEl);
    var meta = node("div", "nc-reply-host", "→ " + (reply.host || ""));
    meta.title = tip;
    root.appendChild(meta);

    var ctl = { id: id, ta: ta, timer: 0, root: root };

    function sendText() {
      if (!ta) return;
      var v = describe(reply, draft);
      var len = cpLen(ta.value);
      if (!v.inputs || v.disabled || v.busy || !ta.value.trim() || len > max) return;
      beginSend({ type: "reply_text", id: id, text: ta.value });
    }

    function beginSend(msg) {
      var v = describe(reply, draft);
      if (v.disabled || v.busy) return;
      draft.state = "sending";
      clearTimeout(ctl.timer);
      ctl.timer = setTimeout(function () {   // Python always answers; this is the safety net
        if (draft.state === "sending") { draft.state = "refused"; draft.reason = "no answer"; update(); }
      }, SEND_TIMEOUT_MS);
      opts.post(msg);
      update();
    }

    function update() {
      var v = describe(reply, draft);
      root.setAttribute("data-state", v.confirm ? "confirm" : (v.kind || "idle"));
      choicesEl.hidden = !(v.inputs && choices.length);
      compose.hidden = !(v.inputs && hasText);
      confirmEl.hidden = !v.confirm;
      confirmText.textContent = v.confirmText;
      statusEl.hidden = !v.status;
      statusEl.textContent = v.status;
      statusEl.setAttribute("data-kind", v.kind);
      meta.hidden = !(v.inputs || v.confirm);
      choiceBtns.forEach(function (b) { b.disabled = v.disabled || v.busy; });
      if (ta) {
        ta.disabled = v.disabled;
        ta.readOnly = v.busy;
        var len = cpLen(ta.value);
        count.textContent = len + "/" + max;
        count.classList.toggle("over", len > max);
        sendBtn.disabled = v.disabled || v.busy || !ta.value.trim() || len > max;
      }
    }

    ctl.deliver = function (res) {
      clearTimeout(ctl.timer);
      var sent = applyResult(draft, res);
      if (sent && ta) ta.value = "";
      update();
      if (sent && opts.onSent) opts.onSent(id);
    };

    live.set(id, ctl);
    update();
    if (hadFocus && ta) {   // the page rebuilt the block under the cursor: put the caret back
      setTimeout(function () {
        if (root.isConnected && !ta.disabled) {
          ta.focus();
          try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (e) { /* ignore */ }
        }
      }, 0);
    }
    return root;
  }

  function deliver(res) {
    if (!res || typeof res.id !== "number") return;
    var ctl = live.get(res.id);
    if (ctl && ctl.root.isConnected) { ctl.deliver(res); return; }
    applyResult(draftFor(res.id), res);   // block not on screen: the next build shows it
  }

  function forget(id) {
    var ctl = live.get(id);
    if (ctl) clearTimeout(ctl.timer);
    live.delete(id);
    drafts.delete(id);
  }

  window.ncReply = { build: build, deliver: deliver, forget: forget, describe: describe, applyResult: applyResult };
})();
