#!/usr/bin/env bash
# Removes the launcher desktop entry and icons that scripts/install-launcher.sh installed. Only
# files carrying our marker are removed; anything else at those paths is left alone with a warning.
# The app + plain symbolic icons are shared with the panel applet and stay while its desktop entry
# is installed.
# Usage: scripts/uninstall-launcher.sh
# Test-only env override (never set for a real uninstall):
#   NOTIFYD_CHANGES_LOG=<file>   write the change rows there instead of docs/system-changes.md
#   (HOME is honoured.)
set -euo pipefail
ROOT="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
cd "$ROOT"

APP_ID=net.corbinmeier.NotificationCenter
MARKER="notifyd-launcher: cosmic-notification-center"
LOG="${NOTIFYD_CHANGES_LOG:-docs/system-changes.md}"
SHARE="$HOME/.local/share"
today="$(date +%F)"

removed=0
remove() {
	local label="$1" f="$2"
	[ -e "$f" ] || [ -L "$f" ] || return 0
	if [ -L "$f" ] || ! grep -q "$MARKER" "$f"; then
		echo "warning: leaving $f: not created by scripts/install-launcher.sh" >&2
		return 0
	fi
	rm -f "$f"
	removed=1
	echo "| $today | remove $label (scripts/uninstall-launcher.sh) | \`$f\` | \`scripts/install-launcher.sh\` |" >>"$LOG"
}

remove "launcher desktop entry" "$SHARE/applications/$APP_ID.desktop"
# The two icons are shared with the panel applet: keep them while its desktop entry is installed.
if [ -e "$SHARE/applications/net.corbinmeier.CosmicAppletNotifications.desktop" ]; then
	echo "keeping the shared icons: the panel applet is installed (scripts/uninstall-applet.sh removes them)"
else
	remove "launcher icon" "$SHARE/icons/hicolor/scalable/apps/$APP_ID.svg"
	remove "launcher symbolic icon" "$SHARE/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
fi

if [ "$removed" = 1 ]; then
	if command -v update-desktop-database >/dev/null 2>&1; then
		update-desktop-database "$SHARE/applications" ||
			echo "warning: update-desktop-database failed" >&2
	fi
	if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -d "$SHARE/icons/hicolor" ]; then
		gtk-update-icon-cache -f -t "$SHARE/icons/hicolor" ||
			echo "warning: gtk-update-icon-cache failed" >&2
	fi
fi

echo "Removed the launcher files."
