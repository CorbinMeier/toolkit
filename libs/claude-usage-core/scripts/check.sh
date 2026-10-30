#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo clippy --all-targets --features usage-text -- -D warnings
cargo test
cargo test --features usage-text
cargo build --release
echo "CHECK PASS"
