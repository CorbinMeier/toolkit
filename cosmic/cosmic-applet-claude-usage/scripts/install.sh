#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cargo build --release
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
install -m 755 target/release/cosmic-applet-claude-usage "$HOME/.local/bin/cosmic-applet-claude-usage"
sed "s|@BIN@|$HOME/.local/bin/cosmic-applet-claude-usage|" resources/net.corbinmeier.CosmicAppletClaudeUsage.desktop \
  > "$HOME/.local/share/applications/net.corbinmeier.CosmicAppletClaudeUsage.desktop"
echo "Installed. Add 'Claude Usage' in Settings > Desktop > Panel > Applets."
echo "Live data: add to ~/.claude/settings.json:"
echo "  \"statusLine\": {\"type\": \"command\", \"command\": \"$HOME/.local/bin/cosmic-applet-claude-usage --statusline\"}"
