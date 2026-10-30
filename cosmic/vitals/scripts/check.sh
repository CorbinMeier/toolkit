#!/usr/bin/env bash
# fmt, clippy, tests, release build and the binary size limit of spec §8. Last line: CHECK PASS.
set -euo pipefail
cd "$(dirname "$0")/.."
# Tests are headless: nothing may reach the live desktop.
unset WAYLAND_DISPLAY DISPLAY
# The snapshot tests print local times (stale words, clocks): pin the zone so they match on every machine.
export TZ=UTC
MAX_BYTES=$((3250 * 1000)) # 3.25 MB (raised 2026-10-29 for --serve, spec §8)

cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo build --release
# The web page's smoke test (web/test/smoke.mjs): needs node only, no npm.
if command -v node >/dev/null 2>&1; then
    node web/test/smoke.mjs
else
    echo "node not found: skipping the web page smoke test"
fi
size=$(stat -c %s target/release/vitals)
if [ "$size" -gt "$MAX_BYTES" ]; then
    echo "binary is $size bytes, over the $MAX_BYTES limit (spec §8)" >&2
    exit 1
fi
echo "binary size: $size bytes (limit $MAX_BYTES)"
echo "CHECK PASS"
