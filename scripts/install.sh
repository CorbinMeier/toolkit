#!/usr/bin/env bash
# Link the systemd user units into place and reload the daemon.
#
# Deliberately does NOT enable or start anything -- per the Gatekeeper
# Protocol, enabling units is the user's call. The commands to do so are
# printed at the end.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$UNIT_DIR"
for unit in "$ROOT"/systemd/*.service; do
  ln -sf "$unit" "$UNIT_DIR/$(basename "$unit")"
  echo "linked $(basename "$unit")"
done

systemctl --user daemon-reload
echo
echo "Units linked but NOT enabled. To start them now:"
echo "    systemctl --user start desktop-dashboard-serve desktop-dashboard-collect desktop-dashboard-host"
echo
echo "To also start them at every login:"
echo "    systemctl --user enable desktop-dashboard-serve desktop-dashboard-collect desktop-dashboard-host"
echo
echo "After editing web/, repaint without a restart:"
echo "    systemctl --user reload desktop-dashboard-host"
