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
BASE = f"http://{CFG['server']['host']}:{CFG['server']['port']}"

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
              {"ts", "config", "weather", "sys", "extra"} <= state.keys(),
              str(sorted(state.keys())))

        w = state["weather"]
        if w.get("unavailable"):
            print("  \033[33mwarn\033[0m weather unavailable (offline?) "
                  "-- skipping weather assertions")
        else:
            check("weather has a temperature", isinstance(w.get("temp"), int))
            check("weather icon is renderable",
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
        # shows all three (see ISSUES.md #5, #9). Temperature is the
        # remaining proof the forecast panel rendered.
        if not w.get("unavailable"):
            check("temperature rendered", text_of("temp").lstrip("-").isdigit(),
                  repr(text_of("temp")))
        check("system table rendered", dom.count(">MEM<") >= 1, dom.count(">MEM<"))
        check("disk rows rendered", dom.count("sysicon") >= 3,
              "expected cpu+mem+>=1 disk row")
        check("storage section rendered", ">Storage<" in dom, "missing Storage header")
        check("network section rendered", ">Network<" in dom and ">NET<" in dom,
              "missing Network header/row")
        tiers_ok = (dom.count(">30S<") >= 2 and dom.count(">5M<") >= 2
                    and dom.count(">30M<") >= 2)
        check("cpu/mem step-chart tiers rendered", tiers_ok,
              "expected 30S/5M/30M tier rows for both cpu and mem")
        check("tailwind compiled utilities",
              "--color-ink" in dom and ".text-faint" in dom,
              "vendor bundle may not have run")

        print(f"\n\033[32m{ok_count} checks passed\033[0m")
        return 0
    finally:
        if started:
            started.terminate()
            started.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
