#!/usr/bin/env bash
# Installs the "Notification Center" app launcher for the current user: the desktop entry (with
# the absolute repo path rendered in) and the app + symbolic icons. Touches nothing else.
# Usage: scripts/install-launcher.sh
#   ~/.local/share/applications/net.corbinmeier.NotificationCenter.desktop
#   ~/.local/share/icons/hicolor/scalable/apps/net.corbinmeier.NotificationCenter.svg
#   ~/.local/share/icons/hicolor/symbolic/apps/net.corbinmeier.NotificationCenter-symbolic.svg
# Refuses (exit 1, writes nothing) when one of those paths holds a file without our marker.
# Re-running is idempotent: unchanged files are left alone and log no row.
# Test-only env override (never set for a real install):
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
# shellcheck source=lib/icons.sh
. scripts/lib/icons.sh

DESKTOP="$SHARE/applications/$APP_ID.desktop"
ICON="$ICON_FULL"
SYMBOLIC="$ICON_PLAIN"

# Render everything first, so a refusal happens before any write.
rendered_desktop="$(sed "s|@REPO@|$ROOT|g" "resources/$APP_ID.desktop")"
declare -A WANT=(
	["$DESKTOP"]="$rendered_desktop"
	["$ICON"]="$(<"resources/icons/$APP_ID.svg")"
	["$SYMBOLIC"]="$(<"resources/icons/$APP_ID-symbolic.svg")"
)
ORDER=("$DESKTOP" "$ICON" "$SYMBOLIC")
declare -A LABEL=(["$DESKTOP"]="launcher desktop entry" ["$ICON"]="launcher icon" ["$SYMBOLIC"]="launcher symbolic icon")

icons_refuse install-launcher.sh "${ORDER[@]}"

changed=0
for f in "${ORDER[@]}"; do
	if [ -e "$f" ] && [ "$(<"$f")" = "${WANT[$f]}" ]; then
		continue
	fi
	mkdir -p "$(dirname "$f")"
	printf '%s\n' "${WANT[$f]}" >"$f"
	changed=1
	echo "| $today | install ${LABEL[$f]} (scripts/install-launcher.sh) | \`$f\` | \`scripts/uninstall-launcher.sh\` (or \`rm $f\`) |" >>"$LOG"
done

if [ "$changed" = 1 ]; then
	if command -v update-desktop-database >/dev/null 2>&1; then
		update-desktop-database "$SHARE/applications" ||
			echo "warning: update-desktop-database failed" >&2
	fi
	icon_refresh_cache
fi

echo "Launcher installed ($DESKTOP). Search 'Notification Center' in the COSMIC launcher."
