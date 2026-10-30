/* Image previews, shared by the toast, shade and center pages.
   window.ncImage.build(n, opts) -> <div class="nc-image"> | null
     n.image     {uri, mime, name} from Python, or null (no preview: nothing is built). The page
                 never sees a file path; `uri` is an nc-image: URL that Python serves.
     opts.post   fn(msg): sends {type: "image_copy" | "image_copy_path" | "image_open", id}
     opts.host   "toast" | "shade" | "center": sets [data-host] (sizes come from CSS variables)
     opts.open   true (center detail): clicking the preview opens the file in the default viewer
   window.ncImage.thumb(n) -> <span class="nc-image-thumb"> | null   (center list rows)
   window.ncImage.deliver(res)   Python's outcome {id, kind, ok}; flips the button to
                                 "Copied" / "Failed" for a moment
   DOM is built with createElement/textContent only. An image that fails to load (deleted since)
   removes its block silently. Clicks on the buttons never reach the notification's own click
   handler (toast: no dismiss, no default action). */
(function () {
  "use strict";

  var SCHEME = /^nc-image:\/\/img\/\d+\/[0-9a-f-]+$/;
  var RESET_MS = 1500;
  var KIND = { image_copy: "copy", image_copy_path: "path" };
  var BUTTONS = [
    ["copy", "Copy image", "image_copy"],
    ["path", "Copy path", "image_copy_path"]
  ];
  var live = []; // {id, kind, btn, label, timer}

  // Pages re-render a lot (the shade on every refresh): forget buttons that left the DOM.
  function prune() {
    live = live.filter(function (b) { return b.btn.isConnected || b.timer; });
  }

  function usable(n) {
    return !!(n && n.image && typeof n.image.uri === "string" && SCHEME.test(n.image.uri));
  }

  function img(n, cls, onGone) {
    var el = document.createElement("img");
    el.className = cls;
    el.setAttribute("alt", "");
    el.setAttribute("decoding", "async");
    el.setAttribute("draggable", "false");
    el.setAttribute("src", n.image.uri);
    el.addEventListener("error", onGone);
    return el;
  }

  function button(n, spec, post) {
    var btn = document.createElement("button");
    btn.className = "nc-image-button";
    btn.type = "button";
    btn.setAttribute("data-kind", spec[0]);
    btn.textContent = spec[1];
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      post({ type: spec[2], id: n.id });
    });
    ["contextmenu", "auxclick"].forEach(function (t) {
      btn.addEventListener(t, function (ev) { ev.preventDefault(); ev.stopPropagation(); });
    });
    live.push({ id: n.id, kind: spec[0], btn: btn, label: spec[1], timer: 0 });
    return btn;
  }

  function build(n, opts) {
    if (!usable(n)) return null;
    if (live.length > 100) prune();
    var box = document.createElement("div");
    box.className = "nc-image";
    box.setAttribute("data-host", opts.host || "toast");
    var pic = img(n, "nc-image-preview", function () {
      if (box.parentNode) box.parentNode.removeChild(box);
    });
    if (n.image.name) pic.setAttribute("title", n.image.name);
    if (opts.open) {
      pic.setAttribute("data-open", "true");
      pic.addEventListener("click", function (ev) {
        ev.stopPropagation();
        opts.post({ type: "image_open", id: n.id });
      });
    }
    box.appendChild(pic);
    var actions = document.createElement("div");
    actions.className = "nc-image-actions";
    BUTTONS.forEach(function (spec) { actions.appendChild(button(n, spec, opts.post)); });
    box.appendChild(actions);
    return box;
  }

  function thumb(n) {
    if (!usable(n)) return null;
    var box = document.createElement("span");
    box.className = "nc-image-thumb";
    box.appendChild(img(n, "nc-image-thumb-img", function () {
      if (box.parentNode) box.parentNode.removeChild(box);
    }));
    return box;
  }

  function deliver(res) {
    if (!res || typeof res !== "object" || !KIND[res.kind]) return;
    prune();
    live.forEach(function (b) {
      if (b.id !== res.id || b.kind !== KIND[res.kind]) return;
      clearTimeout(b.timer);
      b.btn.textContent = res.ok ? "Copied" : "Failed";
      b.btn.setAttribute("data-state", res.ok ? "done" : "failed");
      b.timer = setTimeout(function () {
        b.timer = 0;
        b.btn.textContent = b.label;
        b.btn.removeAttribute("data-state");
      }, RESET_MS);
    });
  }

  window.ncImage = { build: build, thumb: thumb, deliver: deliver };
})();
