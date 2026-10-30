#!/usr/bin/env bash
# Builds the panel applet and installs it for the current user. Does not touch the daemon, the
# shim or the panel config: adding the applet to the panel is manual.
# Usage: scripts/install-applet.sh [--icons-only]
#   Installs ~/.local/bin/cosmic-applet-notification-center and renders the desktop entry into
#   ~/.local/share/applications/ (the panel lists applets from there). Also installs the icons the
#   applet and its desktop entry name: the plain, -new and -dnd symbolic bells plus the full-color
#   one, into ~/.local/share/icons/hicolor/{symbolic,scalable}/apps/ (see scripts/lib/icons.sh).
#   Icons refuse to overwrite files without our marker, skip unchanged ones (no log row), and
#   refresh the icon cache. The launcher shares two of them; this script leaves its desktop alone.
#   --icons-only: skip the cargo build, binary and desktop entry (used by the tests).
# Test-only env override (never set for a real install):
#   NOTIFYD_CHANGES_LOG=<file>   write the change rows there instead of docs/system-changes.md
#   (HOME is honoured.)
set -euo pipefail
ROOT="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
cd "$ROOT"

ICONS_ONLY=0
case "${1:-}" in
	--icons-only) ICONS_ONLY=1 ;;
	"") ;;
	*) echo "usage: scripts/install-applet.sh [--icons-only]" >&2; exit 2 ;;
esac

APP_ID=net.corbinmeier.CosmicAppletNotifications
BIN_NAME=cosmic-applet-notification-center
BIN="$HOME/.local/bin/$BIN_NAME"
DESKTOP="$HOME/.local/share/applications/$APP_ID.desktop"
LOG="${NOTIFYD_CHANGES_LOG:-docs/system-changes.md}"
SHARE="$HOME/.local/share"
MARKER="notifyd-launcher: cosmic-notification-center"
today="$(date +%F)"
# shellcheck source=lib/icons.sh
. scripts/lib/icons.sh

# Icons first: a refusal happens before anything is built or written.
ICON_PATHS=("$ICON_FULL" "$ICON_PLAIN" "$ICON_NEW" "$ICON_DND")
icons_refuse install-applet.sh "${ICON_PATHS[@]}"

install_icons() {
	local f changed=0
	for f in "${ICON_PATHS[@]}"; do
		if [ -e "$f" ] && [ "$(<"$f")" = "$(<"$(icon_source "$f")")" ]; then
			continue
		fi
		mkdir -p "$(dirname "$f")"
		cp "$(icon_source "$f")" "$f"
		changed=1
		echo "| $today | install applet icon (scripts/install-applet.sh) | \`$f\` | \`scripts/uninstall-applet.sh\` (or \`rm $f\`) |" >>"$LOG"
	done
	[ "$changed" = 1 ] && icon_refresh_cache
	return 0
}

if [ "$ICONS_ONLY" = 1 ]; then
	install_icons
	echo "Installed the applet icons under $ICON_DIR."
	exit 0
fi

(cd applet && cargo build --release)

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
# Install under a temp name and rename: a running applet keeps its old inode instead of failing
# with "text file busy".
install -m 755 "applet/target/release/$BIN_NAME" "$BIN.new"
mv -f "$BIN.new" "$BIN"
sed "s|@BIN@|$BIN|" "applet/resources/$APP_ID.desktop" >"$DESKTOP"

{
	echo "| $today | install panel applet binary (scripts/install-applet.sh) | \`$BIN\` | \`scripts/uninstall-applet.sh\` (or \`rm $BIN\`) |"
	echo "| $today | install panel applet desktop entry | \`$DESKTOP\` | \`scripts/uninstall-applet.sh\` (or \`rm $DESKTOP\`) |"
} >>"$LOG"

install_icons

echo "Installed $BIN, $DESKTOP and the icons."
echo "Add 'Notification Center' in Settings > Desktop > Panel > Applets"
