# Shared icon install/uninstall helpers, sourced by install-launcher.sh, install-applet.sh and
# their uninstall counterparts (never run directly). Callers set ROOT, SHARE, LOG, today and
# MARKER (the cwd is ROOT) before calling these.
#
# Icons (all under resources/icons/, all carrying the marker comment):
#   scalable/apps/net.corbinmeier.NotificationCenter.svg          full color    launcher + applet
#   symbolic/apps/net.corbinmeier.NotificationCenter-symbolic.svg  plain bell   launcher + applet
#   symbolic/apps/net.corbinmeier.NotificationCenter-new-symbolic.svg  unseen    applet only
#   symbolic/apps/net.corbinmeier.NotificationCenter-dnd-symbolic.svg  DND       applet only
# The first two are SHARED: the launcher uses them too, so the applet uninstall keeps them while
# the launcher desktop entry is installed. The launcher uninstall removes only its own two.

ICON_ID=net.corbinmeier.NotificationCenter
ICON_DIR="$SHARE/icons/hicolor"
ICON_FULL="$ICON_DIR/scalable/apps/$ICON_ID.svg"
ICON_PLAIN="$ICON_DIR/symbolic/apps/$ICON_ID-symbolic.svg"
ICON_NEW="$ICON_DIR/symbolic/apps/$ICON_ID-new-symbolic.svg"
ICON_DND="$ICON_DIR/symbolic/apps/$ICON_ID-dnd-symbolic.svg"

# icon_source <installed path> -> repo source file
icon_source() { echo "resources/icons/$(basename "$1")"; }

# icons_refuse <script name> <path...>: exit 1 when a path holds a file or link without our marker.
icons_refuse() {
	local who="$1" f
	shift
	for f in "$@"; do
		if [ -e "$f" ] || [ -L "$f" ]; then
			if [ -L "$f" ] || ! grep -q "$MARKER" "$f"; then
				echo "refusing to overwrite $f: not created by scripts/$who" >&2
				exit 1
			fi
		fi
	done
}

# icon_refresh_cache: refresh the hicolor cache (warning only).
icon_refresh_cache() {
	if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -d "$ICON_DIR" ]; then
		gtk-update-icon-cache -f -t "$ICON_DIR" ||
			echo "warning: gtk-update-icon-cache failed" >&2
	fi
}
