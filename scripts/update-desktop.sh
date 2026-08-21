#!/usr/bin/env bash
# Build + deploy: run the lint -> test -> build chain, and only if every
# stage is green, reload the live desktop-dashboard-host unit so the
# change actually shows up on the wallpaper (SIGHUP re-render, not a
# restart -- see CLAUDE.md's "Running and editing"). A red chain exits
# before touching the live surfaces, same as the project's git workflow:
# green is the only thing allowed to ship.
#
# Never starts/enables/stops a unit -- that's gated behind the Gatekeeper
# Protocol (explicit user yes/no). `reload` is the one always-safe verb
# here: it only asks an already-running host to re-render.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

bold "lint"
npx eslint web/app.js eslint.config.js || fail "eslint"
ok "eslint"
.venv/bin/ruff check . || fail "ruff"
ok "ruff"

bold "test"
# Bare `python3` on this machine resolves to a platformio venv with no
# psutil, not the system interpreter dashd-serve/dashd-collect actually
# run under -- CLAUDE.md's "Do not run the app from .venv" gotcha, plus
# an unrelated PATH shadow on top of it. Force the system interpreter
# explicitly rather than trusting PATH.
PY=/usr/bin/python3
"$PY" -m unittest discover -s tests || fail "unittest"

bold "build"
"$PY" scripts/smoke.py || fail "smoke.py"

bold "deploy"
if ! systemctl --user is-active --quiet desktop-dashboard-host; then
  echo "  desktop-dashboard-host is not active -- not starting it" \
       "(Gatekeeper Protocol: enable/start needs explicit yes/no)."
  echo "  Chain is green; nothing was deployed."
  exit 1
fi
systemctl --user reload desktop-dashboard-host
ok "desktop-dashboard-host reloaded"

bold "units"
systemctl --user is-active desktop-dashboard-serve desktop-dashboard-collect \
  desktop-dashboard-host | paste -d' ' \
  <(printf 'serve\ncollect\nhost\n') -

bold "done -- chain green, live host reloaded"
