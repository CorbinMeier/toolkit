#!/usr/bin/env bash
set -euo pipefail
rm -f "$HOME/.local/bin/cosmic-applet-timer" \
  "$HOME/.local/share/applications/com.corbinmeier.CosmicAppletTimer.desktop"
echo "Uninstalled. Remove the applet from the panel in Settings first if it is placed."
