/* Letter avatar for notifications without an icon, shared by the toast, shade and center pages.
   window.ncAvatar(app) -> <span class="nc-avatar" data-app="..."> holding the app's first letter
   (uppercase; the first grapheme when Intl.Segmenter exists). The hue (0-359) is a stable hash of
   the case-folded app name, set with style.setProperty("--nc-avatar-hue", n): never a style
   string. Saturation, lightness and text color come from --nc-avatar-* in default.css. */
(function () {
  "use strict";

  function firstChar(s) {
    s = (s || "").trim();
    if (!s) return "?";
    var ch;
    if (typeof Intl !== "undefined" && Intl.Segmenter) {
      var it = new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(s)[Symbol.iterator]();
      ch = it.next().value.segment;
    } else {
      ch = Array.from(s)[0];
    }
    return ch.toLocaleUpperCase();
  }

  // FNV-1a over UTF-16 code units: same hue in every page and every run.
  function hue(app) {
    var h = 0x811c9dc5;
    var s = (app || "").toLowerCase();
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h % 360;
  }

  window.ncAvatar = function (app) {
    var a = document.createElement("span");
    a.className = "nc-avatar";
    a.setAttribute("data-app", app || "");
    a.setAttribute("aria-hidden", "true");
    a.style.setProperty("--nc-avatar-hue", String(hue(app)));
    a.textContent = firstChar(app);
    return a;
  };
  window.ncAvatar.hue = hue;
  window.ncAvatar.firstChar = firstChar;
})();
