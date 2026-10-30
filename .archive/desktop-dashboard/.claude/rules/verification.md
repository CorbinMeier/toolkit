---
paths:
  - "scripts/**"
  - "tests/**"
  - "package.json"
  - "ruff.toml"
  - "eslint.config.js"
---

# Running and verifying this project

```
scripts/smoke.py    the "build" stage — end-to-end render assertions
tests/test_dashd.py unittest suite (hermetic, no network)
tests/test_control.py unit tests for dashd-control's auth/whitelist logic
```

## Running and editing

```bash
systemctl --user status desktop-dashboard-serve desktop-dashboard-collect desktop-dashboard-host
systemctl --user reload desktop-dashboard-host   # SIGHUP → reload all views
journalctl --user -u desktop-dashboard-host -n 30
```

`web/` is plain HTML/CSS/JS with Tailwind compiled in the browser — **there is
no build step and nothing to bundle**. Edit, then `reload`.

`scripts/update-desktop.sh` (`pnpm run deploy`) automates that: runs the §6
lint→test→build chain and, only if green, `reload`s `desktop-dashboard-host`
so the change reaches the wallpaper. A red chain exits before touching the
live surfaces; it never starts/enables a unit (Gatekeeper Protocol).

Preview without touching the desktop. `?static=1` is required — headless
Chrome's virtual clock never completes a CSS animation, so without it you
screenshot an all-transparent page and conclude the CSS is broken:

```bash
google-chrome --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --window-size=2560,1080 --virtual-time-budget=9000 --screenshot=out.png \
  'http://127.0.0.1:4320/index.html?static=1&output=DELL%20U2913WM'
```

Real desktop capture (no `grim` on this box):
`cosmic-screenshot --interactive=false --modal=false --notify=false -s <dir>`.
It grabs the whole 4000x1390 multi-output framebuffer (blank dead zones are
the offset outputs, not a bug) and can't show the dashboard under windows,
since the surface is correctly *below* them; ask the user to look instead.

`?forcetime=HH:MM` (#92) pins the clock `sunTint()` reads, for a
deterministic time-lapse instead of whatever wall-clock time a render runs
at. **Gated behind `?static=1`** (`dashd-host` never passes it, so the live
wallpaper is unaffected regardless); invalid or missing falls back to now:

```bash
for h in $(seq -w 0 23); do
  google-chrome --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
    --window-size=2560,1080 --virtual-time-budget=6000 \
    --screenshot="/tmp/tint_$h.png" \
    "http://127.0.0.1:4320/index.html?static=1&forcetime=$h:00"
done
```

`--dump-dom` in place of `--screenshot` is the cheap numeric check — grep the
emitted `--tint-*` properties per hour, confirm the glow position moves
monotonically with no jump. A single frame can't show motion; render the series.

## Verification chain (lint → test → build)

```bash
npx eslint web/app.js web/control/control.js eslint.config.js   # or: pnpm run lint:js
.venv/bin/ruff check .                   # or: pnpm run lint:py
python3 -m unittest discover -s tests    # or: pnpm run test
python3 scripts/smoke.py                 # or: pnpm run build
```

`scripts/smoke.py` is the build stage: nothing compiles here, so the
equivalent is proving the page renders — it starts the server if needed,
asserts `/api/state` is well-formed, then renders in headless Chrome and
asserts the DOM actually populated. It catches what lint and unit tests
cannot: a JS exception before first paint, a broken Tailwind bundle, a
renamed state key silently blanking a panel. `--no-browser` skips the Chrome
pass.

Fresh clone needs the dev tooling restored (both are gitignored):

```bash
python3 -m venv .venv && .venv/bin/pip install ruff
pnpm install
```

**Do not run the app from `.venv`.** The venv exists only for `ruff`; it has
no `psutil`, so `.venv/bin/python bin/dashd-serve` dies with
`ModuleNotFoundError`. Runtime is system `/usr/bin/python3` plus the system
`python3-psutil` — which is what both unit files invoke.

`ruff.toml` carries `extend-include = ["bin/dashd-*"]`. Without it ruff finds
"no Python files", prints *All checks passed*, and exits 0 having checked
nothing — a silently-green lint stage. Keep it.

