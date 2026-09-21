# scripts/

One row per script. Full running and verification notes, including the
headless-preview trick and why the app must never run from `.venv`, load with
these files from `.claude/rules/verification.md`.

| script | what it does | how to run | success prints |
|---|---|---|---|
| `install.sh` | Symlinks the four systemd user units into `~/.config/systemd/user` and runs `daemon-reload`. Never enables or starts anything - that is the Gatekeeper Protocol's call - and prints the commands for the user to run instead. | `scripts/install.sh` | one `linked <unit>` line per unit, then the enable commands |
| `smoke.py` | The build stage. Nothing compiles here, so the equivalent is proving the page renders: starts the server if needed, asserts `/api/state` is well-formed, then renders in headless Chrome and asserts the DOM populated. Catches a JS exception before first paint, a broken Tailwind bundle, or a renamed state key silently blanking a panel. | `python3 scripts/smoke.py` (`pnpm run build`); `--no-browser` skips the Chrome pass | `SMOKE PASS` |
| `update-desktop.sh` | Deploy. Runs the whole lint -> test -> build chain and, only if every stage is green, `reload`s `desktop-dashboard-host` so the change reaches the wallpaper. A red chain exits before touching the live surfaces. Never starts, enables or stops a unit. | `scripts/update-desktop.sh` (`pnpm run deploy`) | each stage green, then the host reload line |
| `check.sh` | **Not written yet - issue #76.** The single verification gateway every agent runs: lint -> test -> build, stopping at the first failure. | `scripts/check.sh` | one PASS/FAIL line per stage, last line `CHECK PASS` |

Until `check.sh` exists, the chain is four commands, in this order, stopping at
the first failure:

```bash
npx eslint web/app.js web/control/control.js eslint.config.js   # pnpm run lint:js
.venv/bin/ruff check .                                          # pnpm run lint:py
python3 -m unittest discover -s tests                           # pnpm run test
python3 scripts/smoke.py                                        # pnpm run build
```

`scripts/one-off/YYYY-MM-DD-<slug>.sh` holds single-use work, kept as the
record. It does not exist yet either (#76).
