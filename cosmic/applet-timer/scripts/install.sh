#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cargo build --release
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
install -m 755 target/release/cosmic-applet-timer "$HOME/.local/bin/cosmic-applet-timer"
sed "s|@BIN@|$HOME/.local/bin/cosmic-applet-timer|" resources/com.corbinmeier.CosmicAppletTimer.desktop \
  > "$HOME/.local/share/applications/com.corbinmeier.CosmicAppletTimer.desktop"
echo "Installed. Add 'Timer' in Settings > Desktop > Panel > Applets."
