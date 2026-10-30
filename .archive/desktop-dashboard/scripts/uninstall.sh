#!/usr/bin/env bash
# Unlink the systemd user units that install.sh linked, and reload the daemon.
#
# Deliberately does NOT disable or stop anything -- per the Gatekeeper
# Protocol, changing unit state is the user's call (install.sh only prints the
# enable/start commands; update-desktop.sh never starts, enables or stops).
# Removing a unit file while its wants-links exist leaves systemd with dangling
# references, so this refuses, before unlinking anything, while any unit is
# enabled or active, and prints the disable command instead.
#
# Only symlinks resolving into this checkout's systemd/ are removed; regular
# files and links pointing elsewhere are left alone. data/ is never touched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

units=()
for path in "$ROOT"/systemd/*.service; do
  units+=("$(basename "$path")")
done

busy=()
for unit in "${units[@]}"; do
  enabled="$(systemctl --user is-enabled "$unit" 2>/dev/null || true)"
  active="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
  state=()
  case "$enabled" in enabled | enabled-runtime) state+=("$enabled") ;; esac
  case "$active" in active | activating | reloading) state+=("$active") ;; esac
  if [ "${#state[@]}" -gt 0 ]; then
    busy+=("$unit")
    echo "$unit: ${state[*]}"
  fi
done

if [ "${#busy[@]}" -gt 0 ]; then
  echo
  echo "Refusing to unlink: the units above are enabled or active. Run first:"
  echo "    systemctl --user disable --now ${busy[*]}"
  exit 1
fi

for unit in "${units[@]}"; do
  link="$UNIT_DIR/$unit"
  if [ -L "$link" ]; then
    target="$(readlink -f "$link")"
    case "$target" in
      "$ROOT"/systemd/*)
        rm "$link"
        echo "unlinked $unit"
        ;;
      *) echo "skipped $unit (links to $target, not this checkout)" ;;
    esac
  elif [ -e "$link" ]; then
    echo "skipped $unit (regular file, not a link)"
  else
    echo "not linked $unit"
  fi
done

systemctl --user daemon-reload
