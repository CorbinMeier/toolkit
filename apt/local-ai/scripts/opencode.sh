#!/usr/bin/env bash
# Run the bundled OpenCode with the portable config in local-ai/config/opencode.
# All arguments pass through: `opencode.sh`, `opencode.sh run "..."`, `opencode.sh serve`, ...
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export OPENCODE_CONFIG="$ROOT/config/opencode/opencode.json"
export OPENCODE_CONFIG_DIR="$ROOT/config/opencode"
# The binary in bin/ is pinned; update it on purpose (see docs/opencode.md), not behind your back.
export OPENCODE_DISABLE_AUTOUPDATE=1
exec "$ROOT/bin/opencode/opencode" "$@"
