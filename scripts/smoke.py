#!/usr/bin/env python3
"""Build/smoke stage: prove the page actually renders with real data.

There is nothing to compile here, so the equivalent of a build is an
end-to-end check: bring the server up, assert /api/state is well-formed, then
render the page in headless Chrome and assert the DOM really populated.

Catches the failure modes that lint and unit tests cannot see -- a JS
exception before first paint, a broken Tailwind vendor bundle, a renamed
state key silently blanking a panel.

Exits non-zero on the first failure. `--no-browser` skips the Chrome pass.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text())
# DASHD_PORT overrides config so a git worktree can smoke-test ITSELF.
# Without it this is a silent false-green: main() only starts a server when
# the configured port is closed, so with the real desktop-dashboard-serve
# unit already listening on 4320, smoke.py happily reuses it -- and that
# process serves the MAIN checkout's web/, not the worktree's. Every render
# assertion then passes against code the branch never touched.
_PORT = int(os.environ.get("DASHD_PORT") or CFG["server"]["port"])
BASE = f"http://{CFG['server']['host']}:{_PORT}"

ok_count = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok_count
    if cond:
        ok_count += 1
        print(f"  \033[32mok\033[0m   {label}")
    else:
        print(f"  \033[31mFAIL\033[0m {label}  {detail}")
        raise SystemExit(1)


def get(path: str, timeout: int = 25) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.read()


def server_up() -> bool:
    try:
        return json.loads(get("/api/health", 3))["ok"]
    except (OSError, ValueError, KeyError):
        return False


def main() -> int:
    started = None
    if not server_up():
        print(f"starting dashd-serve on {BASE}")
        started = subprocess.Popen(
            [sys.executable, str(ROOT / "bin" / "dashd-serve")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            if server_up():
                break
            time.sleep(0.25)

    try:
        print("api:")
        check("server healthy", server_up(), BASE)

        state = json.loads(get("/api/state"))
        check("state has all top-level keys",
              {"ts", "config", "weather", "sys", "projects", "sessions",
               "tasks", "logs", "extra", "locations"}
              <= state.keys(), str(sorted(state.keys())))
        # #62: the panel must always get a rows list to iterate, in every
        # state -- pending, unavailable, disabled, or populated. A missing
        # key here is exactly the "renamed state key silently blanks a
        # panel" failure this stage exists to catch.
        check("projects has a rows list",
              isinstance(state["projects"].get("rows"), list),
              state["projects"])
        # #71: same contract as projects -- a rows list in every state so the
        # panel always has something to iterate. This one additionally must
        # not be empty on a machine someone is logged into: the run itself is
        # happening inside a session, so zero rows means the journal read
        # broke (permissions, a sandbox flag, a renamed message id) rather
        # than "nobody has logged in".
        check("sessions has a rows list",
              isinstance(state["sessions"].get("rows"), list),
              state["sessions"])
        if state["sessions"].get("available"):
            check("sessions saw the session running this check",
                  len(state["sessions"]["rows"]) >= 1,
                  "journal read returned no logins while logged in")
        else:
            print("  \033[33mwarn\033[0m sessions unavailable "
                  f"({state['sessions'].get('error')}) -- skipping row checks")
        check("tasks has an items list",
              isinstance(state["tasks"].get("items"), list), state["tasks"])
        check("locations is a list", isinstance(state["locations"], list),
              state["locations"])
        check("logs is a list", isinstance(state["logs"], list), state["logs"])

        w = state["weather"]
        if w.get("unavailable"):
            print("  \033[33mwarn\033[0m weather unavailable (offline?) "
                  "-- skipping weather assertions")
        else:
            check("weather has a temperature", isinstance(w.get("temp"), int))
            check("weather icon field is a known kind",
                  w.get("icon", "").split("-")[0] in {
                      "clear", "partly", "cloudy", "fog", "drizzle", "rain",
                      "sleet", "snow", "storm"}, w.get("icon"))
            check("7 daily entries", len(w.get("daily", [])) == 7,
                  str(len(w.get("daily", []))))
            check("hourly series present", len(w.get("hourly", [])) >= 12,
                  str(len(w.get("hourly", []))))
            check("sun times present",
                  bool(w["sun"]["sunrise"] and w["sun"]["sunset"]))
            check("moon illumination in range",
                  0 <= w["moon"]["illumination"] <= 1)

        history = json.loads(get("/api/history?hours=1"))
        check("history endpoint returns a samples list",
              isinstance(history.get("samples"), list), history)

        s = state["sys"]
        check("cpu percent sane", 0 <= s["cpu"] <= 100, str(s["cpu"]))
        check("memory reported", s["mem"]["total"] > 0)
        check("at least one disk", len(s["disks"]) >= 1)
        for d in s["disks"]:
            check(f"disk {d['name']} has utilization shape",
                  d["pct"] is not None or d["mount"], d)
        check("config exposes metrics_retain_hours",
              isinstance(state["config"].get("metrics_retain_hours"), (int, float)),
              state["config"])

        print("static:")
        for asset, minimum in (("/index.html", 2000), ("/app.js", 5000),
                               ("/vendor/tailwind.js", 100_000)):
            body = get(asset)
            check(f"{asset} served", len(body) > minimum, f"{len(body)}b")

        if "--no-browser" in sys.argv:
            print(f"\n{ok_count} checks passed (browser pass skipped)")
            return 0

        chrome = next((c for c in ("google-chrome", "chromium",
                                   "chromium-browser") if shutil.which(c)), None)
        if not chrome:
            print("\n\033[33mwarn\033[0m no Chrome found; skipping render pass")
            print(f"{ok_count} checks passed")
            return 0

        print("render:")
        # static=1 -- virtual time never completes CSS animations, so without
        # it the entrance fade leaves every panel at opacity 0.
        dom = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--virtual-time-budget=9000", "--dump-dom",
             f"{BASE}/index.html?static=1&output=smoke"],
            capture_output=True, text=True, timeout=120).stdout

        check("dom returned", len(dom) > 5000, f"{len(dom)}b")
        check("boot overlay was dismissed", 'id="boot"' not in dom,
              "page never reached first render")

        def text_of(el_id: str) -> str:
            m = re.search(rf'id="{el_id}"[^>]*>([^<]*)', dom, re.S)
            return (m.group(1) if m else "").strip()

        # No date/clock/location readout -- the user's own system already
        # shows all three (see #5, #9). Current temperature now renders in
        # the Forecast panel's #wtemp cell of the Weather kv-grid (no more
        # NOW sys-table row or large standalone hero readout, see #9, #41)
        # -- a unit-suffixed value there (000F/000C, no degree symbol --
        # #51) is the remaining proof the panel rendered.
        if not w.get("unavailable"):
            check("temperature rendered", re.search(r"\d[FC]$", text_of("wtemp")),
                  "missing unit-suffixed value in #wtemp")
        check("system grid rendered", dom.count(">MEM<") >= 1, dom.count(">MEM<"))
        # #57: System dropped icons for a .sys-grid row shape (label | value
        # | sub) -- sysicon no longer appears here, so row count is proven
        # via sys-key (one per row: CPU/MEM/BAT/NET + >=1 disk = >=5).
        check("system rows rendered", dom.count("sys-key") >= 5,
              "expected cpu+mem+bat+net+>=1 disk row")
        check("storage section rendered", ">Storage<" in dom, "missing Storage header")
        check("network section rendered", ">Network<" in dom and ">NET<" in dom,
              "missing Network header/row")
        # Tasks and Schedule panels were removed (deleted "for now"), and
        # Devices was removed entirely (#56 -- "basically just clutter").
        # Log moved out of the old combined "Monitor" section into its own
        # panel up front (top-right in the wide 2-col layout).
        # "Log" (not ">Log<") -- its <h2> wraps across two lines in the
        # source, same as System's own header, so the text never sits flush
        # against the closing '>' the way single-line h3s do elsewhere.
        check("log section rendered", "Log</h2>" in dom, "missing Log header")
        # #62: same two-line <h2> wrap as System/Log, hence "Projects</h2>".
        check("projects section rendered", "Projects</h2>" in dom,
              "missing Projects header")
        # The panel is fed by a background fetch on a multi-hour TTL, so a
        # cold run legitimately renders a status line ("fetching…") instead
        # of rows -- assert against what /api/state actually reported rather
        # than assuming the cache is warm, same shape as the weather
        # unavailable branch above. When rows ARE present they must have
        # rendered, which is the regression that matters.
        if state["projects"].get("rows"):
            check("project rows rendered", dom.count("proj-num") >= 3,
                  "state had project rows but the grid is empty")
        else:
            reason = ("pending" if state["projects"].get("pending")
                      else state["projects"].get("error") or "no open issues")
            print("  \033[33mwarn\033[0m projects cache cold "
                  f"({reason}) -- skipping row assertions")
        # #36/#64/#65: the interactive controls. These are the first
        # clickable elements in the project, and they are only reachable
        # because dashd-host stopped blanking its input region -- so assert
        # the markup actually reaches the DOM rather than trusting that a
        # render happened. A missing .ctl-btn means the controls silently
        # vanished and the panel went back to being read-only.
        check("music transport controls rendered",
              'id="musicControls"' in dom and dom.count("ctl-btn") >= 3,
              "missing Music play/pause/next/prev buttons")
        # The pager hides itself when everything fits on one page, so the
        # element must exist unconditionally but is only *shown* with more
        # rows than a page holds -- assert existence, not visibility.
        check("projects pager present", 'id="projpager"' in dom,
              "missing Projects pagination controls")
        # #71: header plus real rows. Unlike Projects there is no cold-cache
        # branch to tolerate -- the read is synchronous and the machine
        # running this has a live session -- so an empty grid here is always
        # a regression.
        check("sessions section rendered", "Sessions</h2>" in dom,
              "missing Sessions header")
        if state["sessions"].get("rows"):
            check("session rows rendered", "sess-num" in dom,
                  "state had sessions but the grid is empty")
        # #34: trend graphs are deliberately not shown on the
        # wallpaper anymore (collection continues server-side) -- assert
        # they stay gone rather than silently reappearing on a regression.
        check("cpu/mem trend graphs stay hidden", ">30M<" not in dom,
              "trend graph markup should not render")
        check("tailwind compiled utilities",
              "--color-ink" in dom and ".text-faint" in dom,
              "vendor bundle may not have run")
        theme = state["config"]["display"].get("theme", "night_ops")
        check(f'theme "{theme}" applied to <html>', f'data-theme="{theme}"' in dom,
              "app.js should set documentElement.dataset.theme from display.theme")
        check("crt fx layer present", 'id="crtfx"' in dom, "missing #crtfx overlay")

        print(f"\n\033[32m{ok_count} checks passed\033[0m")
        return 0
    finally:
        if started:
            started.terminate()
            started.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
