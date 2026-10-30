#!/usr/bin/env bash
# Installs the notifyd shim as ~/.local/bin/cosmic-notifications and sets up config/data dirs.
# Usage: scripts/install.sh [--activate]
#   (no flag)   install only; prints the command that swaps the live daemon
#   --activate  also `killall cosmic-notifications`: cosmic-session restarts the daemon AND the
#               panel, and the new daemon runs through the shim. Run with the user present.
#               (Only for swapping daemons: cosmic-session backs off exponentially per process,
#               so repeated killalls leave you without notifications for up to a minute.
#               To restart just the daemon later, use `bin/notifyd --restart`.)
# Every change outside the repo is appended to docs/system-changes.md with its revert command.
# Test-only env overrides (tests/test_install.py; never set them for a real install):
#   NOTIFYD_CHANGES_LOG=<file>       write the change rows there instead of docs/system-changes.md
#   NOTIFYD_INSTALL_SKIP_CHECK=1     skip the typelib import and the scripts/check.sh preflight
#   NOTIFYD_INSTALL_SKIP_SESSION=1   skip the stock-daemon and cosmic-session PATH checks
#   (HOME is honoured, so tests point it at a temp dir.) --activate is refused if either SKIP is set.
set -euo pipefail
ROOT="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
cd "$ROOT"

MARKER='# notifyd-shim: cosmic-notification-center'
STOCK=/usr/bin/cosmic-notifications
SHIM="$HOME/.local/bin/cosmic-notifications"
CONF_DIR="$HOME/.config/net.corbinmeier.notifications"
DATA_DIR="$HOME/.local/share/net.corbinmeier.notifications"
LOG="${NOTIFYD_CHANGES_LOG:-docs/system-changes.md}"
SKIP_CHECK="${NOTIFYD_INSTALL_SKIP_CHECK:-}"
SKIP_SESSION="${NOTIFYD_INSTALL_SKIP_SESSION:-}"

activate=0
case "${1:-}" in
'') ;;
--activate) activate=1 ;;
*)
	echo "usage: $0 [--activate]" >&2
	exit 64
	;;
esac

fail() {
	echo "FAIL: $*" >&2
	exit 1
}
warn() { echo "WARN: $*" >&2; }

# True if file $1 has a line that starts with $MARKER (the rendered marker line carries trailing text).
is_our_shim() {
	local line
	while IFS= read -r line || [ -n "$line" ]; do
		[[ "$line" == "$MARKER"* ]] && return 0
	done <"$1" 2>/dev/null
	return 1
}

if [ "$activate" -eq 1 ] && { [ -n "$SKIP_CHECK" ] || [ -n "$SKIP_SESSION" ]; }; then
	fail "--activate cannot be combined with the NOTIFYD_INSTALL_SKIP_* test overrides"
fi

# ---- preflight ---------------------------------------------------------------------------
if [ -z "$SKIP_SESSION" ]; then
	[ -x "$STOCK" ] || fail "$STOCK not found (is this a COSMIC session?)"
	case "$ROOT" in *"'"*) fail "repo path contains a single quote: $ROOT" ;; esac

	pid="$(pgrep -x cosmic-session | head -n1 || true)"
	if [ -n "$pid" ] && env_path="$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | grep '^PATH=' | head -n1)" && [ -n "$env_path" ]; then
		case "${env_path#PATH=}" in
		"$HOME/.local/bin":*) echo "ok: ~/.local/bin is first on cosmic-session's PATH" ;;
		*) fail "~/.local/bin is not first on cosmic-session's PATH ($env_path); the shim would not shadow the stock daemon" ;;
		esac
	else
		warn "cannot read cosmic-session's PATH (no session or unreadable environ); continuing"
	fi
fi

if [ -z "$SKIP_CHECK" ]; then
/usr/bin/python3 - <<'PY' || fail "typelibs missing (need Gtk 3.0, GtkLayerShell 0.1, WebKit2 4.1)"
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, GtkLayerShell, WebKit2  # noqa: F401
PY
	echo "ok: typelibs import"

	scripts/check.sh >/tmp/notifyd-install-check.log 2>&1 || {
		tail -n 20 /tmp/notifyd-install-check.log >&2
		fail "scripts/check.sh did not pass"
	}
	echo "ok: scripts/check.sh"
fi

if [ -e "$SHIM" ] || [ -L "$SHIM" ]; then
	is_our_shim "$SHIM" || fail "$SHIM exists and is not our shim; refusing to overwrite"
fi

# ---- install -----------------------------------------------------------------------------
mkdir -p "$HOME/.local/bin" "$CONF_DIR" "$DATA_DIR"

tmp="$(mktemp "$HOME/.local/bin/.cosmic-notifications.XXXXXX")"
while IFS= read -r line || [ -n "$line" ]; do
	printf '%s\n' "${line//@REPO@/"$ROOT"}"
done <scripts/shim/cosmic-notifications >"$tmp"
chmod 755 "$tmp"
mv -f "$tmp" "$SHIM"
echo "installed $SHIM"

if [ ! -e "$CONF_DIR/config.toml" ]; then
	sed "s|@REPO@|$ROOT|g" >"$CONF_DIR/config.toml" <<'TOML'
# notifyd config. [sound], [shade] and the [toasts] timeouts apply on save. The [toasts] anchor,
# margin, [history] and [windows] need a restart:
#   @REPO@/bin/notifyd --restart

[toasts]
# one of: top-left top-right bottom-left bottom-right top-center bottom-center
anchor = "top-right"
# pixels between the toast stack and the screen edge
margin = 12
# Default toast lifetime per urgency: milliseconds (> 0) or "never". Applies to notifications
# that arrive after you save. A rule's `timeout` overrides these.
# timeout_low = 5000
# timeout_normal = 10000
# timeout_critical = "never"
# true: an app's own timeout (> 0) wins over the defaults above. 0 always means never.
# respect_app_timeout = true
# The toast stack holds while the pointer is over it; after the pointer leaves, toasts that
# ran out wait this many ms before closing.
# hover_leave_grace_ms = 1000
# Safety net: if the pointer has been "over" the toasts this long with no movement and no leave
# was reported, release the hold. 0 turns it off.
# hover_max_hold_ms = 120000

[shade]
# The dashboard shade the panel bell opens. All four apply on save.
# one of: top-left top-right bottom-left bottom-right top-center bottom-center
# anchor = "top-right"
# pixels between the shade and the screen edge
# margin = 8
# shade width in pixels (240-1000)
# width = 420
# opening the shade closes every toast on screen (sticky ones too); they stay in the shade
# clear_toasts_on_open = true

[history]
# Delete history older than this many days (checked at startup, then daily).
# Leave commented out to keep history forever.
# retention_days = 90

[sound]
# Default sound for notifications that have none: a file path (mp3/wav/ogg/flac) or a sound
# theme name. Leave commented out for the system default (message-new-instant).
# default = "~/Music/sounds/default.mp3"
# Master volume, 0-100, applied on top of any per-rule volume.
master_volume = 100

[windows]
# Prewarm: a few seconds after startup the center and the shade are built hidden (two idle web
# processes) so the first open is quick. Nothing is shown. Needs a restart.
# prewarm = true

[images]
# Image previews: a thumbnail and "Copy image" / "Copy path" buttons on toasts, the shade and the
# center when a notification carries an image: an image-path hint, or a body that is just the
# path of an image file (screenshot tools). Applies on save.
# preview = true
TOML
	echo "wrote default $CONF_DIR/config.toml"
fi

wrote_rules=0
if [ ! -e "$CONF_DIR/rules.toml" ]; then
	# examples/rules.toml with the example rule's sound line commented out (matches, plays the default).
	awk '
		/^sound = "~\/Music\/sounds\/example.mp3"/ && !done {
			print "# Set a path to give the Example App its own sound (mp3/wav/ogg/flac); until then"
			print "# the rule matches but plays the system default."
			print "# sound = \"~/Music/sounds/example.mp3\""
			done = 1; next
		}
		{ print }
	' examples/rules.toml >"$CONF_DIR/rules.toml.tmp"
	grep -q '^# sound = "~/Music/sounds/example.mp3"' "$CONF_DIR/rules.toml.tmp" || {
		rm -f "$CONF_DIR/rules.toml.tmp"
		fail "could not comment out the example rule's sound line in examples/rules.toml"
	}
	mv "$CONF_DIR/rules.toml.tmp" "$CONF_DIR/rules.toml"
	wrote_rules=1
	echo "wrote default $CONF_DIR/rules.toml"
fi

wrote_style=0
if [ ! -e "$CONF_DIR/style.css" ]; then
	cat >"$CONF_DIR/style.css" <<'CSS'
/* Your own toast CSS. Saving this file applies it at once.
   The look is driven by --nc-* variables; see web/default.css in the repo for the full list,
   then override them here, e.g.:
   :root { --nc-bg: #1b1b1f; } */

/* Pairs with the URGENT rule in rules.toml. */
mark.urgent { color: #ff3b3b; font-weight: 700; background: none; }
CSS
	wrote_style=1
	echo "wrote default $CONF_DIR/style.css"
fi

today="$(date +%F)"
{
	echo "| $today | create shim (installed by scripts/install.sh) | \`$SHIM\` | \`scripts/uninstall.sh --activate\` (or \`rm $SHIM && killall cosmic-notifications\`) |"
	echo "| $today | create config dir and default config.toml | \`$CONF_DIR/\` | \`scripts/uninstall.sh --purge\` (or \`rm -r $CONF_DIR\`) |"
	[ "$wrote_rules" -eq 1 ] && echo "| $today | create default rules.toml (never overwritten) | \`$CONF_DIR/rules.toml\` | \`rm $CONF_DIR/rules.toml\` |"
	[ "$wrote_style" -eq 1 ] && echo "| $today | create default style.css (never overwritten) | \`$CONF_DIR/style.css\` | \`rm $CONF_DIR/style.css\` |"
	echo "| $today | create data dir (history.db appears on first run) | \`$DATA_DIR/\` | \`scripts/uninstall.sh --purge\` (or \`rm -r $DATA_DIR\`) |"
} >>"$LOG"

# ---- activate ----------------------------------------------------------------------------
owner_cmdline() {
	local p
	p="$(busctl --user call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus \
		GetConnectionUnixProcessID s org.freedesktop.Notifications 2>/dev/null | awk '{print $2}')" || return 1
	[ -n "$p" ] && tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null
}

if [ "$activate" -eq 0 ]; then
	echo "PASS (installed, not activated)"
	echo "Activate (restarts the daemon AND the panel through the shim; cosmic-session backs off exponentially per restart, so do this once):  killall cosmic-notifications"
echo "Later daemon restarts:  $ROOT/bin/notifyd --restart"
	echo "Undo:  scripts/uninstall.sh --activate"
	exit 0
fi

echo "activating: killall cosmic-notifications"
killall cosmic-notifications || warn "no cosmic-notifications process was running"
for _ in $(seq 1 30); do
	sleep 0.5
	if cmd="$(owner_cmdline)" && [[ "$cmd" == *notifyd* ]]; then
		echo "PASS: org.freedesktop.Notifications is owned by: $cmd"
		echo "Undo:  scripts/uninstall.sh --activate   (or: touch $CONF_DIR/disable && killall cosmic-notifications)"
		exit 0
	fi
done
echo "FAIL: org.freedesktop.Notifications is not owned by notifyd after 15 s (owner: ${cmd:-none})" >&2
echo "Undo:  scripts/uninstall.sh --activate   (or: touch $CONF_DIR/disable && killall cosmic-notifications; recovery only: cosmic-session restart backoff grows with each one)" >&2
exit 1
