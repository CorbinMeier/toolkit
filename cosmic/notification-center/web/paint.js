// Open timing (center and shade): after the page becomes visible, wait two animation frames and
// tell the daemon (`perf` / `painted`) so it can log how long the open took. Python also calls
// window.ncPaint.arm() on every show. Carries no data.
(function () {
  "use strict";

  var token = 0;
  var armed = false;

  function post(msg) {
    try {
      window.webkit.messageHandlers.nc.postMessage(msg);
    } catch (e) { /* not hosted: ignore */ }
  }

  function arm() {
    if (armed || document.visibilityState !== "visible") return;
    armed = true;
    var mine = ++token;
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        if (mine !== token) return; // hidden again meanwhile
        armed = false;
        post({ type: "perf", phase: "painted" });
      });
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      arm();
    } else {
      token++;
      armed = false;
    }
  });

  window.ncPaint = { arm: arm };
  arm();
})();
