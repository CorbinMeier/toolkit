#!/usr/bin/env bash
# Put short commands on PATH (~/.local/bin): qwen-server, oc, voicestudio, openseo.
# Run once per machine after copying local-ai over. Safe to rerun.
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
mkdir -p ~/.local/bin
chmod +x "$ROOT"/scripts/*.sh "$ROOT"/bin/opencode/opencode "$ROOT"/bin/voicestudio/*.AppImage
ln -sfn "$ROOT/scripts/qwen-server.sh" ~/.local/bin/qwen-server
ln -sfn "$ROOT/scripts/opencode.sh"    ~/.local/bin/oc
ln -sfn "$ROOT/scripts/voicestudio.sh" ~/.local/bin/voicestudio
ln -sfn "$ROOT/scripts/openseo.sh"     ~/.local/bin/openseo
echo "Linked: qwen-server, oc, voicestudio, openseo -> ~/.local/bin"
