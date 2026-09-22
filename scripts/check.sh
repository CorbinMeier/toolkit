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

stage eslint    npx eslint web/app.js web/control/control.js eslint.config.js
stage ruff      .venv/bin/ruff check .
stage unittest  "$PY" -m unittest discover -s tests
stage smoke.py  "$PY" scripts/smoke.py

echo "CHECK PASS"
