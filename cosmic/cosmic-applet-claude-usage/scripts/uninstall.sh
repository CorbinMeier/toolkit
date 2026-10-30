#!/usr/bin/env bash
set -euo pipefail
rm -f "$HOME/.local/bin/cosmic-applet-claude-usage" \
  "$HOME/.local/share/applications/net.corbinmeier.CosmicAppletClaudeUsage.desktop"
echo "Uninstalled. Remove the applet from the panel in Settings first if it is placed."
