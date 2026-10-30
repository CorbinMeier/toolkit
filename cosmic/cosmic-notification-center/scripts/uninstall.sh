#!/usr/bin/env bash
# Removes the notifyd shim (only if it is ours). Config and history are kept unless --purge.
# Usage: scripts/uninstall.sh [--activate] [--purge]
#   --activate  killall the shim and daemon so cosmic-session respawns the stock daemon (and
#               the panel), then verify the stock daemon owns org.freedesktop.Notifications
#   --purge     also delete ~/.config/net.corbinmeier.notifications and the history database
# Test-only env overrides (tests/test_install.py; never set for a real uninstall):
#   NOTIFYD_CHANGES_LOG=<file>   write the change rows there instead of docs/system-changes.md
#   (HOME is honoured, so tests point it at a temp dir.) --activate is never allowed under it.
set -euo pipefail
ROOT="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
cd "$ROOT"

MARKER='# notifyd-shim: cosmic-notification-center'
SHIM="$HOME/.local/bin/cosmic-notifications"
CONF_DIR="$HOME/.config/net.corbinmeier.notifications"
DATA_DIR="$HOME/.local/share/net.corbinmeier.notifications"
LOG="${NOTIFYD_CHANGES_LOG:-docs/system-changes.md}"

activate=0
purge=0
for a in "$@"; do
	case "$a" in
	--activate) activate=1 ;;
	--purge) purge=1 ;;
	*)
		echo "usage: $0 [--activate] [--purge]" >&2
		exit 64
		;;
	esac
done

if [ "$activate" -eq 1 ] && [ -n "${NOTIFYD_CHANGES_LOG:-}" ]; then
	echo "FAIL: --activate cannot be combined with NOTIFYD_CHANGES_LOG (test override)" >&2
	exit 1
fi

# True if file $1 has a line that starts with $MARKER (the rendered marker line carries trailing text).
is_our_shim() {
	local line
	while IFS= read -r line || [ -n "$line" ]; do
		[[ "$line" == "$MARKER"* ]] && return 0
	done <"$1" 2>/dev/null
	return 1
}

today="$(date +%F)"
if [ -e "$SHIM" ] || [ -L "$SHIM" ]; then
	if is_our_shim "$SHIM"; then
		rm -f "$SHIM"
		echo "removed $SHIM"
		echo "| $today | remove shim (scripts/uninstall.sh) | \`$SHIM\` | \`scripts/install.sh\` |" >>"$LOG"
	else
		echo "FAIL: $SHIM is not our shim; leaving it alone" >&2
		exit 1
	fi
else
	echo "no shim at $SHIM"
fi

if [ "$purge" -eq 1 ]; then
	rm -rf "$CONF_DIR" "$DATA_DIR"
	echo "purged $CONF_DIR and $DATA_DIR"
	echo "| $today | purge config and history (scripts/uninstall.sh --purge) | \`$CONF_DIR/\`, \`$DATA_DIR/\` | none (data deleted; \`scripts/install.sh\` recreates the defaults) |" >>"$LOG"
else
	echo "kept config ($CONF_DIR) and history ($DATA_DIR); use --purge to delete"
fi

if [ "$activate" -eq 0 ]; then
	echo "PASS (shim removed, not activated)"
	echo "Swap back to the stock daemon now (restarts the panel too; cosmic-session backs off exponentially per restart):  killall cosmic-notifications"
	exit 0
fi

echo "activating: killall cosmic-notifications"
killall cosmic-notifications || echo "WARN: no cosmic-notifications process was running" >&2
pkill -TERM -f 'python3 -m notifyd' || true # belt and braces if the shim died first

owner_cmdline() {
	local p
	p="$(busctl --user call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus \
		GetConnectionUnixProcessID s org.freedesktop.Notifications 2>/dev/null | awk '{print $2}')" || return 1
	[ -n "$p" ] && tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null
}

for _ in $(seq 1 30); do
	sleep 0.5
	if cmd="$(owner_cmdline)" && [[ "$cmd" == *cosmic-notifications* && "$cmd" != *notifyd* ]]; then
		echo "PASS: org.freedesktop.Notifications is owned by the stock daemon: $cmd"
		exit 0
	fi
done
echo "FAIL: stock daemon does not own org.freedesktop.Notifications after 15 s (owner: ${cmd:-none})" >&2
exit 1
