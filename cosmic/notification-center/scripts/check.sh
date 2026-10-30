#!/usr/bin/env bash
# The single verification gateway: lint -> format -> test, stopping at the first failing stage.
# Usage: scripts/check.sh   (any cwd; it cd's to the repo root)
# Prints "PASS <stage>" per stage; a failing stage prints "FAIL <stage>" and exits non-zero.
# The last line on success is `CHECK PASS`.
# Python is always /usr/bin/python3: a PlatformIO venv can sit ahead of it on PATH. The project
# .venv exists for ruff only.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY=/usr/bin/python3

if [ ! -x .venv/bin/ruff ]; then
  "$PY" -m venv .venv
  .venv/bin/pip install --quiet ruff
fi

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

stage ruff-check  .venv/bin/ruff check .
stage ruff-format .venv/bin/ruff format --check .
# Tests must never show windows on the user's desktop: with no display variables GTK/WebKit
# constructors fail instead of opening a surface on the live session.
stage unittest    env -u WAYLAND_DISPLAY -u DISPLAY "$PY" -m unittest discover -s tests -t .

# Phase 4: the panel applet (Rust).
if [ -f applet/Cargo.toml ]; then
  stage cargo-fmt     cargo fmt --manifest-path applet/Cargo.toml --check
  stage cargo-clippy  cargo clippy --manifest-path applet/Cargo.toml --all-targets -- -D warnings
  stage cargo-test    cargo test --manifest-path applet/Cargo.toml
  stage cargo-build   cargo build --release --manifest-path applet/Cargo.toml
fi

echo "CHECK PASS"
