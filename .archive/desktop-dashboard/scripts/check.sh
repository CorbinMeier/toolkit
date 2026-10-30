#!/usr/bin/env bash
# The single verification gateway: lint -> test -> build, stopping at the
# first failing stage. A thin wrapper over the commands documented in
# .claude/rules/verification.md / scripts/README.md -- it does not
# reimplement any of them.
#
# Usage: scripts/check.sh   (run from any cwd; cd's to the repo root itself)
#
# Success prints one "PASS <stage>" line per stage (eslint, ruff, unittest,
# smoke.py), then a final line `CHECK PASS`, and exits 0. Any stage failing
# prints "FAIL <stage>" and the check exits non-zero immediately, running no
# later stage.
#
# The test and build stages force /usr/bin/python3 explicitly rather than
# trusting PATH: on this machine a PlatformIO venv can sit ahead of the
# system interpreter on PATH and silently swallow most of the test suite
# (missing psutil -> import errors -> 12 tests run instead of 136). That is
# also the real runtime interpreter -- systemd's units invoke it the same
# way. `.venv/bin/ruff` stays as-is: that venv exists for ruff only and has
# no psutil, so nothing else should run out of it.
#
# The build stage (smoke.py) must never be graded against the live
# desktop-dashboard-serve unit: that process is long-running and does not
# re-exec on reload, so it keeps answering with whatever code it started
# with -- a merge can be green while the unit is stale and behind, or (the
# more dangerous direction) a broken change can look green if the stale
# unit's old code is what actually answers the assertions (#85). smoke.py
# already honors DASHD_PORT for exactly this ("so a git worktree can
# smoke-test ITSELF") but check.sh used to leave it unset, so it silently
# inherited config.json's configured port -- and whatever was already
# listening there. Pick a fresh, free port for every run instead: bind
# port 0 and let the OS assign one, so two check.sh runs at once (e.g. two
# worktrees) can never collide on the same port.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY=/usr/bin/python3

stage() {
  local name="$1"
  shift
  if "$@"; then
    printf 'PASS %s\n' "$name"
  else
    printf 'FAIL %s\n' "$name"
    exit 1
  fi
}

pick_free_port() {
  "$PY" -c 'import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()'
}

stage eslint    npx eslint web/app.js web/control/control.js eslint.config.js
stage ruff      .venv/bin/ruff check .
stage unittest  "$PY" -m unittest discover -s tests

BUILD_PORT="$(pick_free_port)"
echo "build stage: testing this working tree's own server on 127.0.0.1:${BUILD_PORT} (not the configured/live port -- see #85)"
DASHD_PORT="$BUILD_PORT" stage smoke.py "$PY" scripts/smoke.py

echo "CHECK PASS"
