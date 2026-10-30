#!/usr/bin/env bash
# Removes the panel applet binary and desktop entry. The panel config is never touched: remove
# 'Notification Center' from Settings > Desktop > Panel > Applets yourself.
# Also removes the applet's icons (scripts/lib/icons.sh), marker-carrying files only. Icon rule:
# the -new and -dnd symbolic icons are the applet's alone and always go; the full-color icon and
# the plain -symbolic one are SHARED with the launcher, so they stay while the launcher desktop
# entry is installed (remove them with scripts/uninstall-launcher.sh, or after it).
# Usage: scripts/uninstall-applet.sh
# Test-only env override (never set for a real uninstall):
#   NOTIFYD_CHANGES_LOG=<file>   write the change rows there instead of docs/system-changes.md
#   (HOME is honoured.)
set -euo pipefail
ROOT="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
cd "$ROOT"

APP_ID=net.corbinmeier.CosmicAppletNotifications
BIN="$HOME/.local/bin/cosmic-applet-notification-center"
DESKTOP="$HOME/.local/share/applications/$APP_ID.desktop"
LOG="${NOTIFYD_CHANGES_LOG:-docs/system-changes.md}"
SHARE="$HOME/.local/share"
MARKER="notifyd-launcher: cosmic-notification-center"
today="$(date +%F)"
# shellcheck source=lib/icons.sh
. scripts/lib/icons.sh

if [ -e "$BIN" ]; then
	rm -f "$BIN"
	echo "| $today | remove panel applet binary (scripts/uninstall-applet.sh) | \`$BIN\` | \`scripts/install-applet.sh\` |" >>"$LOG"
fi
if [ -e "$DESKTOP" ]; then
	rm -f "$DESKTOP"
	echo "| $today | remove panel applet desktop entry (scripts/uninstall-applet.sh) | \`$DESKTOP\` | \`scripts/install-applet.sh\` |" >>"$LOG"
fi


removed=0
remove_icon() {
	local f="$1"
	[ -e "$f" ] || [ -L "$f" ] || return 0
	if [ -L "$f" ] || ! grep -q "$MARKER" "$f"; then
		echo "warning: leaving $f: not created by scripts/install-applet.sh" >&2
		return 0
	fi
	rm -f "$f"
	removed=1
	echo "| $today | remove applet icon (scripts/uninstall-applet.sh) | \`$f\` | \`scripts/install-applet.sh\` |" >>"$LOG"
}
remove_icon "$ICON_NEW"
remove_icon "$ICON_DND"
if [ -e "$SHARE/applications/$ICON_ID.desktop" ]; then
	echo "keeping the shared icons: the launcher is installed (scripts/uninstall-launcher.sh removes them)"
else
	remove_icon "$ICON_PLAIN"
	remove_icon "$ICON_FULL"
fi
[ "$removed" = 1 ] && icon_refresh_cache

echo "Removed the applet files. Remove 'Notification Center' from Settings > Desktop > Panel > Applets."
