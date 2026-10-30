# scripts/

One row per script. Full running and verification notes, including the
headless-preview trick and why the app must never run from `.venv`, load with
these files from `.claude/rules/verification.md`.

| script | what it does | how to run | success prints |
|---|---|---|---|
| `install.sh` | Symlinks the four systemd user units into `~/.config/systemd/user` and runs `daemon-reload`. Never enables or starts anything - that is the Gatekeeper Protocol's call - and prints the commands for the user to run instead. | `scripts/install.sh` | one `linked <unit>` line per unit, then the enable commands |
| `uninstall.sh` | The reverse of `install.sh`: removes the unit symlinks in `~/.config/systemd/user` that point into this checkout's `systemd/`, then `daemon-reload`s. Refuses (exit 1, nothing unlinked) while any unit is enabled or active and prints the `systemctl --user disable --now ...` command for the user to run first. Skips regular files and links pointing at another checkout; never touches `data/`. | `scripts/uninstall.sh` | one `unlinked <unit>` line per unit |
| `smoke.py` | The build stage. Nothing compiles here, so the equivalent is proving the page renders: starts the server if needed, asserts `/api/state` is well-formed, then renders in headless Chrome and asserts the DOM populated. Catches a JS exception before first paint, a broken Tailwind bundle, or a renamed state key silently blanking a panel. | `python3 scripts/smoke.py` (`pnpm run build`); `--no-browser` skips the Chrome pass | `SMOKE PASS` |
| `update-desktop.sh` | Deploy. Runs the whole lint -> test -> build chain and, only if every stage is green, `reload`s `desktop-dashboard-host` so the change reaches the wallpaper. A red chain exits before touching the live surfaces. Never starts, enables or stops a unit. | `scripts/update-desktop.sh` (`pnpm run deploy`) | each stage green, then the host reload line |
| `check.sh` | The single verification gateway every agent runs: eslint -> ruff -> unittest -> smoke.py, stopping at the first failure. Test and build stages force `/usr/bin/python3` explicitly rather than trusting `PATH` (a PlatformIO venv can shadow it and silently truncate the test suite). The build stage picks its own free port (`DASHD_PORT`, bind-port-0-and-read-back) instead of inheriting the configured one, so it always grades the working tree's own server, never a stale long-running `desktop-dashboard-serve` unit (#85) -- prints which port it used. | `scripts/check.sh` (`pnpm run check`) | one PASS/FAIL line per stage, last line `CHECK PASS` |

`scripts/one-off/YYYY-MM-DD-<slug>.sh` holds single-use work, kept as the
record.
